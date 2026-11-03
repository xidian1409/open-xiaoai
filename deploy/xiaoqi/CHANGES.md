# 相对上游 open-xiaoai 改了什么

基线：[`OwnDing/open-xiaoai`](https://github.com/OwnDing/open-xiaoai) `main` @ `24f071c`（2026-10-25）。

改动分两类：**改上游已有文件**（最小 diff，用 volume 覆盖）和**新增文件**（整份新增）。
每一处的来龙去脉和实测数字在 [DESIGN.md](./DESIGN.md) 里。

---

## 一、改上游已有文件

映射关系：左边是本目录里的文件，右边是它在镜像里覆盖的位置。

| 本仓库 | 覆盖到镜像 | 改了什么 |
|---|---|---|
| `bridge/config.py` | `/app/config.py` | 唤醒词换成 `你好小七`；打分/阈值/候选路径全部可配（`keywords_score` 3.0、`keywords_threshold` 0.20、`max_active_paths` 8）；开启 AEC 参考、近端抑制 `near_end_min_db` −17 dB；新增 `speaker.*`（声纹）、提示语/告别语 PCM 与对应 `before_wakeup` / `after_wakeup` 逻辑 |
| `bridge/event.py` | `/app/xiaozhi/event.py` | 回答完**不退出**，改成连续听 `wakeup.timeout` 秒；每次进入聆听打灯；修复打断（检测时刻状态判定、`speaker` 变量未赋值导致的 `NameError`）；每轮原始语音落盘；声纹结果写入对话日志 |
| `bridge/xiaozhi_main.py` | `/app/xiaozhi/xiaozhi.py` | 接入灯环状态机（`_led` / `_send_led` / 超时回收）；TTS 直出模式下的输出门控 |
| `bridge/xiaoai.py` | `/app/xiaozhi/xiaoai.py` | AEC 打通；`metadata` 为 `null` 时的 `AttributeError` 兜底 |
| `bridge/stream.py` | `/app/xiaozhi/services/audio/stream.py` | 音频环形缓冲 + 整轮录音落盘 |
| `bridge/vad_hook.py` | `/app/xiaozhi/services/audio/vad/__init__.py` | **提示语播放期间硬静音 VAD**（`mute_for_prompt`），并把任何一帧人声当续期信号 |
| `bridge/kws_sherpa.py` | `/app/xiaozhi/services/audio/kws/sherpa.py` | 打分/阈值/尾随空格数改为读 config（镜像里写死 `2.0`，唤不醒） |
| `bridge/kws_init.py` | `/app/xiaozhi/services/audio/kws/__init__.py` | 唤醒判定打日志；用**检测时刻**的设备状态决定是否打断（异步派发时状态早就变了） |
| `bridge/config_util.py` | `/app/xiaozhi/utils/config.py` | OTA 请求补 `Client-Id`（xiaozhi-server 0.9.x 缺了会返回「OTA请求ClientID为空」） |
| `bridge/models/keywords.txt` | `/app/xiaozhi/models/keywords.txt` | 用 `gen_keywords.py` 重新生成（`你好小七` / `小七小七`） |

## 二、新增文件

### 桥接内的新模块

| 文件 | 作用 |
|---|---|
| `bridge/speaker_id.py` | 声纹采集 worker：CAM++ 提 192 维 embedding，写 `speakers.jsonl` 和对话日志 |
| `bridge/gen_keywords.py` | 中文唤醒词 → 拼音 token，生成 `keywords.txt`（绕过镜像里对 `bpe.model` 的依赖） |
| `bridge/prompt.pcm` / `bridge/farewell.pcm` | 唤醒应答「我在」（0.53 s）与退出语「主人再见，拜拜」（2.23 s），**24 kHz** |

### 独立服务（都不在实时音频链路里，挂了不影响对话）

| 文件 | 端口 | 作用 |
|---|---|---|
| `bridge/voiceprint_svc.py` | 8096 | 声纹识别 HTTP 服务，供服务端 `voiceprint` 接口调用 |
| `bridge/speaker_web.py` | 8097 / 8444 | 声纹管理页：加人、录音、上传、试匹配 |
| `bridge/speaker_lib.py` | — | 声纹公共逻辑（模型加载、存储、相似度） |
| `bridge/dialog_web.py` | 8098 | 对话记录页：每轮文字 + 录音 + 说话人标签 |
| `bridge/speaker_report.py` | — | 离线观察工具：聚类、回填历史录音 |
| `bridge/kws_eval.py` / `bridge/kws_verify.py` | — | 唤醒词离线回归（改阈值/换词后先跑这个） |

### 设备端

| 文件 | 位置 | 作用 |
|---|---|---|
| `device/led.sh` | `/data/open-xiaoai/led.sh` | 灯环播放器，直接写 `/sys/devices/i2c-0/0-003a/led_rgb` |
| `device/led_frames/*.txt` | `/data/open-xiaoai/led_frames/` | 7 种状态的逐帧动画数据 |
| `device/gen_led_frames.py` | 本地 | 生成上面的帧（渐变 / 扫光 / 呼吸 / 跑马） |
| `device/stop_tts.sh` | `/data/open-xiaoai/stop_tts.sh` | 停设备本地 TTS 残留 |

灯效配色刻意拉开差异，扫一眼就能分清状态：唤醒白→金、聆听青白扫描、没听清橙色心跳、
思考紫色对流、说话蓝色呼吸、出错红色闪两下。

### 服务端覆盖层

| 文件 | 作用 |
|---|---|
| `xiaozhi/data/.config.example.yaml` | 只写需要覆盖的项（模板，`api_key` 等留空） |
| `xiaozhi/plugins/get_weather.py` | 天气插件换成 wttr.in（原实现依赖和风天气，公共 key 已 401） |

## 三、没有改的部分

`packages/client-rust`、`packages/client-patch`、`examples/` 下的其余示例都没动。
音箱端二进制直接用上游的，唯一的硬要求是采集格式 `-f S32_LE`。
