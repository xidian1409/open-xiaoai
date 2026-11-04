"""原生小爱 TTS：并行合成 + 整段拼接 + 用常驻 mediaplayer 播。

三条设计决定
------------
1. **不在 NAS 侧实时推 PCM**。LX06 播音频时 load 6+、可用内存 5 MB，实时推流会
   反复断流。现在播放期间设备只读本地文件，不依赖网络实时性。

2. **合成阶段并行 + 少 RPC**。每句只调一次 `ubus call mibrain text_to_speech`
   （直接用它返回的路径，不搬文件），句子之间并发，尺寸一次批量 `wc -c` 取回。
   实测每段从 1.4~2.1 s 降到 0.3~0.5 s，合成几乎完全和大模型输出重叠。

3. **播放走常驻的 mediaplayer 守护进程**（`player_play_url` + `file://`），
   不再每次 spawn 一个 miplayer。实测起播 < 0.2 s（miplayer 约 0.35 s），
   而且播放由守护进程独立完成，桥接侧的 RPC 抖动不会影响声音。

首句优先（可选）
----------------
`first_sentence_play` 打开后，第一句合成完就立刻播，其余句子边播边合成，
首字能从 ~3 s 压到 ~1.5 s；代价是两段之间有一次约 0.2~0.35 s 的衔接缝。
默认关闭，用环境变量 `XIAOQI_FIRST_SENTENCE_PLAY=1` 打开。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shlex

from xiaozhi.services.native_tts import NativeXiaomiTTS

BATCH_FILE = "/tmp/open-xiaoai-batch.mp3"
MAX_PARALLEL_SYNTH = 4
BYTES_PER_SECOND = 4000  # mibrain 恒定 32 kbps
PLAY_STOP_ACTION = '{"action":"stop"}'


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_tts_path(stdout: str) -> str | None:
    """从 `ubus call mibrain text_to_speech` 的响应里取音频路径。"""
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    info = payload.get("info")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            return None
    if isinstance(info, dict):
        path = info.get("path")
        if isinstance(path, str) and path.startswith("/"):
            return path
    return None


def parse_play_status(stdout: str) -> int | None:
    """解析 `player_get_play_status`，返回 status（1 = 正在播）。"""
    try:
        outer = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    info = outer.get("info")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            return None
    if isinstance(info, dict):
        status = info.get("status")
        if isinstance(status, int):
            return status
    return None


class NativeXiaomiTTSPCM(NativeXiaomiTTS):
    """沿用上游的分段/预取/打断逻辑，只改"怎么合成""怎么播"。"""

    def __init__(self, speaker, settings=None):
        super().__init__(speaker, settings)
        self._first_sentence_play = env_flag("XIAOQI_FIRST_SENTENCE_PLAY", False)

    # ---------- 合成：一句一次 RPC ----------

    async def _generate_audio(self, text, token):
        if token != self._token:
            raise asyncio.CancelledError
        payload = json.dumps(
            {"text": text, "save": 1}, ensure_ascii=False, separators=(",", ":")
        )
        started = asyncio.get_running_loop().time()
        result = await self.speaker.run_shell(
            f"ubus call mibrain text_to_speech {shlex.quote(payload)}",
            timeout=self.settings.generate_timeout_ms,
        )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or f"ubus exit {result.exit_code}")
        path = parse_tts_path(result.stdout or "")
        if not path:
            raise RuntimeError(f"无法解析 mibrain 响应: {result.stdout!r}")
        self._generated_files.add(path)
        cost = (asyncio.get_running_loop().time() - started) * 1000
        print(f"🎵 合成完成: chars={len(text)} {cost:.0f}ms")
        return path

    async def _fill_sizes(self, paths) -> None:
        """一次 RPC 批量取回字节数（判"小米拒读"和算播放时长都要用）。"""
        paths = [path for path in paths if path not in self._generated_sizes]
        if not paths:
            return
        quoted = " ".join(shlex.quote(path) for path in paths)
        result = await self.speaker.run_shell(f"busybox wc -c {quoted}", timeout=5000)
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                self._generated_sizes[parts[1]] = int(parts[0])

    # ---------- 生成 worker：收到一句就开合成，不排队等文本 ----------

    async def _generation_worker(self, token):
        """收一句就起一个合成任务，合成完一段就按顺序立刻交给播放。

        关键点：**按顺序、尽早交付**。如果等全部句子都合成完再一起给播放，
        首句优先就失去意义了（大模型说完最后一句之前，第一句一直卡在手里）。
        """
        semaphore = asyncio.Semaphore(MAX_PARALLEL_SYNTH)
        pending = []
        reading_done = asyncio.Event()

        async def one(segment):
            async with semaphore:
                return await self._generate_audio(segment, token)

        async def reader():
            while token == self._token:
                text = await self._segment_queue.get()
                if text is None:
                    break
                if text:
                    pending.append((text, asyncio.create_task(one(text))))
            reading_done.set()

        reader_task = asyncio.create_task(reader())
        try:
            while True:
                while pending:
                    text, task = pending[0]
                    try:
                        path = await task
                    except asyncio.CancelledError:
                        raise
                    except BaseException as error:  # noqa: BLE001
                        print(f"❌ 某段合成失败，已跳过: {str(error)[:80]}")
                        pending.pop(0)
                        continue
                    pending.pop(0)
                    await self._fill_sizes([path])
                    if self._is_refusal(text, path):
                        await self._delete_audio_file(path)
                        for item in await self._synth_refused(text, token):
                            await self._audio_queue.put(item)
                        continue
                    await self._audio_queue.put((path, text))

                if reading_done.is_set():
                    break
                await asyncio.sleep(0.02)

            print("⏱️ 合成阶段结束")
            await self._audio_queue.put(None)
        except asyncio.CancelledError:
            for _, task in pending:
                task.cancel()
            raise
        except Exception as error:  # noqa: BLE001
            self._error = str(error)
            print(f"❌ 原生小爱 TTS 合成失败: {error}")
            await self._audio_queue.put(None)
        finally:
            reader_task.cancel()

    async def _synth_refused(self, text, token):
        """小米把整段当敏感内容拒读时，拆句重试，仍被拒的直接跳过。"""
        sentences = [part.strip() for part in self._SENTENCE_END.split(text)]
        sentences = [part for part in sentences if part]
        if len(sentences) <= 1:
            print(f"⚠️ 原生小爱 TTS 拒绝朗读，已跳过: {text[:40]}")
            return []

        print(f"⚠️ 原生小爱 TTS 拒绝朗读整段，改为逐句合成: chars={len(text)}")
        paths = []
        for sentence in sentences:
            try:
                paths.append((await self._generate_audio(sentence, token), sentence))
            except Exception as error:  # noqa: BLE001
                print(f"❌ 拆句合成失败: {error}")
        await self._fill_sizes([path for path, _ in paths])

        kept = []
        for path, sentence in paths:
            if self._is_refusal(sentence, path):
                await self._delete_audio_file(path)
                print(f"⚠️ 原生小爱 TTS 拒绝朗读，已跳过: {sentence[:40]}")
                continue
            kept.append((path, sentence))
        return kept

    # ---------- 播放 ----------

    async def _playback_worker(self, token):
        items = []
        played_first = False
        try:
            while token == self._token:
                item = await self._audio_queue.get()
                if item is None:
                    break
                if not self._music_state_checked:
                    await self._pause_music_if_needed()

                if self._first_sentence_play and not played_first:
                    played_first = True
                    print("▶️ 首句先播（其余边播边合成）")
                    await self._play_items([item])
                    continue

                items.append(item)

            if items:
                await self._play_items(items)
        except asyncio.CancelledError:
            raise
        finally:
            if token == self._token:
                await self._cleanup_generated_files()
                await self._restore_music_if_needed()
                self._active = False
                self._done_event.set()

    async def _play_items(self, items) -> None:
        paths = [path for path, _ in items]
        chars = sum(len(text or "") for _, text in items)
        started = asyncio.get_running_loop().time()
        try:
            batch, size = await self._prepare_batch(paths)
            audio_s = size / BYTES_PER_SECOND
            elapsed = await self._play_and_wait(batch, audio_s)
            print(
                f"✅ 原生小爱 TTS 播放完成: segs={len(paths)} chars={chars} "
                f"audio={audio_s:.2f}s elapsed={elapsed:.3f}s "
                f"total={(asyncio.get_running_loop().time() - started):.3f}s"
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self._error = str(error)
            print(f"❌ 原生小爱 TTS 播放失败: {error}")

    async def _prepare_batch(self, paths):
        """返回 (要播的文件, 字节数)；多段时拼成一个文件。"""
        loop = asyncio.get_running_loop()
        started = loop.time()
        if len(paths) == 1:
            batch = paths[0]
            build_cmd = f"busybox wc -c < {shlex.quote(batch)}"
        else:
            quoted = " ".join(shlex.quote(path) for path in paths)
            batch = BATCH_FILE
            build_cmd = (
                f"busybox cat {quoted} > {BATCH_FILE} && busybox wc -c < {BATCH_FILE}"
            )
        result = await self.speaker.run_shell(build_cmd, timeout=10000)
        if result.exit_code != 0:
            raise RuntimeError((result.stderr or result.stdout or "拼接失败").strip())
        try:
            size = int((result.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"读不到拼接后的文件大小: {result.stdout!r}")
        if size <= 0:
            raise RuntimeError("拼接后的音频为空")
        print(
            f"🔗 准备 {len(paths)} 段 → {size} 字节，用时 "
            f"{(loop.time() - started) * 1000:.0f}ms"
        )
        return batch, size

    async def _play_and_wait(self, path: str, audio_s: float) -> float:
        """用常驻的 mediaplayer 播；失败则回退 miplayer。返回实际耗时。"""
        loop = asyncio.get_running_loop()
        started = loop.time()
        payload = json.dumps(
            {"url": f"file://{path}", "type": 1},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self.speaker.run_shell(
            f"ubus call mediaplayer player_play_url {shlex.quote(payload)}",
            timeout=5000,
        )
        started_ok = result.exit_code == 0 and '"code": 0' in (result.stdout or "")
        if not started_ok:
            print(
                "⚠️ mediaplayer 起播失败，回退 miplayer："
                f"{(result.stderr or result.stdout or '').strip()[:80]}"
            )
            return await self._play_with_miplayer(path, audio_s)

        # 等这段播完。mibrain 输出恒定 32 kbps，时长估算误差只有十几毫秒，
        # 所以完全按估算时长等，不再轮询状态——每次查询都是一次设备 RPC，
        # 光排队就带来 ~0.35 s 的衔接缝。多等 0.2 s 覆盖起播延迟，
        # 既不会切掉尾音，段与段之间也就只剩 ~0.05 s 的空隙。
        await asyncio.sleep(audio_s + 0.2)
        return loop.time() - started

    async def _query_status(self) -> int | None:
        try:
            result = await self.speaker.run_shell(
                "ubus call mediaplayer player_get_play_status", timeout=3000
            )
        except Exception:  # noqa: BLE001
            return None
        return parse_play_status(result.stdout or "")

    async def _play_with_miplayer(self, path: str, audio_s: float) -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        watchdog_s = max(5, math.ceil(audio_s) + 15)
        result = await self.speaker.run_shell(
            "busybox timeout "
            f"-t {watchdog_s} -s KILL miplayer -f {shlex.quote(path)}",
            timeout=(watchdog_s + 5) * 1000,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                (result.stderr or f"miplayer exit {result.exit_code}").strip()
            )
        return loop.time() - started

    async def _stop_playback(self) -> None:
        try:
            await self.speaker.run_shell(
                "ubus call mediaplayer player_play_operation "
                f"{shlex.quote(PLAY_STOP_ACTION)}",
                timeout=2000,
            )
        except Exception:  # noqa: BLE001
            pass

    # ---------- 打断 ----------

    async def abort(self):
        await super().abort()
        await self._stop_playback()

    # ---------- 清理 ----------

    async def _delete_audio_file(self, audio_path):
        """只记账，真正删除留到收尾一次做完（省掉每段一次 RPC）。"""
        self._generated_files.discard(audio_path)
        self._generated_sizes.pop(audio_path, None)

    async def _cleanup_generated_files(self):
        paths = list(self._generated_files)
        self._generated_files.clear()
        self._generated_sizes.clear()
        try:
            quoted = " ".join(shlex.quote(path) for path in paths)
            await self.speaker.run_shell(
                f"busybox rm -f {quoted} {BATCH_FILE}".strip(), timeout=3000
            )
        except Exception:  # noqa: BLE001
            pass
        await self._cleanup_stale_files()
