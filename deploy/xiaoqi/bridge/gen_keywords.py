#!/usr/bin/env python3
"""为 open-xiaoai 的 KWS 模型生成 keywords.txt（中文→拼音→声母/带调韵母）。

用法（在桥接镜像里跑，镜像自带 pypinyin）：
  docker run --rm -v $PWD/deploy/xiaoqi/bridge/models:/m --entrypoint /app/.venv/bin/python \
    local/open-xiaoai-xiaozhi:barge-in /m/gen_keywords.py 你好小瑞 0.05

模型 tokens.txt 里既有无调韵母也有带调韵母，这里统一用带调（style=TONE），
与模型训练时的 ppinyin tokens 一致。
"""
from __future__ import annotations

import sys

from pypinyin import Style, pinyin
from pypinyin.contrib.tone_convert import to_finals_tone, to_initials

MODELS = "/m"


def load_tokens(path: str) -> set[str]:
    table = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) == 2:
                table.add(parts[0])
    return table


def encode(keyword: str, table: set[str]) -> list[str] | None:
    toks: list[str] = []
    for ch in keyword:
        py = pinyin(ch, style=Style.TONE)[0][0]
        initial = to_initials(py, strict=False)
        final = to_finals_tone(py, strict=False)
        if initial:
            toks.append(initial)
        if final:
            toks.append(final)
    missing = [t for t in toks if t not in table]
    if missing:
        print(f"⚠️  {keyword}: 词表里没有 {missing}", file=sys.stderr)
        return None
    return toks


def main() -> int:
    # 每个词可以单独带阈值： 你好小七:0.20,小七小七:0.30
    raw = sys.argv[1].split(",") if len(sys.argv) > 1 else ["你好小瑞"]
    default_threshold = sys.argv[2] if len(sys.argv) > 2 else "0.20"
    table = load_tokens(f"{MODELS}/tokens.txt")
    lines = []
    for item in raw:
        kw, _, own = item.partition(":")
        threshold = own or default_threshold
        toks = encode(kw, table)
        if toks:
            lines.append(" ".join(toks) + f" #{threshold} @{kw}")
    if not lines:
        print("❌ 没有生成任何唤醒词", file=sys.stderr)
        return 1
    with open(f"{MODELS}/keywords.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
