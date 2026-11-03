#!/usr/bin/env python3
"""生成 LX06 灯环（18 颗）的动画帧，供设备端 led.sh 逐帧播放。

每帧 18 行 "<索引> 0xBBGGRR"，帧之间放一行 "SLEEP <秒>"。
颜色在这里算好（gradient / 扫光 / 波纹），设备上只做 read + write，省 CPU。

配色原则：每个状态一个明显不同的色系 + 明显不同的运动方式，扫一眼就能分辨，
不要都挤在黄绿色上。
    wake        白→金  从 12 点向外扩散，最后白闪一下（一次性）
    listen      青白  一个亮头绕圈扫描，底光很暗          （循环）
    listen_low  橙色  两短一停的心跳，越等越暗            （循环）
    think       紫色  两个光点反向对流，转得快            （循环）
    speak       蓝色  整环呼吸，没有方向感                （循环）
    busy        白色  单点亮珠跑马                      （循环）
    error       红色  闪两下                        （一次性）
"""
from __future__ import annotations

import math
import os

N = 18
OUT = "deploy/xiaoqi/device/led_frames"


def clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def hexcolor(r: float, g: float, b: float) -> str:
    """驱动要 0xBBGGRR（R + G<<8 + B<<16）。"""
    return f"0x{clamp(b):02X}{clamp(g):02X}{clamp(r):02X}"


def write(name: str, frames: list[list[str]], sleep: float, loop_note: str = "") -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {loop_note}\n")
        for frame in frames:
            for i, c in enumerate(frame):
                f.write(f"{i} {c}\n")
            f.write(f"SLEEP {sleep}\n")
    print(f"{name}: {len(frames)} 帧 -> {path}")


def blend(c1, c2, t):
    return tuple(a + (b - a) * t for a, b in zip(c1, c2))


def ring_distance(i: int, center: float) -> float:
    """环上第 i 颗到 center 位置的最短角距（弧度）。"""
    ang = i / N * 2 * math.pi
    return abs((ang - center + math.pi) % (2 * math.pi) - math.pi)


# ---------------------------------------------------------------- 唤醒
def wake_frames() -> list[list[str]]:
    """金色从 12 点向两侧铺开，最后一记白闪：我听见了。"""
    frames = []
    gold = (255, 190, 60)
    white = (255, 255, 255)
    for step in range(10):
        f = []
        for i in range(N):
            d = min((i - 9) % N, (9 - i) % N)
            lit = 1.0 if d <= step else 0.0
            fall = max(0.0, 1.0 - (d - step) * 0.4) if d > step else 1.0
            f.append(hexcolor(*(c * lit * fall for c in gold)))
        frames.append(f)
    frames.append([hexcolor(*white)] * N)
    frames.append([hexcolor(*(c * 0.35 for c in white))] * N)
    frames.append([hexcolor(0, 0, 0)] * N)
    return frames


# ---------------------------------------------------------------- 在听
def listen_frames() -> list[list[str]]:
    """青白扫描：暗冷底 + 一个亮头带长尾绕圈。等待感最强，且不显绿。"""
    frames = []
    base = (8, 26, 40)        # 极暗冷底，只留一点点存在感
    head = (130, 240, 255)    # 青白亮头
    steps = 36
    for k in range(steps):
        phase = k / steps * 2 * math.pi
        f = []
        for i in range(N):
            d = ring_distance(i, phase)
            glow = max(0.0, 1.0 - d / 0.95) ** 1.7
            col = blend(base, head, glow)
            f.append(hexcolor(*col))
        frames.append(f)
    return frames


# ---------------------------------------------------------------- 快超时
def listen_low_frames() -> list[list[str]]:
    """橙色心跳：两短一停，亮度也低——一眼看出她快不听了。"""
    frames = []
    orange = (255, 95, 0)
    for level in (1.0, 0.45, 1.0, 0.15, 0.0, 0.0):
        frames.append([hexcolor(*(c * level * 0.6 for c in orange))] * N)
    return frames


# ---------------------------------------------------------------- 在想
def think_frames() -> list[list[str]]:
    """紫色对流：两个亮头反向绕圈，节奏比在听快一倍——正在盘算。"""
    frames = []
    violet = (150, 55, 255)
    hot = (235, 205, 255)
    steps = 26
    for k in range(steps):
        phase = -k / steps * 2 * math.pi
        f = []
        for i in range(N):
            d1 = ring_distance(i, phase)
            d2 = ring_distance(i, phase + math.pi)
            head = max(max(0.0, 1.0 - d1 / 0.45), max(0.0, 1.0 - d2 / 0.45)) ** 1.25
            tail = max(max(0.0, 1.0 - d1 / 1.3), max(0.0, 1.0 - d2 / 1.3)) ** 3
            col = blend(violet, hot, head)
            v = head + tail * 0.28
            f.append(hexcolor(*(c * v for c in col)))
        frames.append(f)
    return frames


# ---------------------------------------------------------------- 在说
def speak_frames() -> list[list[str]]:
    """蓝色整体呼吸：没有方向、没有亮点跳动，安静不抢注意力。"""
    frames = []
    blue = (35, 100, 255)
    steps = 30
    for k in range(steps):
        breath = 0.22 + 0.78 * (0.5 + 0.5 * math.sin(k / steps * 2 * math.pi - math.pi / 2))
        f = []
        for i in range(N):
            ripple = 0.045 * math.sin(i / N * 2 * math.pi * 2 - k / steps * 2 * math.pi)
            v = max(0.0, breath + ripple)
            f.append(hexcolor(*(c * v for c in blue)))
        frames.append(f)
    return frames


# ---------------------------------------------------------------- 其它
def error_frames() -> list[list[str]]:
    red = (255, 25, 25)
    dim = (90, 0, 0)
    return [[hexcolor(*red)] * N, [hexcolor(*dim)] * N] * 2 + [[hexcolor(0, 0, 0)] * N]


def busy_frames() -> list[list[str]]:
    """连接中：白色单点亮珠跑马（和快超时的橙色心跳区分开）。"""
    frames = []
    white = (235, 240, 255)
    for k in range(N):
        f = []
        for i in range(N):
            on = 1.0 if i == k else 0.06
            f.append(hexcolor(*(c * on for c in white)))
        frames.append(f)
    return frames


if __name__ == "__main__":
    write("wake", wake_frames(), 0.05, "唤醒：金色波浪铺开 + 白闪")
    write("listen", listen_frames(), 0.07, "在听：青白扫描头绕圈（循环）")
    write("listen_low", listen_low_frames(), 0.12, "快超时：橙色两短一停心跳（循环）")
    write("think", think_frames(), 0.045, "在想：紫色双头反向绕圈（循环）")
    write("speak", speak_frames(), 0.10, "在说：蓝色整体呼吸（循环）")
    write("busy", busy_frames(), 0.07, "连接中：白色跑马（循环）")
    write("error", error_frames(), 0.18, "出错：红色闪两下")
