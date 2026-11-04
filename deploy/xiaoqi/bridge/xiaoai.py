import argparse
import asyncio
import os
import threading
import json
import time

import numpy as np
import open_xiaoai_server

from config import APP_CONFIG
from xiaozhi.event import EventManager
from xiaozhi.ref import get_speaker, set_xiaoai
from xiaozhi.services.audio.aec import SubbandEchoCanceller
from xiaozhi.services.audio.stream import GlobalStream
from xiaozhi.services.audio.health import HEALTH
from xiaozhi.services.speaker import SpeakerManager
from xiaozhi.utils.base import json_decode

ASCII_BANNER = """
▄▖      ▖▖▘    ▄▖▄▖
▌▌▛▌█▌▛▌▚▘▌▀▌▛▌▌▌▐ 
▙▌▙▌▙▖▌▌▌▌▌█▌▙▌▛▌▟▖
  ▌                
                                                                                                                
v1.0.0  by: https://del.wang
"""


# The speaker records one microphone plus a loopback of its own playback
# (see the client's echo_ref); the bridge cancels the echo.
DEFAULT_ECHO_CAPTURE = {
    "pcm": "Capture",
    "channels": 4,
    "sample_rate": 48000,
    "mic_channel": 0,
    "ref_channel": 3,
    "mic_shift": 14,
    "ref_shift": 16,
}
# Echo-reference frames older than this mean the speaker stopped sending them.
ECHO_REFERENCE_STALE_S = 2.0

# 排查"段间缝"用的回环录音开关：存在 /data/logs/ref-dump.on 时，
# 把音箱回传的播放参考通道（= 扬声器真正在放的东西）原样追加到
# /data/logs/ref-dump.pcm（48 kHz 单声道 S16），平时零开销。
REF_DUMP_MARKER = os.environ.get("XIAOQI_REF_DUMP_MARKER", "/data/logs/ref-dump.on")
REF_DUMP_FILE = os.environ.get("XIAOQI_REF_DUMP_FILE", "/data/logs/ref-dump.pcm")


