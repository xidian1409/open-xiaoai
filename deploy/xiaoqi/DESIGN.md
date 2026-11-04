# 小七家庭语音助手 · 设计文档

> 最后一次大改：2026-11-04（音频输出链路重做：原生音色 + 并行合成 + 整段播放）
> 本文档记录**当前实现状态**、**为什么这么做**、**踩过的坑**和**实测数字**，
> 方便后续迭代时不用重新踩一遍。
>
> 机器：小米 AI 音箱 Pro（LX06）· NAS 上跑 5 个容器

---

## 1. 一句话概览

把 LX06 改成一个**唤醒、VAD、ASR、声纹全部本地**的家庭语音助手：叫「你好小七」就能对话，
有灯环反馈、能被打断、能认出是谁在说话、说话用的是小爱原生音色。只有大模型在联网。

---

## 2. 系统架构

### 2.1 组件

| 容器 | 镜像 | 端口 | 作用 |
|---|---|---|---|
| `xiaoai-bridge` | `local/open-xiaoai-xiaozhi:pcm-buf` | 4399 / 4398 | 接音箱、唤醒、VAD、AEC、音频转发、原生 TTS 合成调度 |
| `xiaozhi-server` | `xinnan-tech/xiaozhi-esp32-server:server_latest` | 8000(ws) / 8003(OTA) | ASR、LLM、会话状态机（TTS 已换成"只发文字"） |
| `xiaoqi-dialog` | 同 bridge | 8098 | 对话记录页（只读） |
| `xiaoqi-speaker` | 同 bridge | 8097 / 8444(https) | 声纹管理页 |
| `xiaoqi-voiceprint` | 同 bridge | 8096 | 声纹识别 HTTP 服务（给小智服务端用） |

> `pcm-buf` 这个 tag 是因为设备侧 aplay 缓冲从 200 ms 加深到了 600 ms（见 3.9）。

### 2.2 数据流

```
LX06 6 麦 + 2 路回采（8ch / 48k / S32）
   │  客户端只取 ch0(麦) + ch6(回采)，降到 16k，发立体声 [mic, ref]
   ▼
桥接 AEC（SubbandEchoCanceller，消 ~27 dB）──────► 16k 单声道
   │
   ├─► KWS（sherpa-onnx zipformer，关键词「你好小七」）
   ├─► VAD（Silero v5）
   └─► 整句 PCM ──┬─► 送 xiaozhi-server（ASR → LLM）
                  └─► 本机算声纹 embedding（192 维，~110 ms）
                        ├─► 写 speakers.jsonl（观察用）
                        └─► 写 dialog.jsonl 的 speaker 记录（页面标签）

xiaozhi-server：
   ├─► SenseVoice 识别出文字
   ├─► 调 http://xiaoqi-voiceprint:8096/voiceprint/identify 认人 → 注入 <speakers_info>
   └─► DeepSeek 生成回答 → **只下发文字**（TextOnly TTS provider，不合成音频）
          │
          ▼
桥接原生 TTS 链路（见 2.4）：mibrain 并行合成 → 拼接 → 常驻播放器播放
```

### 2.3 为什么音频链路是"一路麦克风"

设备能采 8 声道（6 麦 + 2 路重复回采），但我们只取 ch0（主麦）和 ch6（回采参考）：

- 实测 6 路麦平均**没有 SNR 收益**（噪声相关性 0.40），远场语音 SNR 只有 4.8 dB
- 做多路实时处理会把 118 MB 内存的设备压垮（曾 OOM 杀掉小爱引擎 29 次）

### 2.4 语音链路全流程（当前实现）

这是整条链路，从用户开口到声音出来：

```
① 用户说话
② 设备采集 8ch/48k → 只取 ch0+ch6 → 16k 立体声 → 桥接
③ 桥接 AEC 消回声 → KWS 监听 / VAD 判停
④ 说完一句 → 整句 PCM 送 xiaozhi-server
⑤ 服务端 SenseVoice 识别（本地）→ 声纹认人（本地 HTTP）→ DeepSeek 生成
⑥ 服务端按标点切句，每句发一条 sentence_start(文字) —— TextOnly provider 不做合成
⑦ 桥接收到一句文字 → 立刻起一个 mibrain 合成任务（设备本地，并发上限 4）
⑧ 桥接同时在设备上拉起一个"水槽"：常驻 aplay + 命名管道（FIFO），回答开始时
   就绪、整个回答只开一次声卡
⑨ 第一句合成完 → 桥接把 mp3 路径排进队（只做一个 ln 符号链接），设备上的喂料脚本
   `curl` 取回 PCM 直接写进管道
⑩ 后续句子合成一段喂一段，aplay 连续吐给 ALSA —— 句间没有"重开播放器"的动作
⑪ 桥接排完最后一段落 stop → aplay 排空缓冲退出 → 回到聆听状态（连续对话 5 秒）
```

