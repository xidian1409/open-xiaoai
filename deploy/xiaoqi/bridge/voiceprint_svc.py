#!/usr/bin/env python3
"""声纹识别 HTTP 服务，给小智服务端用。

小智服务端自带声纹能力（core/utils/voiceprint_provider.py），它只认一个外部 HTTP 接口：
  GET  /voiceprint/health?key=KEY           -> {"status": "healthy"}
  POST /voiceprint/identify?key=KEY          multipart: speaker_ids, file(wav)
                                            -> {"speaker_id": "...", "score": 0.72}
识别成功后服务端会把 `当前说话人：X` 注入系统提示词（<speakers_info>），
大模型就能回答"我是谁"这类问题。

这里的识别用和采集端同一套：3D-Speaker CAM++ + 管理页登记的**设备录音**。
"""
from __future__ import annotations

import io
import json
import os
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

SAMPLE_RATE = 16000

PORT = int(os.environ.get("PORT", "8096"))
KEY = os.environ.get("VOICEPRINT_KEY", "xiaoqi")
SPEAKER_DIR = os.environ.get("SPEAKER_DIR", "/data/speakers")
MODEL = os.environ.get("VOICEPRINT_MODEL", "/app/xiaozhi/models/campplus.onnx")

_ex = None
_cache: dict = {"sig": None, "ids": [], "mat": None}


def extractor():
    global _ex
    if _ex is None:
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=MODEL, num_threads=1, debug=False, provider="cpu"
        )
        _ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        print(f"🎙️ 声纹模型已加载: {MODEL}", flush=True)
    return _ex


def references():
    """人员 id + 设备登记录音的向量矩阵（文件变了才重读）。"""
    try:
        with open(os.path.join(SPEAKER_DIR, "people.json"), encoding="utf-8") as f:
            people = json.load(f)
    except Exception:  # noqa: BLE001
        return [], None
    sig, ids, vecs = [], [], []
    for pid in people:
        path = os.path.join(SPEAKER_DIR, pid, "clips.jsonl")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        sig.append((pid, mtime, people[pid].get("name")))
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    c = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if c.get("src") != "device":
                    continue
                ids.append(pid)
                vecs.append(c["emb"])
    sig = tuple(sig)
    if _cache["sig"] == sig:
        return _cache["ids"], _cache["mat"]
    if not vecs:
        _cache.update(sig=sig, ids=[], mat=None)
        return [], None
    mat = np.asarray(vecs, dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    _cache.update(sig=sig, ids=ids, mat=mat)
    print(f"👥 声纹库已更新: {len(ids)} 段 / {len(set(ids))} 人", flush=True)
    return ids, mat


def wav_to_pcm(data: bytes) -> np.ndarray | None:
    try:
        with wave.open(io.BytesIO(data)) as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001
        return None
    if sw != 2:
        return None
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != SAMPLE_RATE:
        n = int(len(x) * SAMPLE_RATE / sr)
        x = np.interp(np.arange(n) * sr / SAMPLE_RATE, np.arange(len(x)), x).astype(np.float32)
    return x


def identify(wav: bytes) -> tuple[str, float]:
    pcm = wav_to_pcm(wav)
    if pcm is None or len(pcm) < SAMPLE_RATE // 2:
        return "", 0.0
    ids, mat = references()
    if mat is None:
        return "", 0.0
    ex = extractor()
    stream = ex.create_stream()
    stream.accept_waveform(SAMPLE_RATE, pcm)
    stream.input_finished()
    v = np.asarray(ex.compute(stream), dtype=np.float32)
    v /= float(np.linalg.norm(v)) + 1e-9
    scores = mat @ v
    i = int(np.argmax(scores))
    return ids[i], float(scores[i])


def parse_multipart(body: bytes, content_type: str) -> tuple[bytes | None, bytes | None]:
    """只取 speaker_ids 和 file 两个字段，够用就行。"""
    try:
        boundary = content_type.split("boundary=")[1].strip().strip('"')
    except IndexError:
        return None, None
    sep = b"--" + boundary.encode()
    ids = file_data = None
    for part in body.split(sep):
        if b'name="speaker_ids"' in part:
            at = part.find(b"\r\n\r\n")
            if at >= 0:
                ids = part[at + 4 :].strip(b"\r\n-")
        elif b'name="file"' in part:
            at = part.find(b"\r\n\r\n")
            if at >= 0:
                file_data = part[at + 4 :]
                # 去掉结尾的 \r\n
                if file_data.endswith(b"\r\n"):
                    file_data = file_data[:-2]
    return ids, file_data


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def _key_ok(self) -> bool:
        q = parse_qs(urlparse(self.path).query)
        if q.get("key", [""])[0] == KEY:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {KEY}"

    def do_GET(self):
        if not self.path.startswith("/voiceprint/health"):
            return self._json({"error": "not found"}, 404)
        if not self._key_ok():
            return self._json({"error": "unauthorized"}, 401)
        ids, mat = references()
        self._json({"status": "healthy", "speakers": len(set(ids)) if ids else 0})

    def do_POST(self):
        if not self.path.startswith("/voiceprint/identify"):
            return self._json({"error": "not found"}, 404)
        if not self._key_ok():
            return self._json({"error": "unauthorized"}, 401)
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        _, wav = parse_multipart(body, self.headers.get("Content-Type", ""))
        if not wav:
            return self._json({"speaker_id": "", "score": 0.0})
        try:
            pid, score = identify(wav)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 声纹识别失败: {exc}", flush=True)
            return self._json({"speaker_id": "", "score": 0.0})
        print(f"👤 声纹: {pid or '未识别'} {score:.3f} ({len(wav)}B)", flush=True)
        self._json({"speaker_id": pid, "score": round(score, 4)})


if __name__ == "__main__":
    os.makedirs(SPEAKER_DIR, exist_ok=True)
    print(f"🎙️ 声纹服务: http://0.0.0.0:{PORT}/voiceprint/identify?key={KEY}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
