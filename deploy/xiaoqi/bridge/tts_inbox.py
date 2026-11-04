"""桥接容器里的小收件服务器：接收音箱上传的 TTS mp3。

为什么需要它
------------
原生音色是 mibrain 在音箱上合成 mp3 的，文件在音箱的 /tmp 里；要把音频变成
PCM 推流就得在桥接侧解码，所以必须先把文件从音箱取回来。

试过用 `hexdump -C` 把文件转成文本走 run_shell 拿回来，但 8 KB 的音频变成
70 KB 文本后会在链路上被截断（实测丢了 7 行），不可靠。改成音箱用 curl 直接
POST 二进制上来，一次往返、无损。

端口默认 4398，路径 POST /tts?id=<名字>，落盘到 /tmp/tts_inbox/<名字>.mp3。
"""

from __future__ import annotations

import io
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INBOX_DIR = os.environ.get("TTS_INBOX_DIR", "/tmp/tts_inbox")
PORT = int(os.environ.get("TTS_INBOX_PORT", "4398"))
MAX_BYTES = 2 * 1024 * 1024
PCM_RATE = int(os.environ.get("TTS_PCM_RATE", "16000"))
# mibrain 每段音频自带 ~85 ms 前导静音、~560 ms 结尾静音（实测），
# 直接拼起来每个句子之间就有 ~650 ms 冷场。这里把停顿归一到 ~0.17 s。
TRIM_LEAD_MS = int(os.environ.get("TTS_TRIM_LEAD_MS", "30"))
TRIM_TAIL_MS = int(os.environ.get("TTS_TRIM_TAIL_MS", "100"))
TRIM_MARGIN_MS = int(os.environ.get("TTS_TRIM_MARGIN_MS", "20"))


def _trim_silence(data, rate: int):
    """把前后静音收拢到固定长度，句间停顿才不会被放大到 0.6 s。"""
    import numpy as np

    if data.size == 0:
        return data
    peak = float(np.abs(data).max())
    if peak <= 0:
        return data
    threshold = max(peak * 0.02, 200.0)
    mask = np.abs(data) > threshold
    if not mask.any():
        return data
    margin = int(rate * TRIM_MARGIN_MS / 1000)
    first = max(0, int(np.argmax(mask)) - margin)
    last = min(len(data) - 1, len(data) - 1 - int(np.argmax(mask[::-1])) + margin)
    body = data[first : last + 1]
    lead = np.zeros(int(rate * TRIM_LEAD_MS / 1000), dtype="<i2")
    tail = np.zeros(int(rate * TRIM_TAIL_MS / 1000), dtype="<i2")
    return np.concatenate([lead, body, tail])


def decode_to_pcm(body: bytes) -> bytes:
    """把 mibrain 合成的 mp3 解成 PCM（16 kHz 单声道 s16le）。

    设备端 aplay 从命名管道连续吞 PCM，整个回答只开一次声卡，句间没有
    "重开播放器"的 130 ms 静音。mp3 在设备上、解码放桥接侧，所以走
    "设备 POST mp3、响应体直接回 PCM"的一次往返。
    """
    import numpy as np
    import soundfile as sf

    data, rate = sf.read(io.BytesIO(body), dtype="int16", always_2d=False)
    if getattr(data, "ndim", 1) > 1:
        data = data[:, 0]
    if rate != PCM_RATE:
        import soxr

        data = np.clip(
            np.round(soxr.resample(data.astype(np.float32), rate, PCM_RATE)),
            -32768,
            32767,
        ).astype(np.int16)
        rate = PCM_RATE
    data = _trim_silence(data, rate)
    return np.ascontiguousarray(data, dtype="<i2").tobytes()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/pcm"):
            self._serve_pcm()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BYTES:
                self._reply(400, b"bad length")
                return
            data = self.rfile.read(length)
            raw_id = self.path.split("id=", 1)[-1]
            name = re.sub(r"[^0-9A-Za-z_-]", "", raw_id)[:48] or "unknown"
            os.makedirs(INBOX_DIR, exist_ok=True)
            target = os.path.join(INBOX_DIR, f"{name}.mp3")
            staging = f"{target}.part"
            with open(staging, "wb") as handle:
                handle.write(data)
            os.replace(staging, target)  # 原子改名，读取方永远看不到半个文件
            self._reply(200, b"ok")
        except Exception as error:  # noqa: BLE001
            self._reply(500, str(error).encode())

    def _serve_pcm(self) -> None:
        """POST 一个 mp3，响应体就是 PCM（设备 curl 直接写进 FIFO）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BYTES:
                self._reply(400, b"bad length")
                return
            body = self.rfile.read(length)
            pcm = decode_to_pcm(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(pcm)))
            self.end_headers()
            self.wfile.write(pcm)
        except Exception as error:  # noqa: BLE001
            self._reply(500, f"decode failed: {error}".encode())

    def log_message(self, *args):  # 静音，别把桥接日志刷乱
        return


def main() -> None:
    os.makedirs(INBOX_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"📥 TTS 收件服务已启动: 0.0.0.0:{PORT} → {INBOX_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