**关键设计**：一条只开一次声卡的连续音频流。mp3 在设备上合成、在桥接侧解码成
16 kHz PCM（顺带把每段自带的前后静音收拢），PCM 走局域网 HTTP 回设备写进管道。
播放期间设备不做任何网络实时性依赖，也不需要每句重启播放器。

### 2.5 各阶段耗时（实测）

环境：LX06（4 核 / 118 MB 可用 / 播音频时 load 6+）+ NAS（Ubuntu 系，x86_64）

| 阶段 | 实测耗时 | 说明 |
|---|---|---|
| 唤醒词检测 | < 100 ms | 桥接侧 KWS，流式 |
| 提示语「我在」 | 0.54 s 音频 | 本地 mibrain 生成，走 PCM 通道 |
| VAD 判停 | 0.3~0.6 s | Silero v5，可调 |
| ASR（SenseVoice，本地） | 0.2~0.4 s | 整句识别 |
| 声纹识别 | 0.09~0.15 s | 并行执行，不占主链路 |
| **大模型首字（TTFT）** | **0.83~1.14 s** | DeepSeek，最大的一块 |
| 大模型出完整回答 | +0.3~1.5 s | 取决于句子数，逐句流式 |
| 单句合成（mibrain） | 0.08~0.65 s | 设备本地；设备忙时可到 1.2 s |
| 解码 + 静音修剪（桥接） | 20~30 ms/段 | soundfile + numpy |
| 取回 PCM（设备 curl，局域网） | 60~360 ms/段 | 设备忙时偏慢，但都在"边播边取"里 |
| 起播（常驻 aplay，已预开） | ~0.2 s | `-B 200000` 后约 0.1~0.2 s |
| **首字延迟（说完→出声）** | **0.9~1.6 s（中位 ~1.2 s）** | 见下方说明 |
| **段间衔接缝** | **0 ms** | 单一连续 PCM 流，实测波形连续 |
| 句间停顿（音频自带） | ~0.17 s | 修剪前是 ~0.65 s（mibrain 每段尾部 ~0.56 s 静音） |

**首字延迟的构成**（典型一轮）：

```
大模型首字 0.9s  +  首句合成 0.2s  +  起播 0.15s  ≈ 1.25s
```

带工具调用的轮次会明显更慢（实测调天气工具那轮 3.10 s，多出来的全是 `wttr.in` 的
网络往返）。

**不同做法的对比**（同一批问题，实测）：

| 做法 | 首字 | 段间 | 结论 |
|---|---|---|---|
| EdgeTTS 流式（原始方案） | ~3.2 s | 无 | 慢在每句 1.05~1.19 s 的云端合成，且逐句串行 |
| 原生 TTS + 现状服务端 | ~3.9 s | 0.4 s/段 | 最差：在 EdgeTTS 等待上又加了合成和起播 |
| 原生 TTS + 只发文字 + PCM 实时推流（旧） | ~2.4 s | 断流 | 数据塞进 WebSocket 控制通道，反复 `send stalled` |
| 原生 TTS + 只发文字 + 整段文件播放 | ~1.2 s | ~0.3 s | 稳定；缝来自"重开播放器"，实测下限就有 0.13 s |
| **原生 TTS + 只发文字 + 常驻 aplay 连续流**（现行） | **~1.2 s** | **0** | 稳定；3 句连播实测 0 underrun |

**为什么"每句一个文件"注定有缝**：设备上量化过，同一个播放器播第二个文件时，
从上一段被切断到新文件出声固定有 **~130 ms** 静音（与 mp3/wav、`type`、`group`
都无关，是播放器自身的启停代价）；再叠上 `sleep(时长 + 0.2 s)` 就是实测的 0.33 s。
想压到 0.1 s 以下，只能不重开播放器 —— 也就是现在的连续流。

