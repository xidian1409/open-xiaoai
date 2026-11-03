"""声纹 embedding 采集（阶段 0：只记录，不做任何判断）。

设计原则
- **绝不参与决策**：只把每句话的 192 维 embedding 追加到日志里，
  认人、阈值、权限这些以后再谈。
- **不挡音频链路**：单独一个工作线程 + 队列，算完就写盘，主线程立刻返回。
- **模型可缺席**：模型文件不在、onnxruntime 起不来，都只是记一条警告，
  对话照常进行。

模型：3D-Speaker CAM++（sherpa-onnx，zh-cn 通用，16k 输入，192 维输出）
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time

import numpy as np

LOG_PATH = os.environ.get("SPEAKER_LOG", "/data/logs/speakers.jsonl")
# 管理页面写的"下一句归给谁"的请求；桥接这边收到就把它当成登记音频
SPEAKER_DIR = os.environ.get("SPEAKER_DIR", "/data/speakers")
PENDING_PATH = os.path.join(SPEAKER_DIR, "pending.json")
DIALOG_LOG = os.environ.get("DIALOG_LOG", "/data/logs/dialog.jsonl")

# 认人的门槛：设备登记实测真人语音 >=0.50、杂音 <=0.38，取 0.45 居中。
try:
    from config import APP_CONFIG as _CFG

    _SPK = _CFG.get("speaker", {}) or {}
except Exception:  # noqa: BLE001
    _SPK = {}
SPEAKER_ENABLED = bool(_SPK.get("enabled", True))
SPEAKER_THRESHOLD = float(_SPK.get("threshold", 0.45))
SPEAKER_MIN_SECONDS = float(_SPK.get("min_seconds", 1.0))
# 同一个人刚刚被认出来过，紧接着的这一句可以放宽一点门槛（"连续对话"场景）。
SPEAKER_CARRY = float(_SPK.get("carry_threshold", 0.38))
SPEAKER_CARRY_WINDOW = float(_SPK.get("carry_window_s", 120))
_last_hit: dict = {"name": "", "ts": 0.0}

_refs_cache: dict = {"sig": None, "names": [], "mat": None}


def _references():
    """读所有已登记的人的设备录音，拼成一个矩阵（文件变了才重读）。"""
    import glob

    try:
        with open(os.path.join(SPEAKER_DIR, "people.json"), encoding="utf-8") as f:
            people = json.load(f)
    except Exception:  # noqa: BLE001
        return [], None
    sig = []
    names, vecs = [], []
    for pid, info in people.items():
        path = os.path.join(SPEAKER_DIR, pid, "clips.jsonl")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        sig.append((pid, mtime))
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
                names.append(info.get("name", pid))
                vecs.append(c["emb"])
    sig = tuple(sig)
    if _refs_cache["sig"] == sig:
        return _refs_cache["names"], _refs_cache["mat"]
    if not vecs:
        _refs_cache.update(sig=sig, names=[], mat=None)
        return [], None
    mat = np.asarray(vecs, dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    _refs_cache.update(sig=sig, names=names, mat=mat)
    return names, mat


def identify(vec: np.ndarray, seconds: float) -> tuple[str, float] | None:
    """返回 (名字, 分数)；不够可信就返回 None。"""
    if not SPEAKER_ENABLED or seconds < SPEAKER_MIN_SECONDS:
        return None
    names, mat = _references()
    if mat is None:
        return None
    scores = mat @ vec
    i = int(np.argmax(scores))
    score = float(scores[i])
    name = names[i]

    if score >= SPEAKER_THRESHOLD:
        _last_hit.update(name=name, ts=time.time())
        return name, score
    # 差一点点，但刚才同一轮对话里已经确认是他：继续认他
    fresh = time.time() - _last_hit["ts"] < SPEAKER_CARRY_WINDOW
    if fresh and name == _last_hit["name"] and score >= SPEAKER_CARRY:
        return name, score
    return None


def _log_speaker(name: str, who: tuple[str, float] | None) -> None:
    rec = {"ts": _record_ts(name), "role": "speaker", "file": name}
    if who:
        rec["name"], rec["score"] = who[0], round(who[1], 3)
    else:
        rec["name"] = ""
    try:
        with open(DIALOG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
MODEL_CANDIDATES = (
    "/app/xiaozhi/models/campplus.onnx",
    "/app/campplus.onnx",
)
SAMPLE_RATE = 16000
MIN_SECONDS = 0.5

_queue: "queue.Queue[tuple[str, float, bytes]]" = queue.Queue(maxsize=32)
_worker: threading.Thread | None = None
_extractor = None
_failed = False
_lock = threading.Lock()


def _load():
    """首次调用时加载模型；失败就永久关闭，不再重试。"""
    global _extractor, _failed
    with _lock:
        if _extractor is not None or _failed:
            return _extractor
        path = next((p for p in MODEL_CANDIDATES if os.path.exists(p)), None)
        if not path:
            print("⚠️ 声纹模型缺失，跳过 embedding", flush=True)
            _failed = True
            return None
        try:
            import sherpa_onnx

            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=path, num_threads=1, debug=False, provider="cpu"
            )
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            print(f"🎙️ 声纹模型已加载: {path}", flush=True)
        except Exception as error:  # noqa: BLE001 - 声纹坏了不能影响对话
            print(f"⚠️ 声纹模型加载失败: {error}", flush=True)
            _failed = True
            _extractor = None
        return _extractor


def embed(pcm: bytes) -> np.ndarray | None:
    """16k 单声道 s16le PCM -> 192 维单位向量。"""
    ex = _load()
    if ex is None or len(pcm) < SAMPLE_RATE * 2 * MIN_SECONDS:
        return None
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    try:
        stream = ex.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        v = np.asarray(ex.compute(stream), dtype=np.float32)
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ 声纹计算失败: {error}", flush=True)
        return None
    norm = float(np.linalg.norm(v)) + 1e-9
    return v / norm


def _wait_for_model() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _load() is not None or _failed:
            return
        time.sleep(0.5)


def _maybe_capture(name: str, vec: np.ndarray, pcm: bytes, seconds: float) -> None:
    """管理页面点了"对着小七说"：把这一句直接收进那个人的登记里。"""
    try:
        if not os.path.exists(PENDING_PATH):
            return
        with open(PENDING_PATH, encoding="utf-8") as f:
            req = json.load(f) or {}
        if not req or float(req.get("expire", 0)) < time.time():
            return
        pid = req.get("person")
        if not pid:
            return
        import wave

        d = os.path.join(SPEAKER_DIR, pid)
        os.makedirs(d, exist_ok=True)
        clip_id = name[:-4] if name.endswith(".wav") else name
        with wave.open(os.path.join(d, clip_id + ".wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        rec = {
            "id": clip_id,
            "ts": round(time.time(), 3),
            "sentence": int(req.get("sentence", -1)),
            "src": "device",
            "emb": [round(float(x), 4) for x in vec],
        }
        with open(os.path.join(d, "clips.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)
        print(f"🎙️ 声纹收录: {pid} 第{rec['sentence'] + 1}句 {seconds:.1f}s", flush=True)
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ 声纹收录失败: {error}", flush=True)


def _loop() -> None:
    # 工作线程启动时先把模型加载好，避免第一句话赶上冷启动。
    _wait_for_model()
    while True:
        name, seconds, pcm = _queue.get()
        try:
            vec = embed(pcm)
            if vec is None:
                continue
            _maybe_capture(name, vec, pcm, seconds)
            _append(name, seconds, vec)
            who = identify(vec, seconds)
            _log_speaker(name, who)
            if who:
                print(f"👤 认出来了: {who[0]} ({who[1]:.2f})", flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"⚠️ 声纹写盘失败: {error}", flush=True)
        finally:
            _queue.task_done()


def _record_ts(name: str) -> float:
    """从文件名 20261103-090955-230.wav 解析出真正的录音时间。

    之前用的是"算完 embedding 的时间"，补算历史录音时全变成今天，
    没法按日期做分析。
    """
    import datetime

    base = name[:-4] if name.endswith(".wav") else name
    try:
        t = datetime.datetime.strptime(base[:15], "%Y%m%d-%H%M%S")
        ms = int(base.split("-")[-1]) if base.count("-") == 2 else 0
        return t.timestamp() + ms / 1000.0
    except Exception:  # noqa: BLE001
        return time.time()


def _append(name: str, seconds: float, vec: np.ndarray) -> None:
    rec = {
        "ts": round(_record_ts(name), 3),
        "file": name,
        "sec": round(seconds, 2),
        # 四位小数够用了（余弦相似度有效位数远小于此），一行约 1.3 KB
        "emb": [round(float(x), 4) for x in vec],
    }
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def record(name: str, pcm: bytes, seconds: float | None = None) -> None:
    """投递一句话去算 embedding（不阻塞调用方）。"""
    if _failed or not pcm:
        return
    if seconds is None:
        seconds = len(pcm) / 2 / SAMPLE_RATE
    if seconds < MIN_SECONDS:
        return
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, name="speaker-id", daemon=True)
        _worker.start()
    try:
        # 线上是一句一句来，队列不会满；补算历史时允许短暂等一下，
        # 免得一次性投递几十条被丢掉（_queue.put_nowait 会直接丢）。
        _queue.put((name, seconds, pcm), timeout=0.5)
    except queue.Full:
        pass
