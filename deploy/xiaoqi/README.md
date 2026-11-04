# 小七 · 小爱音箱 Pro（LX06）语音助手

把小米 AI 音箱 Pro（LX06）改造成一个**唤醒/ASR/声纹全本地、只有大模型联网**的家庭语音助手：
叫「你好小七」就能对话，有灯环反馈、能被打断、能认出是谁在说话。

> 这是 [open-xiaoai](https://github.com/OwnDing/open-xiaoai) 的**定制层（overlay）**，不是上游代码的拷贝。
> 架构、参数、踩坑记录都在 [DESIGN.md](./DESIGN.md)；这里只讲怎么把它跑起来。

---

## 1. 思路：overlay，不动上游

上游 `examples/xiaozhi` 已经是一套能跑的桥接程序，但它的几乎所有行为都写死在代码里。
我们的做法是**只改需要改的文件**，再用 docker volume 把它们盖到镜像里的对应路径：

```yaml
volumes:
  - ./bridge/event.py:/app/xiaozhi/event.py:ro   # 用我们的版本覆盖镜像里的
```

好处是升级上游时只要重新 build 镜像，overlay 里没碰过的文件自动跟着上游走，
冲突面只有下面这张表里的十几个文件。

## 2. 目录结构

```
deploy/xiaoqi/
├── README.md               # 本文
├── DESIGN.md               # 设计文档（架构 / 参数 / 踩坑 / 迭代方向）
├── CHANGES.md              # 相对上游 open-xiaoai 改了什么
├── compose.yaml            # 5 个服务的完整编排
├── Dockerfile.bridge       # 桥接镜像（build context = 仓库根目录）
├── bridge/                 # 挂载进桥接镜像的定制文件
│   ├── *.py
│   ├── prompt.pcm          # 唤醒应答「我在」（24 kHz）
│   ├── farewell.pcm        # 退出语「主人再见，拜拜」（24 kHz）
│   └── models/             # keywords.txt / tokens.txt / campplus.onnx
├── device/                 # 抄到音箱上的脚本
│   ├── led.sh              # 灯环播放器（18 颗 LED）
│   ├── led_frames/*.txt    # 逐帧动画数据
│   ├── gen_led_frames.py   # 生成动画帧（本地跑）
│   └── stop_tts.sh
└── xiaozhi/                # 小智服务端（xiaozhi-esp32-server）的覆盖层
    ├── data/.config.example.yaml
    └── plugins/get_weather.py
```

## 3. 服务与端口

| 服务 | 端口 | 作用 |
|---|---|---|
| `xiaozhi-server` | 8000 / 8003 | ASR、LLM、会话管理（TTS 已换成「只发文字」） |
| `bridge` | 4399 / 4398 / 4400 | 音箱拨进来的 WebSocket + 音频链路 + 原生 TTS 调度 |
| `voiceprint` | 8096 | 声纹识别 HTTP 服务（CAM++） |
| `speaker-web` | 8097 / 8444 | 声纹管理页（8444 是 HTTPS，**网页录音必须走它**） |
| `dialog-web` | 8098 | 对话记录页（只读） |

## 4. 部署

### 4.1 构建桥接镜像

build context 必须是**仓库根目录**（`Dockerfile.bridge` 里要 `COPY examples/xiaozhi` 和 `COPY packages/client-rust`）：

```bash
docker build -f deploy/xiaoqi/Dockerfile.bridge -t local/open-xiaoai-xiaozhi:local .
```

### 4.1.1 补两个 Python 小依赖

镜像里没有 `pip`，而原生 TTS 链路要用 `soundfile`（解 mibrain 的 mp3）和 `soxr`
（16k→24k 重采样）。装到一个挂载目录里即可，不用重建镜像：

```bash
docker run --rm -v "$PWD/deploy/xiaoqi/bridge/pylibs:/out" \
  --entrypoint /usr/local/bin/pip local/open-xiaoai-xiaozhi:pcm-buf \
  install --no-cache-dir --no-deps --target /out \
  soundfile soxr cffi typing_extensions pycparser
```

（`pylibs/` 是二进制产物，没有入库，`.gitignore` 里排除了。）

> 镜像里 `CMD` 会先跑 `keywords.py` 生成唤醒词，这一步需要 `bpe.model`。
> 本定制层用 `gen_keywords.py` **预生成** `keywords.txt` 并挂载进去，同时在 `compose.yaml`
> 里把 `command` 改成直接 `python main.py`，绕开这个依赖。

### 4.2 准备模型

`bridge/models/` 里已经带了三个小文件（`keywords.txt`、`tokens.txt`、`campplus.onnx`），
还差 KWS 和 VAD 的 ONNX：

```bash
# 上游 release：VAD + KWS 模型
curl -L -o models.zip https://github.com/idootop/open-xiaoai/releases/tag/vad-kws-models
unzip models.zip -d /tmp/oxa-models

# 需要的 4 个文件
cp /tmp/oxa-models/*/encoder.onnx  deploy/xiaoqi/bridge/models/
cp /tmp/oxa-models/*/decoder.onnx  deploy/xiaoqi/bridge/models/
cp /tmp/oxa-models/*/joiner.onnx   deploy/xiaoqi/bridge/models/
cp /tmp/oxa-models/*/silero_vad.onnx deploy/xiaoqi/bridge/models/
```

> ⚠️ Silero 必须用 **v5**（2.3 MB）。v4 的模型在这个链路上概率恒为 0，
> 表现是「怎么说话都不结束」。详见 DESIGN.md 6.1。

### 4.3 服务端配置

```bash
cd deploy/xiaoqi
cp xiaozhi/data/.config.example.yaml xiaozhi/data/.config.yaml
vi xiaozhi/data/.config.yaml     # 填 DeepSeek api_key、默认城市、声纹人员
```

`.config.yaml` 里几处必须对上：

- `LLM.DeepSeekLLM.api_key` —— 自己的 Key
- `plugins.get_weather.default_location` —— 默认城市
- `voiceprint.url` 必须和 `voiceprint` 服务一致（默认 `http://xiaoqi-voiceprint:8096/voiceprint/identify?key=xiaoqi`）
- `voiceprint.speakers` 里的 `p_xxxxxxxx` 是**声纹管理页添加人员后生成的 id**，
  人员删了重建要回来改，否则服务端拿不到这个人的名字

> 文档里的 `<NAS_IP>` / `<音箱IP>` 是占位，代码里的 `192.168.1.100` 也是占位示例。
> 至少要改三处：`bridge/config.py` 的 `xiaozhi.OTA_URL` 与 `WEBSOCKET_URL`、
> `xiaozhi/data/.config.yaml` 的 `server.websocket`，否则音箱连不上桥接。

### 4.4 音箱端

音箱需要先按上游 README 刷好、跑起 `client-rust`。有两个坑：

- 采集格式必须是 **`-f S32_LE`**（`S16_LE` 采出来是 −81 dBFS 的静音）
- 桥接地址指向 NAS：`ws://<NAS_IP>:4399`

然后把灯环脚本抄上去：

```bash
scp -r device/led_frames device/led.sh device/stop_tts.sh root@<音箱IP>:/data/open-xiaoai/
ssh root@<音箱IP> 'chmod +x /data/open-xiaoai/*.sh'
```

### 4.5 启动

```bash
cd deploy/xiaoqi
docker compose up -d
docker compose logs -f bridge
```

看到 `🔎 KWS命中 '你好小七'` 就说明唤醒链路通了。

## 5. 改完什么要重启什么

这是最容易踩的坑：**挂载进去的 Python 文件不会热更新**。

| 改了什么 | 命令 |
|---|---|
| `bridge/*.py`（桥接补丁） | `docker compose up -d --force-recreate bridge` |
| `.config.yaml`（服务端配置） | `docker compose restart xiaozhi-server` |
| `speaker_web.py` / `dialog_web.py` / `voiceprint_svc.py` | `docker compose restart speaker-web voiceprint dialog-web` |
| `led_frames/*.txt` | 拷到音箱即可，下一次召唤醒自动生效 |

> 服务端启动时会**缓存声纹服务健康检查 10 分钟**，所以顺序上要先把 `voiceprint` 拉起来，
> 再重启 `xiaozhi-server`，否则会一直认为「声纹服务不可用」。

## 6. 常用操作

```bash
# 桥接日志（唤醒 / 打断 / 声纹 / 每轮耗时）
docker compose logs -f bridge

# 对话记录页（每轮文字 + 原始录音 + 说话人标签）
open http://<NAS_IP>:8098

# 声纹管理（加人、录测试音、试匹配）
open https://<NAS_IP>:8444        # 用手机录音时必须是 https

# 唤醒词离线回归（改 keywords.txt 或阈值后先跑这个）
docker compose exec bridge python /app/xiaozhi/kws_verify.py

# 声纹数据观察（聚类 / 回填历史录音）
docker compose exec bridge python /app/speaker_report.py
```

`prompt.pcm` / `farewell.pcm` 用**设备自己的 mibrain** 生成，和对话是同一个音色，
不会再出现"提示语一个声音、回答另一个声音"。必须转成 24 kHz 单声道 s16le
（设备播放通道是 24k，用 16k 会变速变调）：

```bash
# 1. 在音箱上合成（LX06 的 mibrain，0.1~0.3 秒）
ssh root@<音箱IP> 'ubus call mibrain text_to_speech "{\"text\":\"我在\",\"save\":1}"'
# 2. 把返回的 /tmp/tts/xxx.mp3 取回 NAS（ssh cat 即可，走 pty 会损坏二进制）
# 3. 转成 24k 单声道裸 PCM
ffmpeg -y -i prompt.mp3 -f s16le -acodec pcm_s16le -ac 1 -ar 24000 bridge/prompt.pcm
```

## 7. 已知限制

- 桥接只取 `ch0`（一路麦克风）+ `ch6`（AEC 参考），6 麦阵列的其余 5 路没用上 ——
  实测远场 SNR 4.8 dB，做多路平均也没有收益，反而把设备内存打爆过。详见 DESIGN.md 2.3 / 6.7。
- 声纹**必须用音箱自己录的片段**登记。手机上传的音频域差太大，相似度中位数只有 0.237，
  和「不认识的人」完全区分不开。
- 原生音色模式下打断走 `mediaplayer player_play_operation stop`，比 PCM 流更干脆；
  首句优先（`XIAOQI_FIRST_SENTENCE_PLAY=1`）会让段之间留约 0.3 秒衔接缝，关掉可回到无缝的整段播放。
- 音箱只有 118 MB 可用内存、5–8 MB 空闲，**不要在设备上加实时多路 DSP**。

## 8. 许可

上游是 MIT / Apache-2.0（见仓库根目录 LICENSE 与 agreement.md），本定制层沿用相同许可。