---

## 3. 功能模块现状

### 3.1 唤醒词

- 关键词：`你好小七`、`小七小七`（叠词召回差，主力用前者）
- `keywords_score: 3.0`、`keywords_threshold: 0.20`、`max_active_paths: 8`
- 打分/阈值改成读 `config.py`（镜像里写死 2.0，唤不醒）
- 回归脚本：`bridge/kws_verify.py`（改参数后先跑这个）

### 3.2 唤醒提示语与告别语

- 「我在」**0.54 秒**、「主人再见，拜拜」**1.51 秒**，24 kHz 单声道 s16le
- 必须 24k：设备播放通道是 24k，用 16k 会变速变调（听起来"换了个人"）
- **用设备自己的 mibrain 生成**，和服务端对话同一个音色，不会再出现"提示语一个声音、回答另一个声音"：

  ```bash
  ubus call mibrain text_to_speech '{"text":"我在","save":1}'   # 在音箱上执行
  # 取回 mp3 后：
  ffmpeg -y -i prompt.mp3 -f s16le -acodec pcm_s16le -ac 1 -ar 24000 bridge/prompt.pcm
  ```

### 3.3 静音窗口（防"幽灵轮次"）

唤醒提示语播放期间**直接掐掉麦克风**（不送 VAD）。

原因：AEC 只削 27 dB，而 Silero VAD 是幅度归一化的，削完的残留人声照样打 **0.9+ 分**，
和真人说话分布完全重叠。**任何阈值都分不开。**

窗口 = 提示语时长 + 0.8 s，并会自动延长到「设备回采报告播放结束」（`echo_until`）。

### 3.4 连续对话

- 回答完**不退出**，继续听 `timeout: 5` 秒
- 有人说话就续期，一直没人说话才静默退出（不播告别语）
- 说「拜拜」「再见」「退出吧」立即播告别语退出

### 3.5 灯环

7 种状态，配色刻意拉开差异，扫一眼能分辨（`device/led_frames/*.txt`，18 颗灯）：

| 状态 | 颜色 | 动画 |
|---|---|---|
| 唤醒 | 白→金 | 从 12 点向外扩散（一次性） |
| 聆听 | 青白 | 一个亮头绕圈扫描 |
| 没听清 | 橙 | 两短一停的心跳 |
| 思考 | 紫 | 两个光点反向对流 |
| 说话 | 蓝 | 整环呼吸 |
| 忙 | 白 | 单点亮珠跑马 |
| 出错 | 红 | 闪两下 |

### 3.6 打断（barge-in）

- 说话时喊唤醒词 → 立刻停播、响一下、听新问题
- 三个必要条件（都踩过坑）：
  1. **检测时刻**的设备状态要随唤醒一起传下去（异步派发时状态早变了）
  2. 近端能量阈值：LX06 实测 +7.5 / −1.5 dB，取 `near_end_min_db: -17`
  3. 停播要真的调到播放器（常驻 mediaplayer 用 `player_play_operation stop`）

### 3.7 声纹识别

- 模型：CAM++（3D-Speaker，192 维，28 MB），`sherpa_onnx.SpeakerEmbeddingExtractor`
- 只使用**设备录的**片段做底库（手机上传的域差太大：中位相似度 0.237，设备录的是 0.50~0.76）
- 服务端每句并行调 `POST /voiceprint/identify`，把「当前说话人：X」注入提示词
- 阈值 0.45；当前只登记了一个人，所以"认错人"的概率还测不了

### 3.8 网页

| 页面 | 地址 | 用途 |
|---|---|---|
| 对话记录 | `http://<NAS_IP>:8098` | 每轮文字 + 原始录音 + 说话人标签 |
| 声纹管理 | `http://<NAS_IP>:8097` | 加人、录/上传、试匹配 |
| 声纹管理（HTTPS） | `https://<NAS_IP>:8444` | 同上，**网页录音必须走这个** |

> `http://` + 内网 IP 时浏览器的 `navigator.mediaDevices` 是 `undefined`，录音按钮不可用。

### 3.9 原生音色 TTS（本轮重做的部分）

**服务端**：`TTS: TextOnly`（自写的 provider，`bridge/textonly_tts.py`）。

