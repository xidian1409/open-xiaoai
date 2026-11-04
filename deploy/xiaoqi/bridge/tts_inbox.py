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

import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INBOX_DIR = os.environ.get("TTS_INBOX_DIR", "/tmp/tts_inbox")
PORT = int(os.environ.get("TTS_INBOX_PORT", "4398"))
MAX_BYTES = 2 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
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

    def log_message(self, *args):  # 静音，别把桥接日志刷乱
        return


def main() -> None:
    os.makedirs(INBOX_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"📥 TTS 收件服务已启动: 0.0.0.0:{PORT} → {INBOX_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
