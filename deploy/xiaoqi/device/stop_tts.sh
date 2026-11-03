#!/bin/sh
# 立刻停掉正在播的唤醒提示语。
# 提示语走设备本地 TTS（tts_play.sh -> mibrain -> miplayer），
# 和对话回复（服务端 PCM 流）是两条通道，所以这里只杀本地的播放进程。
pkill -f "tts_play.sh" 2>/dev/null
pkill -x "miplayer" 2>/dev/null
exit 0