它只做一件事：把每句文字立刻放进发送队列，音频位置塞一段 60 ms 静音占位。
为什么需要它：上游服务端是「先合成音频，再把文字和音频一起下发」，桥接要多等
**1.05~1.19 s** 的云端合成才能拿到文字。换成 TextOnly 后文字随大模型输出立刻到达。

**桥接侧**（`bridge/native_tts_pcm.py`，覆盖镜像里的 `services/native_tts.py`）：

| 环节 | 实现 | 为什么 |
|---|---|---|
| 合成 | 每句一次 `ubus call mibrain text_to_speech`，4 路并发 | 设备实测：串行 1.28 s，并行 0.35 s；每段少一次 RPC（不再搬文件） |
| 交付 | 按顺序、合成完一段就排进水槽（一个 `ln -sf`） | "等全部合成完"会让首句干等长回答的最后一句 |
| 播放 | 设备上常驻 `aplay` + 命名管道；桥接把 mp3 解码成 PCM 回给设备 | 整轮只开一次声卡，段间 0 缝；播放期间不依赖网络实时性 |
| 停顿归一 | 解码时把每段前后静音收拢（前 30 ms / 后 100 ms） | mibrain 每段自带前 85 ms、后 560 ms 静音，直接拼就是 0.65 s 冷场 |
| 收尾 | 排完最后一段落 `stop`，等 aplay 排空后落 `finished` | 不用猜时长，播完立刻回到聆听 |
| 打断 | kill 水槽进程 + `player_play_operation stop` | 管道会立刻断，声音马上停 |

**流式水槽的细节**（`bridge/tts_inbox.py` 的 `POST /pcm` + `bridge/native_tts_pcm.py`）：

```
桥接                                   设备
start()  → 一条单行 printf 生成 /tmp/xq-sink.sh
         → start-stop-daemon -S -b -x /tmp/xq-sink.sh 192.168.31.190:4398 16000
                                         ├─ mkfifo /tmp/xq-tts.pcm
                                         ├─ aplay -q -B 200000 -t raw -f S16_LE -r 16000 -c 1
                                         └─ 循环看 /tmp/xq-tts/N/<n>
每段 ln -sf <mp3路径> /tmp/xq-tts/N/<n>  ─→  curl --data-binary @<mp3> http://桥接:4398/pcm
                                            回应体就是 PCM，直接 >&3 写进管道
最后 touch /tmp/xq-tts/stop            ─→  关写端 → aplay 排空退出 → touch finished
桥接轮询 finished（300 ms 一次）         ←┘
```

几个刻意的选择：

- **脚本用一条 printf 写下去，不用 heredoc**。设备侧命令执行器只适合单行命令。
- **停止标记在"队列里没有第 n 段"之后才生效**，否则最后一段会被吞掉。
- **PCM 走 HTTP 不走 WebSocket**。旧方案把 32 KB/s 的数据塞进控制通道，设备忙时
  直接 `send stalled for 10s`；现在数据走独立连接，控制通道只发一两个字符的命令。
- **aplay 缓冲设成 200 ms**（默认 500 ms）：起播门槛更小，快 ~0.3 s；前面还有
  64 KB 管道（约 2 s）兜住，实测 3 句连播 0 underrun。
- **播放结束不用估时长**：喂料进程关写端后 aplay 会排空退出，落 `finished` 就是
  真播完了。估时长要么切尾音、要么让"说完"延迟。

**兜底**：`XIAOQI_PLAYBACK_MODE=file` 回到"整段 cat 成文件 + mediaplayer 播"
（首句优先开关对它有效）。水槽启动失败、或某段排队三次都失败时，本轮回自动降级。

---

## 4. 关键参数速查

载体：`bridge/config.py`（挂载进容器，改完要 `--force-recreate bridge`）

