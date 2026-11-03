#!/usr/bin/env python3
"""声纹数据汇总（阶段 0 观察工具）。

用法（在桥接镜像里跑，镜像有 numpy + sherpa_onnx）：
  # 先把历史录音补算一遍（只在 speakers.jsonl 里没有的才补）
  docker exec xiaoai-bridge /app/.venv/bin/python /app/speaker_report.py --backfill

  # 看聚类情况
  docker exec xiaoai-bridge /app/.venv/bin/python /app/speaker_report.py

只读，不改任何业务逻辑。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import wave

import numpy as np

sys.path.insert(0, "/app")
LOG = os.environ.get("SPEAKER_LOG", "/data/logs/speakers.jsonl")
UTT = os.environ.get("UTTERANCE_DIR", "/data/logs/utterances")
DIALOG = os.environ.get("DIALOG_LOG", "/data/logs/dialog.jsonl")


def load_log() -> dict[str, dict]:
    out = {}
    if not os.path.exists(LOG):
        return out
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            out[rec["file"]] = rec
    return out


def texts() -> dict[str, str]:
    """wav 文件名 -> 这一轮识别出来的文字（用来人工核对谁在说话）。"""
    m: dict[str, str] = {}
    last = None
    if not os.path.exists(DIALOG):
        return m
    with open(DIALOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("role") == "audio":
                last = rec.get("file")
            elif rec.get("role") == "user" and last:
                m[last] = rec.get("text", "")
                last = None
    return m


def backfill(known: dict[str, dict]) -> int:
    from xiaozhi.speaker_id import record, SAMPLE_RATE

    added = 0
    for path in sorted(glob.glob(os.path.join(UTT, "*.wav"))):
        name = os.path.basename(path)
        if name in known:
            continue
        with wave.open(path) as w:
            pcm = w.readframes(w.getnframes())
            sec = w.getnframes() / w.getframerate()
        if sec < 0.5:
            continue
        record(name, pcm, sec)
        added += 1
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="把历史录音补算 embedding")
    ap.add_argument("--top", type=int, default=3, help="打印几个最大的簇")
    args = ap.parse_args()

    known = load_log()
    if args.backfill:
        n = backfill(known)
        print(f"投递 {n} 条补算，等待写盘…")
        import time

        for _ in range(120):
            time.sleep(1)
            now = load_log()
            if len(now) >= len(known) + n:
                break
        known = load_log()

    if not known:
        print("还没有数据。先跑一次 --backfill，或者对着小七说几句话。")
        return 0

    files = sorted(known, key=lambda k: known[k]["ts"])
    V = np.stack([np.asarray(known[f]["emb"], dtype=np.float32) for f in files])
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    secs = np.array([known[f]["sec"] for f in files])
    txt = texts()

    print(f"样本 {len(files)} 条，总语音 {secs.sum()/60:.1f} 分钟，时长中位 {np.median(secs):.1f}s")
    S = V @ V.T
    iu = np.triu_indices(len(files), 1)
    sims = S[iu]
    print("成对相似度: 均值 %.3f 中位 %.3f P90 %.3f 最大 %.3f"
          % (sims.mean(), np.median(sims), np.percentile(sims, 90), sims.max()))

    # 按阈值连边做簇，看家人能不能被分开
    for t in (0.45, 0.5, 0.55, 0.6, 0.65):
        parent = list(range(len(files)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for a in range(len(files)):
            for b in range(a + 1, len(files)):
                if S[a, b] >= t:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[ra] = rb
        from collections import Counter

        groups: dict[int, list[int]] = {}
        for i in range(len(files)):
            groups.setdefault(find(i), []).append(i)
        sizes = sorted((len(v) for v in groups.values()), reverse=True)
        print("  阈值 %.2f -> %d 簇，最大几个: %s" % (t, len(groups), sizes[:6]))

    # 最大簇里的样本（方便人工核对是不是同一个人）
    t = 0.55
    parent = list(range(len(files)))

    def find2(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a in range(len(files)):
        for b in range(a + 1, len(files)):
            if S[a, b] >= t:
                ra, rb = find2(a), find2(b)
                if ra != rb:
                    parent[ra] = rb
    groups: dict[int, list[int]] = {}
    for i in range(len(files)):
        groups.setdefault(find2(i), []).append(i)
    biggest = max(groups.values(), key=len)
    print(f"\n最大簇（阈值 {t}）{len(biggest)} 条，抽样 8 条：")
    for i in biggest[:8]:
        f = files[i]
        print("  %-28s %5.2fs  %s" % (f, secs[i], (txt.get(f) or "")[:26]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
