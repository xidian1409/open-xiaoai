"""原生小爱 TTS：常驻 aplay + FIFO 的连续播放，句与句之间没有缝隙。

为什么要换掉"一个文件一次 mediaplayer"
--------------------------------------
实测（音箱回采通道量化，见 DESIGN.md §2.6）：每调用一次
`mediaplayer player_play_url` 播一个新文件，从上一个文件被切断到新文件出声，
中间有固定 ~130 ms 的静音（与 mp3/wav、type 参数、group 参数无关，是播放器
自身的启停代价）。用 `sleep(音频时长 + 0.2)` 接力，实测缝约 330~350 ms。

所以只要"每句一个文件、逐句重开播放器"，缝就压不到 100 ms 以下。改成：

1. **一个 aplay 进程 + 一个命名管道（FIFO）**，整个回答期间只开一次 ALSA，
   音频以 PCM 形式连续写进去 —— 实测两段音频拼起来是**连续波形，缝 0 ms**。
2. aplay 在**回答开始时就启起来**（FIFO 里还没有数据，它阻塞等），
   所以播放器启动代价被大模型出字的时间掩盖，首字不会变慢。
3. 设备上的喂料进程按顺序 `curl` 取 PCM：桥接侧把 mibrain 合成的 mp3 解码成
   16 kHz 单声道 PCM 直接回在 HTTP 响应体里，设备写进 FIFO。mp3 只有几 KB、
   PCM 每秒 32 KB，走局域网 HTTP，不占 WebSocket 控制通道（早期 PCM 推流
   就是死在把数据塞进 RPC 通道上）。
4. 播完的标志：喂料进程关掉 FIFO 写端 → aplay 排空缓冲后退出 → 落一个
   `finished` 标记，桥接侧轮询它就知道"说完了"。

保留文件播放（`XIAOQI_PLAYBACK_MODE=file`）作为兜底：整段 cat 成一个文件，
用常驻 mediaplayer 播，首句优先模式下段间约 130 ms 缝。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shlex
from urllib.parse import urlsplit

from config import APP_CONFIG
from xiaozhi.services.native_tts import NativeXiaomiTTS

BATCH_FILE = "/tmp/open-xiaoai-batch.mp3"
MAX_PARALLEL_SYNTH = 4
BYTES_PER_SECOND = 4000  # mibrain 恒定 32 kbps
PLAY_STOP_ACTION = '{"action":"stop"}'

# ---------- 流式播放（常驻 aplay + FIFO） ----------
SINK_DIR = "/tmp/xq-tts"
SINK_FIFO = "/tmp/xq-tts.pcm"
SINK_SCRIPT = "/tmp/xq-sink.sh"
SINK_RATE = 16000
SINK_READY_TIMEOUT_MS = 4000
SINK_FINISH_POLL_MS = 300
SINK_PUSH_RETRIES = 3

SINK_SCRIPT_BODY = r"""#!/bin/sh
# 小七的音频水槽：aplay 常开，PCM 从命名管道连续灌进来，句间没有重开的代价。
# 用法: xq-sink.sh <桥接host:port> <采样率>
BR="$1"
RATE="${2:-16000}"
DIR=/tmp/xq-tts
rm -rf "$DIR"
mkdir -p "$DIR/N"
rm -f "$DIR.pcm"
mkfifo "$DIR.pcm" || exit 1
echo $$ > "$DIR/sink.pid"
# -B 200 ms：ALSA 起播门槛跟着变小，首字比默认 500 ms 缓冲快 ~0.3 s；
# 前面还有 64 KB 命名管道（约 2 s）兜着，实测 3 句连播没有一次 underrun。
nice -n -10 aplay -B 200000 -t raw -f S16_LE -r "$RATE" -c 1 "$DIR.pcm" >"$DIR/aplay.log" 2>&1 &
AP=$!
echo $AP > "$DIR/aplay.pid"
exec 3>"$DIR.pcm" || exit 1
touch "$DIR/ready"
n=1
while :; do
  # 先看队列里还有没有第 n 段，再看停止标记：桥接是"排完最后一段才落 stop"，
  # 如果先判 stop 就会把最后一段吞掉。
  if [ -L "$DIR/N/$n" ] || [ -e "$DIR/N/$n" ]; then
    SRC=$(readlink "$DIR/N/$n")
    if [ -n "$SRC" ]; then
      curl -s --max-time 30 --data-binary "@$SRC" "http://$BR/pcm" >&3 2>>"$DIR/curl.log"
    fi
    rm -f "$DIR/N/$n"
    n=$((n+1))
  elif [ -f "$DIR/stop" ]; then
    break
  else
    sleep 0.04
  fi