| 参数 | 现值 | 说明 |
|---|---|---|
| `keywords` | `["你好小七","小七小七"]` | 唤醒词 |
| `keywords_score` | 3.0 | 触发分 |
| `keywords_threshold` | 0.20 | 检出阈值 |
| `keyword_thresholds` | `{"小七小七":0.30}` | 单关键词覆盖（叠词误报多） |
| `wakeup.timeout` | 5 | 连续对话等待秒数 |
| `barge_in.near_end_min_db` | -17.0 | 打断的近端能量阈值 |
| `capture.channels` | 8 | 设备采集声道数 |
| `capture.mic_channel` / `ref_channel` | 0 / 6 | 主麦 / 回声参考 |
| `echo_canceller.taps` | 8 | AEC 抽头数 |
| `tts_output.mode` | `native_xiaomi` | 原生音色；`sherpa` = 服务端音频流 |
| `tts_output.native_xiaomi.target_chars` | 12 | 分段粒度（基本等于一句一段） |
| `tts_output.native_xiaomi.prefetch_segments` | 4 | 预取段数 |
| `speaker.threshold` | 0.45 | 声纹判定阈值 |
| `music.enabled` | `false` | 本地音乐（还没接） |

环境变量：

| 变量 | 值 | 说明 |
|---|---|---|
| `XIAOZHI_TTS_OUTPUT_MODE` | `native_xiaomi` | 覆盖 `tts_output.mode` |
| `XIAOQI_PLAYBACK_MODE` | `stream` | `stream` = 常驻 aplay 连续流（0 缝）；`file` = 整段文件播放（兜底） |
| `XIAOQI_FIRST_SENTENCE_PLAY` | `1` | 只有 `file` 模式用得到 |
| `TTS_TRIM_LEAD_MS` / `TTS_TRIM_TAIL_MS` | `30` / `100` | 每段前后静音收拢到多少毫秒（决定句间停顿长短） |
| `TTS_PCM_RATE` | `16000` | 喂给 aplay 的采样率，必须和 `aplay -r` 一致 |
| `XIAOQI_REF_DUMP_MARKER` | `/data/logs/ref-dump.on` | 排查用：存在这个文件时把播放回采落成 `ref-dump.pcm` |
| `PYTHONPATH` | `/app/pylibs` | soundfile/soxr 所在目录 |

---

## 5. 文件清单

### 桥接补丁（挂载进镜像，覆盖原文件）

| 文件 | 覆盖到 | 作用 |
|---|---|---|
| `bridge/config.py` | `/app/config.py` | 全部配置 + 提示语/告别语逻辑 |
| `bridge/event.py` | `/app/xiaozhi/event.py` | 会话状态机、录音落盘、声纹写入、打断 |
| `bridge/xiaozhi_main.py` | `/app/xiaozhi/xiaozhi.py` | 灯环、STT 处理、TTS 门控、原生链路接入 |
| `bridge/xiaoai.py` | `/app/xiaozhi/xiaoai.py` | AEC、metadata=null 修复 |
| `bridge/stream.py` | `/app/xiaozhi/services/audio/stream.py` | 音频环形缓冲 + 录音 |
| `bridge/vad_hook.py` | `/app/xiaozhi/services/audio/vad/__init__.py` | VAD + **提示语静音窗口** |
| `bridge/config_util.py` | `/app/xiaozhi/utils/config.py` | OTA 带 Client-Id |
| `bridge/kws_sherpa.py` | `/app/xiaozhi/services/audio/kws/sherpa.py` | 唤醒参数从 config 读 |
| `bridge/kws_init.py` | `/app/xiaozhi/services/audio/kws/__init__.py` | 唤醒判定日志 + 用检测时刻的状态 |
| `bridge/native_tts_pcm.py` | `/app/xiaozhi/services/native_tts_pcm.py` | **原生音色播放链路**（并行合成 + 常驻 aplay 连续流） |
| `bridge/speaker_id.py` | `/app/xiaozhi/speaker_id.py` | 声纹采集/标签 |
| `bridge/prompt.pcm` | `/app/prompt.pcm` | 「我在」（24k，0.54 s） |
| `bridge/farewell.pcm` | `/app/farewell.pcm` | 「主人再见，拜拜」（24k，1.51 s） |
| `bridge/models/campplus.onnx` | — | 声纹模型（28 MB） |
| `bridge/pylibs/` | `/app/pylibs` | soundfile / soxr / cffi（无 pip 的镜像里补依赖） |

### 服务端补丁

| 文件 | 覆盖到 | 作用 |
|---|---|---|
| `bridge/textonly_tts.py` | `/opt/xiaozhi-esp32-server/core/providers/tts/textonly.py` | **只发文字、不合成**的 TTS provider |
| `xiaozhi/plugins/get_weather.py` | 同名插件 | 天气换成 wttr.in（和风公共 key 已 401） |
| `xiaozhi/data/.config.yaml` | `/opt/…/data/.config.yaml` | 选择模块、提示词、声纹配置 |