class XiaoAI:
    mode = "xiaoai"
    speaker = SpeakerManager()
    async_loop: asyncio.AbstractEventLoop = None
    echo = None  # SubbandEchoCanceller, created on the first stereo frame
    echo_gain = 1.0
    _echo_seen = None
    _ref_dump = None
    _ref_dump_check = 0
    # GlobalStream input up to this sample may still carry (cancelled) playback.
    echo_until = -1

    @classmethod
    def setup_mode(cls):
        set_xiaoai(cls)
        parser = argparse.ArgumentParser(
            description="小爱音箱接入小智 AI | by: https://del.wang"
        )
        parser.add_argument(
            "--mode",
            type=str,
            choices=["xiaoai", "xiaozhi"],
            default="xiaoai",
            help="运行模式：【xiaoai】使用小爱音箱的输入输出音频（默认）、【xiaozhi】使用本地电脑的输入输出音频",
        )
        args = parser.parse_args()
        if args.mode == "xiaozhi":
            cls.mode = "xiaozhi"

    @classmethod
    def on_input_data(cls, data: bytes):
        HEALTH.pcm(np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0)
        cls._feed_input(data)

    @classmethod
    def _feed_input(cls, data):
        audio_array = np.frombuffer(data, dtype=np.uint16)
        GlobalStream.input(audio_array.tobytes())

    @classmethod
    def on_input_packet(cls, packet):
        data, metadata = packet
        # 老版本的音箱客户端不发 packet 元信息，Rust 侧会给一个 "null"，
        # 原来这里直接 json.loads 后当 dict 用 -> AttributeError，音频帧被整帧丢弃。
        try:
            meta = json.loads(metadata) if metadata else None
        except (TypeError, ValueError):
            meta = None
        if isinstance(meta, dict) and meta.get("layout") == "mic_ref":
            data = cls._cancel_echo(data)
        HEALTH.pcm(
            np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0,
            meta if isinstance(meta, dict) else None,
        )
        cls._feed_input(data)

    @classmethod
    def _dump_ref(cls, ref):
        """把播放回环落盘（见 REF_DUMP_MARKER），用于量起播延迟和段间缝。"""
        cls._ref_dump_check += 1
        if cls._ref_dump_check % 25 == 0:  # 约 0.5 秒查一次开关文件
            alive = os.path.exists(REF_DUMP_MARKER)
            if alive and cls._ref_dump is None:
                try:
                    cls._ref_dump = open(REF_DUMP_FILE, "ab", buffering=0)
                    # 记下开始时刻（容器内 monotonic），分析端才能把回采波形
                    # 和桥接侧的日志时间对齐，算出真正的起播延迟。
                    with open(REF_DUMP_FILE + ".start", "w") as meta:
                        meta.write(f"{time.monotonic():.3f}")
                except OSError:
                    return
            elif not alive and cls._ref_dump is not None:
                try:
                    cls._ref_dump.close()
                except OSError:
                    pass
                cls._ref_dump = None
                return
        if cls._ref_dump is None:
            return
        try:
            cls._ref_dump.write(ref.tobytes())
        except OSError:
            cls._ref_dump = None

    @classmethod
    def _cancel_echo(cls, data):
        """Stereo [mic, playback] S16 in, the microphone without the
        playback out, as mono S16 at the level the rest expects."""
        frames = np.frombuffer(data, dtype="<i2").reshape(-1, 2)
        cls._dump_ref(frames[:, 1])
        if cls.echo is None:
            settings = APP_CONFIG.get("barge_in", {}).get("echo_canceller", {})
            cls.echo = SubbandEchoCanceller(
                taps=int(settings.get("taps", 8)),
                tau_s=float(settings.get("tau_s", 3.0)),
            )
            cls.echo_gain = float(settings.get("output_gain", 64))
        cleaned = cls.echo.process(frames[:, 0], frames[:, 1]) * cls.echo_gain
        cls._echo_seen = time.monotonic()
        if np.any(frames[:, 1]):
            # This packet's echo reaches the output one canceller delay later
            # and the room keeps ringing a little after that.
            hangover = int(APP_CONFIG.get("barge_in", {}).get("echo_vad_hangover_ms", 300)) * 16
            cls.echo_until = GlobalStream.samples + len(frames) + cls.echo.delay + hangover
        reduction = cls.echo.reduction_db()
        HEALTH.update(aec_ref_active=cls.echo.ref_active,
                      aec_reduction_db=None if reduction is None else round(reduction, 1))
        return np.clip(np.round(cleaned), -32768, 32767).astype("<i2").tobytes()

    @classmethod
    def echo_reference_live(cls):
        seen = cls._echo_seen
        return seen is not None and time.monotonic() - seen < ECHO_REFERENCE_STALE_S

    @classmethod
    def on_health_event(cls, event):
        record = json.loads(event)
        HEALTH.emit(record["event"], **record.get("fields", {}))

    @classmethod
    def on_output_data(cls, data: bytes):
        async def on_output_data_async(data: bytes):
            return await open_xiaoai_server.on_output_data(data)

        asyncio.run_coroutine_threadsafe(
            on_output_data_async(data),
            cls.async_loop,
        )

    @classmethod
    async def run_shell(cls, script: str, timeout: float = 10 * 1000):
        return await open_xiaoai_server.run_shell(script, timeout)

    @classmethod
    async def on_event(cls, event: str):
        event_json = json_decode(event) or {}
        event_data = event_json.get("data", {})
        event_type = event_json.get("event")

        if not event_json.get("event"):
            return

        if event_type == "instruction" and event_data.get("NewLine"):
            line = json_decode(event_data.get("NewLine"))
            if (
                line
                and line.get("header", {}).get("namespace") == "SpeechRecognizer"
                and line.get("header", {}).get("name") == "RecognizeResult"
            ):
                text = line.get("payload", {}).get("results")[0].get("text")
                if not text and not line.get("payload", {}).get("is_vad_begin"):
                    print("🔥 唤醒小爱")
                    EventManager.on_interrupt()
                elif text and line.get("payload", {}).get("is_final"):
                    print(f"🔥 收到指令: {text}")
                    await EventManager.wakeup(text, "xiaoai")
        elif event_type == "playing":
            get_speaker().status = event_data.lower()

    @classmethod
    def __init_background_event_loop(cls):
        def run_event_loop():
            cls.async_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(cls.async_loop)
            cls.async_loop.run_forever()

        thread = threading.Thread(target=run_event_loop, daemon=True)
        thread.start()

    @classmethod
    def __on_event(cls, event: str):
        asyncio.run_coroutine_threadsafe(
            cls.on_event(event),
            cls.async_loop,
        )

    @classmethod
    async def init_xiaoai(cls):
        from xiaozhi.ref import get_kws, get_xiaozhi
        def probe():
            kws = get_kws()
            stream = getattr(kws, "stream", None)
            state = getattr(get_xiaozhi(), "device_state", None)
            return {"kws_paused": bool(getattr(kws, "paused", False)), "device_state": getattr(state, "name", str(state)),
                    "kws_reset_pending": getattr(kws, "_reset_reason", None) is not None,
                    "kws_thread_alive": bool(getattr(kws, "thread", None) and kws.thread.is_alive()),
                    "kws_buffer_samples": len(getattr(stream, "input_bytes", [])) // 2}
        HEALTH.start(probe)
        GlobalStream.on_output_data = cls.on_output_data
        barge_in = APP_CONFIG.get("barge_in", {})
        if barge_in.get("echo_reference", False):
            capture = {**DEFAULT_ECHO_CAPTURE, **barge_in.get("capture", {})}
            open_xiaoai_server.set_echo_ref(json.dumps(capture))
            print("🔁 请音箱同时发送播放回采，用于消除回声"
                  + ("；小七说话时可以喊唤醒词打断" if barge_in.get("enabled") else ""))
        open_xiaoai_server.register_fn("on_input_data", cls.on_input_data)
        open_xiaoai_server.register_fn("on_input_packet", cls.on_input_packet)
        open_xiaoai_server.register_fn("on_health_event", cls.on_health_event)
        open_xiaoai_server.register_fn("on_event", cls.__on_event)
        cls.__init_background_event_loop()
        print(ASCII_BANNER)
        await open_xiaoai_server.start_server()
