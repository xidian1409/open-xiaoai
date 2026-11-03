"""声纹数据的公共部分：embedding + 存储。

给 speaker_web.py（管理页面）和 speaker_id.py（线上采集）共用同一套模型参数，
避免两边算出不同的向量。
"""
from __future__ import annotations

import json
import os
import time
import uuid

import numpy as np

SAMPLE_RATE = 16000
MODEL_CANDIDATES = (
    "/app/xiaozhi/models/campplus.onnx",
    "/app/campplus.onnx",
)
DATA_DIR = os.environ.get("SPEAKER_DIR", "/data/speakers")
PEOPLE_FILE = os.path.join(DATA_DIR, "people.json")

# 固定测试句：覆盖不同韵母/声母组合，长度也拉开，方便横向比较
SENTENCES = [
    "今天天气不错，我们晚上出去吃饭吧",
    "小七，帮我看看明天会不会下雨",
    "把客厅的灯关掉，谢谢",
    "我需要一杯温水",
    "一二三四五六七八九十",
]

_ex = None
_failed = False


def extractor():
    global _ex, _failed
    if _ex is not None or _failed:
        return _ex
    path = next((p for p in MODEL_CANDIDATES if os.path.exists(p)), None)
    if not path:
        _failed = True
        return None
    try:
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=path, num_threads=1, debug=False, provider="cpu"
        )
        _ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ 声纹模型加载失败: {error}", flush=True)
        _failed = True
        _ex = None
    return _ex


def embed_pcm(pcm: np.ndarray) -> np.ndarray | None:
    """float32 [-1,1] 16k 单声道 -> 单位向量。"""
    ex = extractor()
    if ex is None or len(pcm) < SAMPLE_RATE // 2:
        return None
    stream = ex.create_stream()
    stream.accept_waveform(SAMPLE_RATE, pcm.astype(np.float32))
    stream.input_finished()
    v = np.asarray(ex.compute(stream), dtype=np.float32)
    return v / (float(np.linalg.norm(v)) + 1e-9)


def load_people() -> dict:
    if not os.path.exists(PEOPLE_FILE):
        return {}
    try:
        with open(PEOPLE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_people(people: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PEOPLE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(people, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PEOPLE_FILE)


def person_dir(pid: str) -> str:
    return os.path.join(DATA_DIR, pid)


def load_clips(pid: str) -> list[dict]:
    path = os.path.join(person_dir(pid), "clips.jsonl")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def add_clip(pid: str, wav_bytes: bytes, emb: np.ndarray, sentence: int, src: str) -> dict:
    d = person_dir(pid)
    os.makedirs(d, exist_ok=True)
    clip_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with open(os.path.join(d, clip_id + ".wav"), "wb") as f:
        f.write(wav_bytes)
    rec = {
        "id": clip_id,
        "ts": round(time.time(), 3),
        "sentence": sentence,
        "src": src,
        "emb": [round(float(x), 4) for x in emb],
    }
    with open(os.path.join(d, "clips.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def intra_similarity(clips: list[dict]) -> float | None:
    """同一个人各片段之间的平均相似度，用来判断登记质量。"""
    if len(clips) < 2:
        return None
    V = np.asarray([c["emb"] for c in clips], dtype=np.float32)
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    S = V @ V.T
    n = len(clips)
    return float((S.sum() - n) / (n * (n - 1)))