### 独立服务

| 文件 | 作用 |
|---|---|
| `bridge/dialog_web.py` | 对话记录页（8098） |
| `bridge/speaker_web.py` | 声纹管理页（8097/8444） |
| `bridge/speaker_lib.py` | 声纹公共逻辑 |
| `bridge/voiceprint_svc.py` | 声纹识别 HTTP 服务（8096） |
| `bridge/tts_inbox.py` | TTS 水槽服务（4398）：`POST /pcm` = mp3 进、PCM 出（含静音收拢） |
| `bridge/speaker_report.py` | 离线观察工具（聚类/回填） |
| `bridge/kws_eval.py` / `kws_verify.py` | 唤醒词离线回归 |

### 设备端

| 文件 | 位置 | 作用 |
|---|---|---|
| `device/led.sh` | `/data/open-xiaoai/led.sh` | 灯环播放器 |
| `device/led_frames/*.txt` | `/data/open-xiaoai/led_frames/` | 逐帧动画 |
| `device/gen_led_frames.py` | 本地 | 生成帧 |
| `device/stop_tts.sh` | `/data/open-xiaoai/stop_tts.sh` | 停设备本地 TTS |

---

## 6. 踩坑记录（最有价值的部分）

### 6.1 音频采集

| 现象 | 原因 | 解法 |
|---|---|---|
| 采到 −81 dBFS 的静音 | 客户端用了 `-f S16_LE` | 必须 `-f S32_LE` |
| Silero 概率恒 0 | 用了 v4 模型 | 必须 v5（2.3 MB） |
| 远场唤醒不稳 | 6 麦平均无收益，SNR 仅 4.8 dB | 只用 ch0，靠调阈值 |

### 6.2 唤醒

- 叠词「小七小七」斜召回差（1/4），换回「你好小七」后 4/4
- `keywords.txt` 里逐行的 `:boost` 在这个 sherpa-onnx 版本**不生效**，只能改全局值
- 镜像里 `keywords_score` 写死 2.0，必须挂载 `kws_sherpa.py` 才能唤醒

### 6.3 TTS 音色与播放（本轮花了最久）

**① 「LX06 没有 mibrain」是个错误结论。** 设备上 `mibrain_service` 一直在跑，
`ubus call mibrain text_to_speech` 直接可用，vendor 是 `AiNiRobot`，30 字只要 0.12 s。

**② 接入原生音色后反而更慢**，因为服务端是"先合成 EdgeTTS，再下发文字"：
文字要等云端合成完才到桥接，原生链路的合成和起播全加在 1.1 s 之上。

**③ 实时推 PCM 在这台设备上走不通。** 试了 60 ms 分片 + 350 ms 领先量，日志里反复出现：

```
⚠️ 播放缓冲见底 -275 ms
❌ 已断开连接: send stalled for 10s
```

根因是设备在播音频时 load 6+、可用内存 5 MB，任何一次卡顿超过缓冲深度就断流；
而且领先量超过设备 aplay 缓冲（200 ms）会反向顶死，连 WS 连接都被拖断。

**④ 玩家进程开销不能忽略。** 每段 spawn 一次 `miplayer` 要 0.35 s 起播，四句话就是
1.4 s 的空档；换成常驻 `mediaplayer` 守护进程后 < 0.2 s，而且播放与桥接解耦。

**⑤ 设备 RPC 是稀缺资源。** 单次 `run_shell` 在设备忙时要 0.3~0.5 s。
上游实现每段两次 RPC（ubus + mv/wc），四句话排八次队就是两秒多。
把"搬文件""量大小""删文件"全部合并/批量化之后，合成阶段从 2.09 s 降到 0.32 s。

**⑥ 交付时机比合成速度更重要。** 即使并行合成，如果"等全部文本 + 全部合成完再交给
播放器"，长回答的首句仍然要等到最后一句合成完。改成"合成完一段就按顺序交付"后，
首句 80 ms 就能开播。

