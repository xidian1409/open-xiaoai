#!/usr/bin/env python3
"""离线评估唤醒词：用真实录音比较不同唤醒词 / 打分 / 阈值的检出率。

用法（在桥接镜像里跑）：
  docker cp deploy/xiaoqi/bridge/kws_eval.py xiaoai-bridge:/tmp/kwseval/kws_eval.py
  docker exec xiaoai-bridge /app/.venv/bin/python /tmp/kwseval/kws_eval.py
"""
from __future__ import annotations

import glob
import json
import os
import wave

import numpy as np
import sherpa_onnx
from pypinyin import Style, pinyin
from pypinyin.contrib.tone_convert import to_finals_tone, to_initials

M = "/app/xiaozhi/models"
EVAL = "/tmp/kwseval"
SR = 16000

KEYWORDS = ["小七小七", "你好小七", "小七"]
SCORES = [2.0, 3.0]
THRESHOLDS = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05]


def tokenize(kw: str) -> list[str]:
    toks: list[str] = []
    for ch in kw:
        py = pinyin(ch, style=Style.TONE)[0][0]
        ini = to_initials(py, strict=False)
        fin = to_finals_tone(py, strict=False)
        if ini:
            toks.append(ini)
        if fin:
            toks.append(fin)
    return toks


def read_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert w.getframerate() == SR, (path, w.getframerate())
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def spotter(line: str, score: float, threshold: float, trailing: int):
    kwf = os.path.join(EVAL, "kw_tmp.txt")
    with open(kwf, "w", encoding="utf-8") as f:
        f.write(line + "\n")
    return sherpa_onnx.KeywordSpotter(
        provider="cpu",
        num_threads=1,
        max_active_paths=8,
        keywords_score=score,
        keywords_threshold=threshold,
        num_trailing_blanks=trailing,
        keywords_file=kwf,
        tokens=f"{M}/tokens.txt",
        encoder=f"{M}/encoder.onnx",
        decoder=f"{M}/decoder.onnx",
        joiner=f"{M}/joiner.onnx",
    )


def run(kws, audio: np.ndarray) -> str | None:
    stream = kws.create_stream()
    stream.accept_waveform(SR, audio)
    while kws.is_ready(stream):
        kws.decode_streams([stream])
        r = kws.get_result(stream)
        if r:
            return r
    return None


def main() -> int:
    pos = sorted(glob.glob(f"{EVAL}/pos/*.wav"))
    neg = sorted(glob.glob(f"{EVAL}/neg/*.wav"))[:10]
    files = [(os.path.basename(p), p, True) for p in pos] + [
        (os.path.basename(p), p, False) for p in neg
    ]
    audio = {p: read_wav(p) for _, p, _ in files}

    out = {}
    for kw in KEYWORDS:
        toks = tokenize(kw)
        base = " ".join(toks)
        for score in SCORES:
            for thr in THRESHOLDS:
                line = f"{base} #{thr} @{kw}"
                kws = spotter(line, score, thr, 0)
                hits = []
                for name, path, is_pos in files:
                    if run(kws, audio[path]):
                        hits.append(name)
                key = f"{kw}|score={score}|thr={thr}"
                out[key] = hits
                pos_hits = sum(1 for n, _, isp in files if isp and n in hits)
                neg_hits = sum(1 for n, _, isp in files if not isp and n in hits)
                print(
                    f"{kw:6s} score={score} thr={thr:.2f}  "
                    f"pos {pos_hits}/{len(pos)}  neg {neg_hits}/{len(neg)}",
                    flush=True,
                )
    with open(f"{EVAL}/result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
