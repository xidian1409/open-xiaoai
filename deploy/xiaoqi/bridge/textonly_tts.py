"""只发文字、不合成音频的 TTS provider（给桥接端 native_xiaomi 模式用）。

为什么需要它
------------
xiaozhi-server 的 TTS 流程是「先把这句话合成成音频，再把 sentence_start(文本)
和音频一起下发」（base.py: `text_to_speak()` 返回之后才 put 进音频队列）。
桥接端的小爱原生音色只能等这条文本到达才开始合成，于是白白承担了 EdgeTTS
的云端耗时——实测每句 1.05~1.19 s，正好压在"首字延迟"上。

把服务端换成这个 provider 之后：
    文本  ── 立即下发 ──▶  桥接 mibrain 合成 0.12~0.39 s  ──▶  PCM 播放
首字延迟从 ~1.2 s 降到 ~0.4 s，而且 TTS 彻底不再依赖网络。

实现细节
--------
不能返回空 bytes：`to_tts_stream()` 里 `if audio_bytes:` 为假会重试 5 次然后
判定"语音生成失败"。所以返回一段 60 ms 的静音 mp3，让服务端的流控/上报按原
逻辑走，音频本身在桥接侧会被丢弃（native_xiaomi 模式不消费服务端音频）。
"""

from __future__ import annotations

import base64

from core.providers.tts.base import TTSProviderBase

# 60 ms / 16 kHz / mono 静音 mp3（801 字节），只为满足"有音频"这一判断
_SILENT_MP3 = base64.b64decode(
    "SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjYyLjEyLjEwMgAAAAAAAAAAAAAA//NYwAAAAAAAAAAAAEluZm8AAAAP"
    "AAAABAAAAvQAbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tnp6enp6enp6enp6enp6enp6enp6enp6ens/Pz8/Pz8/P"
    "z8/Pz8/Pz8/Pz8/Pz8/Pz8//////////////////////////////////AAAAAExhdmM2Mi4yOAAAAAAAAAAAAAAA"
    "ACQDAAAAAAAAAAL05RM1vAAAAAAAAAAAAAAA//NIxAAAAANIAAAAAExBTUVVVVVMQU1FMy4xMDBVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NIxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4x"
    "MDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NIxHwAAANIAAAAAFVV"
    "VVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NI"
    "xHwAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVV"
)


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file=True):
        super().__init__(config, delete_audio_file)
        self.audio_file_type = "mp3"

    def generate_filename(self, extension=".mp3"):
        import os
        import uuid

        return os.path.join(self.output_file, f"silent-{uuid.uuid4().hex}{extension}")

    async def text_to_speak(self, text, output_file):
        if output_file:
            with open(output_file, "wb") as handle:
                handle.write(_SILENT_MP3)
            return None
        return _SILENT_MP3