**⑦ 查播放状态不如按时长等。** 每次 `player_get_play_status` 都是一次 RPC，
光排队就带来 0.35 s 的段间衔接缝；音频是恒定 32 kbps，按时长估算误差只有十几毫秒，
直接睡更划算。（这条在改成连续流之前成立；现在收尾改成"等 aplay 排空后落标记"，
不需要估时长了。）

**⑧ 段间缝的根因不是"等得不够久"，是"重开播放器"。** 用音箱的回采通道
（ch6/播放回环）量化过：同一个 mediaplayer 播第二个文件时，从上一段被切断到
新文件出声，固定 **~130 ms** 静音。换 mp3/wav、换 `type`、带 `group`、
提前/延后触发都改不了；改成设备本地定时触发也只是把 0.33 s 变成 0.13 s。
只要每句一个文件，就压不到 0.1 s 以下。

**⑨ 顺手排除了三条"看起来更优雅"的路**（都是为了不重开播放器）：

| 想法 | 实测结果 |
|---|---|
| 边播边往同一个文件里 `cat >>` | 不行：播放器打开时就定死了长度，后续写入不播 |
| `miplayer -f <命名管道>` 直接喂 mp3 | 不行：`input error -10102`，它需要可 seek 的输入 |
| `miplayer --socket <unix socket>` | 设备上没有能写 unix socket 的工具（busybox nc 只支持 TCP），桥接又在 NAS 上连不到 |
| `mibrain text_to_speech` 加 `play:1` 原生播 | 不能当队列用：异步投递、快速连投会互相覆盖，而且完全无法知道什么时候播完 |

**⑩ 常驻 `aplay` + 命名管道才是零缝隙的正解。** FIFO 里的 PCM 是连续字节流，
aplay 只开一次 ALSA：两段 1.5 s 的音实测是一条连续的 3005 ms 波形（缝 0）。
关键配套：aplay 在"回答开始"就预开（阻塞等数据），起播代价被大模型出字掩盖；
`-B 200000` 让起播门槛从 500 ms 降到 200 ms，又快 ~0.3 s。

**⑪ mibrain 每段自带 0.65 s 静音。** 解码后实测：前导 80~90 ms、结尾 540~570 ms。
以前"整段 cat 播放"时，每个句号后面就是这 0.65 s 的冷场 —— 用户听到的"停顿长"
有一半是它，不是衔接缝。现在解码时把前后静音收拢到 30 ms / 100 ms，
句间停顿归一到 ~0.17 s。

**⑫ mibrain 的 `codec` 参数是摆设。** `pcm`/`wav`/`raw`/`opus`/`aac` 全部返回
同样的 32 kbps mp3（同样的字节数），所以 PCM 只能在桥接侧解。

### 6.4 幽灵轮次

AEC 只削 27 dB，Silero 幅度归一化后残留人声照样 0.9+ 分，和真人分布完全重叠。
**只能硬静音**，没有阈值可调。

### 6.5 打断（花了最久）

三层故障串在一起，每修一层才露出下一层：

1. `near_end_min_db` 写成正数 8.0 → 真实打断全被判成"自己的声音"
2. 状态在**异步派发时**读取 → 那时小七已说完，结果被当过期丢弃
3. `_stop_prompt(speaker)` 里 `speaker` 从未赋值 → `NameError`，又被
   `run_coroutine_threadsafe` 静默吞掉 → 表现为"喊了完全没反应"

> 教训：遇到"完全没反应"，先想办法把异常打出来，别去猜阈值。

### 6.6 声纹

- 手机上传的音频拿去当底库**完全不可用**（中位相似度 0.237，和不认识的人分不开）
- 必须用音箱自己录的片段（0.50~0.76 真实 / ≤0.38 噪声）
- 服务端启动时会缓存声纹服务健康检查 10 分钟，顺序上要先把 `voiceprint` 拉起来

### 6.7 设备资源

- 118 MB 可用内存、播音频时 load 6+、可用内存 5 MB
- 8 声道实时采集会 OOM 杀掉小爱引擎（`MdsWakeup`/`mipns-xiaomi`），只能重启 `/etc/init.d/pns`
- **不要在设备上做实时多路 DSP**

### 6.8 运维

- 挂载进容器的 Python 文件**不会热更新**，必须 `--force-recreate`
- 容器重建会丢失 `docker exec` 里装的东西（依赖要放在挂载目录里）

---

## 7. 已知限制

