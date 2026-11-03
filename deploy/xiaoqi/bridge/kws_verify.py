#!/usr/bin/env python3
"""验证最终唤醒词配置：4 个真实正样本 + 全部真实录音当负样本。

对比点：
  * 逐行 `:3.0` 是否能替代全局 keywords_score
  * 阈值 0.15 / 0.20 / 0.25 的召回与误唤
  * 双流错开 [0,160] 的增益
  * 同时挂 你好小七 + 小七小七 会不会互相干扰
"""
from __future__ import annotations

import glob
import os
import wave

import numpy as np
import sherpa_onnx
from pypinyin import Style, pinyin
from pypinyin.contrib.tone_convert import to_finals_tone, to_initials

M = "/app/xiaozhi/models"
EVAL = "/tmp/kwseval"
SR = 16000


def tokenize(kw: str) -> str:
    toks: list[str] = []
    for ch in kw:
        py = pinyin(ch, style=Style.TONE)[0][0]
        ini = to_initials(py, strict=False)
        fin = to_finals_tone(py, strict=False)
        if ini:
            toks.append(ini)
        if fin:
            toks.append(fin)
    return " ".join(toks)


def read_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def build(lines: list[str], score: float, thr: float, trailing: int):
    kwf = os.path.join(EVAL, "kw_final.txt")
    with open(kwf, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return sherpa_onnx.KeywordSpotter(
        provider="cpu",
        num_threads=1,
        max_active_paths=8,
        keywords_score=score,
        keywords_threshold=thr,
        num_trailing_blanks=trailing,
        keywords_file=kwf,
        tokens=f"{M}/tokens.txt",
        encoder=f"{M}/encoder.onnx",
        decoder=f"{M}/decoder.onnx",
        joiner=f"{M}/joiner.onnx",
    )


def hit(kws, audio: np.ndarray, skip: int = 0) -> bool:
    stream = kws.create_stream()
    if skip:
        stream.accept_waveform(SR, np.zeros(skip, dtype=np.float32))
    stream.accept_waveform(SR, audio)
    while kws.is_ready(stream):
        kws.decode_streams([stream])
        if kws.get_result(stream):
            return True
    return False


def main() -> int:
    pos = sorted(glob.glob(f"{EVAL}/pos/*.wav"))
    neg = sorted(glob.glob(f"{EVAL}/neg/*.wav"))
    PO = [read_wav(p) for p in pos]
    NE = [(os.path.basename(p), read_wav(p)) for p in neg]

    t_nh = tokenize("你好小七")
    t_xq = tokenize("小七小七")

    configs = {
        "A_单行:3.0_thr0.20": (["%s #0.20 @你好小七" % t_nh], 2.0, 0.20, 0),
        "B_全局3.0_thr0.20": (["%s @你好小七" % t_nh], 3.0, 0.20, 0),
        "C_单行:3.0_thr0.25": (["%s #0.25 @你好小七" % t_nh], 2.0, 0.25, 0),
        "D_单行:3.0_thr0.15": (["%s #0.15 @你好小七" % t_nh], 2.0, 0.15, 0),
        "E_双词_小七小七#0.30": (
            ["%s #0.20 @你好小七" % t_nh, "%s #0.30 @小七小七" % t_xq],
            2.0,
            0.20,
            0,
        ),
    }

    for name, (lines, score, thr, trailing) in configs.items():
        kws = build(lines, score, thr, trailing)
        p = sum(1 for a in PO if hit(kws, a))
        # 双流错开 160ms 的增益
        p2 = sum(1 for a in PO if hit(kws, a) or hit(kws, a, skip=SR * 160 // 1000))
        false = [n for n, a in NE if hit(kws, a)]
        print(
            f"{name:24s} pos {p}/4 (+错开 {p2}/4)  neg {len(false)}/{len(NE)}",
            flush=True,
        )
        if false:
            print(f"    误唤: {false[:6]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
