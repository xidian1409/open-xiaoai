#!/bin/sh
# 小七的灯环播放器（LX06：AW20054，18 颗灯，写 "<编号> 0xBBGGRR"）
#
# 用法：led.sh off|wake|listen|think|speak|error|busy
# 帧动画在 /data/open-xiaoai/led_frames/<mode>.txt 里（由 gen_led_frames.py 生成），
# 这里只负责逐帧读出来写进设备节点，循环动画一直播到下一次调用为止。
# 每次都把上一段动画 kill 掉，再把新动画丢到后台立即返回
# （run_shell 是同步 RPC，不能让调用方等动画跑完）。

LED=/sys/devices/i2c-0/0-003a/led_rgb
DIR=/data/open-xiaoai/led_frames
PIDF=/tmp/led.pid
N=18

# 杀掉所有旧的播放进程（pid 文件可能被后一次调用覆盖，只靠它会出现孤儿动画，
# 表现就是"待机了灯还亮着"）
for p in $(ps | grep 'led\.sh' | grep -v grep | awk '{print $1}'); do
  [ "$p" = "$$" ] && continue
  [ "$p" = "$PPID" ] && continue
  kill "$p" 2>/dev/null
done
rm -f "$PIDF"
sleep 0.05

all_off() {
  i=0
  while [ $i -lt $N ]; do
    echo "$i 0x000000" > "$LED"
    i=$((i + 1))
  done
}

case "$1" in
  off)
    all_off
    exit 0
    ;;
  wake|listen|listen_low|think|speak|error|busy) : ;;
  *)
    echo "usage: led.sh off|wake|listen|listen_low|think|speak|error|busy" >&2
    exit 2
    ;;
esac

F="$DIR/$1.txt"
[ -f "$F" ] || { all_off; exit 0; }

case "$1" in
  wake|error) ONESHOT=1 ;;
  *) ONESHOT=0 ;;
esac

(
  while :; do
    while IFS=' ' read -r idx val; do
      case "$idx" in
        SLEEP) sleep "$val" ;;
        '#'*) : ;;
        '') : ;;
        *) [ -n "$val" ] && echo "$idx $val" > "$LED" ;;
      esac
    done < "$F"
    [ "$ONESHOT" = "1" ] && break
  done
  [ "$ONESHOT" = "1" ] && all_off
) &

echo $! > "$PIDF"
exit 0