1. **唤醒距离**：远场 SNR 只有 4.8 dB，3 米内还行，更远要靠喊
2. **单人声纹**：只登记了一个人，"认错人"的概率还没法测
3. **人物 id 写死**：`.config.yaml` 里的 `p_xxxxxxxx` 在管理页删人重建后会失效
4. **音乐功能未启用**：`music.enabled: false`，说"放首歌"只有回复没有声音
5. **设备连接偶发抖动**：3 小时 4 次断连（`nothing received for 15s` / `Connection reset`），
   会自动重连，但断的瞬间唤醒是死的
6. **工具调用拖慢首字**：调天气那轮 3.10 s，多出来的全是外部网络往返
7. **声纹只在每轮开头有效**：服务端要求音频 > 28800 字节（约 0.9 s），太短的句子不认
8. **连续流依赖设备上的 curl + 喂料进程**：设备极端卡顿时单段 PCM 取回会慢到 0.3 s，
   但管道里有约 2 s 缓冲，实测没有出现断续；真出问题时本轮回自动降级到文件播放

---

## 8. 后续迭代方向

按性价比排序：

1. **本地音乐** —— 曲库 121 GB / 4425 首现成（4309 flac + 77 mp3 + 39 wav），
   设备播放器现成，改 `music.enabled` + 加一个意图钩子即可
2. **Home Assistant 接入** —— A 档：服务端内置 hass 插件，查状态 + 开关灯
3. **多人声纹** —— 补登记妈妈，才能测出"认错率"
4. **声纹驱动行为** —— 称呼、儿童模式、个人偏好
5. **设备连接稳定性** —— 减少那 4 次/3 小时的断连
6. **工具调用提速** —— 天气/新闻这类外部请求是最慢的一环

---

## 9. 常用命令

```bash
# 看桥接日志（唤醒/打断/合成/播放耗时）
docker logs -f xiaoai-bridge

# 改了 config.py / event.py / native_tts_pcm.py 后必须重建
docker compose up -d --force-recreate bridge

# 改了网页/声纹服务
docker compose restart dialog-web speaker-web voiceprint

# 改了 .config.yaml（服务端配置）
docker compose restart xiaozhi-server

# 唤醒词离线回归（改 keywords.txt 或阈值后先跑）
docker compose exec bridge python /app/xiaozhi/kws_verify.py

# 声纹数据观察（聚类 / 回填历史录音）
docker compose exec bridge python /app/speaker_report.py

# 备份
tar czf backup-$(date +%F).tgz logs xiaozhi/data/.config.yaml bridge
```

---

## 10. 一次性验证过的关键数字

| 项目 | 数值 | 日期 |
|---|---|---|
| 唤醒成功率（你好小七） | 4/4 | 11-02 |
| 唤醒提示语时长 | 0.54 s | 11-04 |
| 告别语时长 | 1.51 s | 11-04 |
| AEC 回声抑制 | ~27 dB | 11-02 |
| 声纹 embedding 耗时 | ~110 ms | 11-03 |
| 声纹识别（设备录音，真实） | 0.50~0.76 | 11-03 |
| mibrain 合成（30 字，空闲） | 0.12 s | 11-04 |
| 并行 vs 串行合成（4 句） | 0.35 s vs 1.28 s | 11-04 |
| mediaplayer 起播 | < 0.2 s | 11-04 |
| miplayer 起播（对照） | ~0.35 s | 11-04 |
| **首字延迟（纯对话）** | **0.88~1.55 s，中位 ~1.2 s** | 11-04 |
| 首字延迟（带工具调用） | 3.10 s | 11-04 |
| 段间衔接缝（旧：每句一个文件） | 0.13 s（播放器重开下限）/ 实测 0.33 s | 11-04 |
| **段间衔接缝（现行：常驻 aplay 连续流）** | **0 ms** | 11-04 |
| mibrain 每段自带静音 | 前 85 ms / 后 560 ms | 11-04 |
| 句间停顿（收拢后） | ~0.17 s | 11-04 |
| 三句连播 underrun | 0 次（`-B 200000`） | 11-04 |
| 整轮水槽开销（起播+排空） | ~0.37 s，摊销到整轮 | 11-04 |
| 稳定性（近 3 小时） | 0 断流 / 0 播放失败 | 11-04 |