done
exec 3>&-
wait $AP 2>/dev/null
rm -f "$DIR.pcm"
UR=$(grep -c underrun "$DIR/aplay.log" 2>/dev/null)
URMAX=$(grep -o "at least [0-9.]*" "$DIR/aplay.log" 2>/dev/null | tail -1)
echo "underruns=${UR:-0} worst=${URMAX:-none}" > "$DIR/finished"
"""


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


def _sink_script_command() -> str:
    """把水槽脚本写成一条单行命令（printf 逐行输出）。"""
    lines = SINK_SCRIPT_BODY.strip("\n").splitlines()
    quoted = []
    for line in lines:
        if "'" in line:
            raise ValueError("水槽脚本里不能出现单引号")
        quoted.append(f"'{line}'")
    body = " ".join(quoted)
    return f"printf '%s\\n' {body} > {SINK_SCRIPT} && chmod +x {SINK_SCRIPT}"


def bridge_host_port() -> str:
    """设备该用哪个地址回连桥接：跟 OTA_URL 同一个主机，端口用 TTS 收件端口。"""
    port = os.environ.get("TTS_INBOX_PORT", "4398")
    url = ""
    try:
        url = str((APP_CONFIG.get("xiaozhi") or {}).get("OTA_URL") or "")
    except Exception:  # noqa: BLE001
        url = ""
    host = urlsplit(url).hostname if url else ""
    if not host:
        host = os.environ.get("XIAOQI_BRIDGE_HOST", "")
    return f"{host}:{port}"


class NativeXiaomiTTSPCM(NativeXiaomiTTS):
    """沿用上游的分段/预取/打断逻辑，只改"怎么合成""怎么播"。"""

    def __init__(self, speaker, settings=None):
        super().__init__(speaker, settings)
        self._first_sentence_play = env_flag("XIAOQI_FIRST_SENTENCE_PLAY", False)
        mode = os.environ.get("XIAOQI_PLAYBACK_MODE", "stream").strip().lower()
        self._playback_mode = mode if mode in ("stream", "file") else "stream"
        self._sink_task = None
        self._sink_ok = False
        self._sink_count = 0
        self._sink_script_ready = False
        self._sink_underruns = ""

    # ---------- 会话生命周期 ----------

    async def start(self, session_id=None):
        token = await super().start(session_id)
        # 先把 aplay 拉起来（它阻塞等 FIFO 里的第一字节），
        # 这样等第一句合完时播放通道已经就绪，起播代价被大模型出字的时间盖掉。
        if self._playback_mode == "stream":
            self._sink_count = 0
            # 先把音乐停掉再开声卡：aplay 和 mediaplayer 抢同一个设备，
            # 音乐还在响时开管道有可能打不开。这一步和大模型出字并行，不占首字。
            try:
                await self._pause_music_if_needed()
            except Exception:  # noqa: BLE001
                pass
            self._sink_task = asyncio.create_task(self._sink_start())
        return token

    async def _sink_start(self):
        try:
            ok = await self._start_sink()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            print(f"⚠️ 流式播放启动失败，本轮回退整段播放: {str(error)[:120]}")
            ok = False
        self._sink_ok = ok

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
        """流式优先：一句一句塞进常驻 aplay；失败则回退整段播放。"""
        items = []
        streamed = 0
        try:
            while token == self._token:
                item = await self._audio_queue.get()
                if item is None:
                    break
                if not self._music_state_checked:
                    await self._pause_music_if_needed()

                # 水槽是会话开始时异步拉起的，第一段到货时通常已经就绪；
                # 万一还没好，这里等一下（它自己带 4 秒超时）。
                if self._playback_mode == "stream" and self._sink_task and not self._sink_task.done():
                    await self._sink_task
                    self._sink_task = None

                if self._playback_mode == "stream" and self._sink_ok:
                    if await self._sink_push(item[0]):
                        streamed += 1
                        continue
                    # 推不进管道就转成文件播放，已经进管道的仍会照常播完。
                    print("⚠️ 流式喂料失败，余下改走整段播放")

                if self._first_sentence_play and not items and not streamed:
                    print("▶️ 首句先播（其余边播边合成）")
                    await self._play_items([item])
                    continue

                items.append(item)

            if streamed:
                await self._sink_finish(streamed)
            if items:
                await self._play_items(items)
        except asyncio.CancelledError:
            raise
        finally:
            if token == self._token:
                self._sink_ok = False
                await self._sink_cleanup()
                await self._cleanup_generated_files()
                await self._restore_music_if_needed()
                self._active = False
                self._done_event.set()

    # ---------- 流式播放：设备侧 aplay + FIFO ----------

    async def _start_sink(self) -> bool:
        host_port = bridge_host_port()
        # 先清掉上一轮可能残留的水槽：start-stop-daemon 按可执行名查重，
        # 残留进程会让"新水槽"根本不启动，然后就只能降级。
        await self._sink_cleanup()
        if not host_port.startswith(":"):
            await self._ensure_sink_script()
            # 用脚本自身当 -x：busybox 的 start-stop-daemon 会按可执行名查重，
            # 用 /bin/sh 会误判成"已经在跑"。
            cmd = (
                f"start-stop-daemon -S -b -x {SINK_SCRIPT} -- "
                f"{shlex.quote(host_port)} {SINK_RATE} >/dev/null 2>&1"
            )
            result = await self.speaker.run_shell(cmd, timeout=6000)
            if result.exit_code != 0:
                raise RuntimeError(
                    (result.stderr or f"start-stop-daemon exit {result.exit_code}").strip()
                )
            deadline = SINK_READY_TIMEOUT_MS
            waited = 0
            while waited < deadline:
                probe = await self.speaker.run_shell(
                    f"[ -f {SINK_DIR}/ready ] && echo yes", timeout=3000
                )
                if "yes" in (probe.stdout or ""):
                    print(f"🎚️ 播放水槽就绪: {waited + 120}ms")
                    return True
                await asyncio.sleep(0.1)
                waited += 130
            raise RuntimeError("aplay 水槽 4 秒内没有就绪")
        raise RuntimeError("拿不到桥接地址，无法回连")

    async def _ensure_sink_script(self) -> None:
        if self._sink_script_ready:
            return
        # 一条命令写完脚本（不依赖 heredoc：设备侧的命令执行器只适合单行命令）
        await self.speaker.run_shell(_sink_script_command(), timeout=6000)
        self._sink_script_ready = True

    async def _sink_push(self, audio_path: str) -> bool:
        """把一段音频排进水槽：只做一个符号链接，数据由设备自己 curl 取。"""
        self._sink_count += 1
        index = self._sink_count
        cmd = f"ln -sf {shlex.quote(audio_path)} {SINK_DIR}/N/{index}"
        for attempt in range(SINK_PUSH_RETRIES):
            try:
                result = await self.speaker.run_shell(cmd, timeout=4000)
                if result.exit_code == 0:
                    return True
                detail = (result.stderr or result.stdout or "").strip()
                print(f"⚠️ 排队第 {index} 段失败({attempt + 1}): {detail[:80]}")
            except Exception as error:  # noqa: BLE001
                print(f"⚠️ 排队第 {index} 段异常({attempt + 1}): {str(error)[:80]}")
            await asyncio.sleep(0.08)
        # 实在排不进去就落一个空占位，让喂料进程跳过这一段而不是永远等下去
        print(f"⚠️ 第 {index} 段放弃排队，落空占位跳过")
        try:
            result = await self.speaker.run_shell(
                f"busybox touch {SINK_DIR}/N/{index}", timeout=3000
            )
            return result.exit_code == 0
        except Exception:  # noqa: BLE001
            return False

    async def _sink_finish(self, streamed: int) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await self.speaker.run_shell(f"touch {SINK_DIR}/stop", timeout=3000)
        except Exception as error:  # noqa: BLE001
            print(f"⚠️ 结束水槽失败: {str(error)[:80]}")
        timeout = self.settings.play_timeout_ms
        watched = (
            f"if [ -f {SINK_DIR}/finished ]; then echo yes:$(cat {SINK_DIR}/finished); "
            f"elif ! kill -0 $(cat {SINK_DIR}/sink.pid 2>/dev/null) 2>/dev/null; "
            "then echo dead; fi"
        )
        waited = 0
        while waited < timeout:
            try:
                probe = await self.speaker.run_shell(watched, timeout=3000)
            except Exception:  # noqa: BLE001
                probe = None
            answer = (probe.stdout or "") if probe is not None else ""
            if answer.startswith("yes"):
                self._sink_underruns = answer.replace("yes:", "", 1).strip()
                break
            if "dead" in answer:
                print("⚠️ 播放水槽中途退出，本轮余下音频丢失")
                break
            await asyncio.sleep(SINK_FINISH_POLL_MS / 1000)
            waited += SINK_FINISH_POLL_MS + 120
        else:
            print("⚠️ 等待播放结束超时，强制停止")
        elapsed = loop.time() - started
        status = getattr(self, "_sink_underruns", "") or "?"
        print(
            f"✅ 原生小爱 TTS 连续播放完成: segs={streamed} "
            f"尾部排空={elapsed:.3f}s {status}"
        )
        if not status.startswith("underruns=0"):
            try:
                detail = await self.speaker.run_shell(
                    f"tail -20 {SINK_DIR}/aplay.log 2>/dev/null", timeout=3000
                )
                print("🔍 水槽 underrun 明细:\n" + (detail.stdout or "").strip())
            except Exception:  # noqa: BLE001
                pass
        await self._sink_cleanup()

    async def _sink_cleanup(self) -> None:
        try:
            await self.speaker.run_shell(
                f"busybox kill $(cat {SINK_DIR}/sink.pid 2>/dev/null) "
                f"$(cat {SINK_DIR}/aplay.pid 2>/dev/null) 2>/dev/null; "
                f"rm -rf {SINK_DIR} {SINK_FIFO}",
                timeout=4000,
            )
        except Exception:  # noqa: BLE001
            pass

    # ---------- 文件播放（兜底） ----------

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
        # 所以完全按估算时长等，不再轮询状态——每次查询都是一次设备 RPC。
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
        task = self._sink_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._sink_task = None
        await self._sink_cleanup()
        self._sink_ok = False
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
