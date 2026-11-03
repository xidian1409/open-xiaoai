# 小七家庭语音助手 · 设计文档

> 最后一次大改：2026-11-03。本文档记录**当前实现状态**、**为什么这么做**、**踩过的坑**，
> 方便后续迭代时不用重新踩一遍。
>
> 机器：小米 AI 音箱 Pro（LX06）· <音箱IP> · NAS `<NAS_IP>`

---

## 1. 一句话概览

用 `OwnDing/open-xiaoai` 的 fork 把音箱改造成自建语音助手：
**唤醒/VAD 跑在音箱边的桥接上，ASR/LLM/TTS 跑在 NAS 上**，全链路数据不出家门（除了 LLM 和 TTS 的网络请求）。

和原版小爱的关键差异：

| | 原版小爱 | 现在这套 |
|---|---|---|
| 唤醒词 | 小爱同学 | **你好小七**（备用：小七小七） |
| ASR | 小米云 | 本地 SenseVoice |
| LLM | 小爱云 | DeepSeek |
| TTS | 小米云（有内容审查） | EdgeTTS（无审查） |
| 声纹 | 小米云 | 本地 CAM++ |
| 唤醒音频 | 上传小米云（100%） | 不出家门 |

---

## 2. 系统架构

### 2.1 组件

| 容器 | 镜像 | 端口 | 作用 |
|---|---|---|---|
| `xiaoai-bridge` | `local/open-xiaoai-xiaozhi:barge-in` | 4399 | 接音箱、唤醒、VAD、AEC、音频转发 |
| `xiaozhi-server` | `xinnan-tech/xiaozhi-esp32-server:server_latest` | 8000(ws) / 8003(OTA) | ASR、LLM、TTS、会话状态机 |
| `xiaoqi-dialog` | 同 bridge | 8098 | 对话记录页（只读） |
| `xiaoqi-speaker` | 同 bridge | 8097 / 8444(https) | 声纹管理页 |
| `xiaoqi-voiceprint` | 同 bridge | 8096 | 声纹识别 HTTP 服务（给小智服务端用） |

### 2.2 数据流

```
LX06 6 麦 + 2 路回采（8ch / 48k / S32）
   │  客户端只取 ch0(麦) + ch6(回采)，降到 16k，发立体声 [mic, ref]
   ▼
桥接 AEC（SubbandEchoCanceller，消 ~27 dB）──────► 16k 单声道
   │
   ├─► KWS（sherpa-onnx zipformer，关键词「你好小七」）
   ├─► VAD（Silero v5）
   └─► 整句 PCM ──┬─► 送 xiaozhi-server（ASR → LLM → TTS）
                  └─► 本机算声纹 embedding（192 维，~110 ms）
                        ├─► 写 speakers.jsonl（观察用）
                        └─► 写 dialog.jsonl 的 speaker 记录（页面标签）

xiaozhi-server 收到整句音频后：
   ├─► SenseVoice 识别出文字
   ├─► 调 http://xiaoqi-voiceprint:8096/voiceprint/identify 认人
   │      → 把「当前说话人：X」注入系统提示词 <speakers_info>
   └─► DeepSeek 生成回答 → EdgeTTS 合成 24k PCM → 桥接 → 音箱
```

### 2.3 为什么音频链路是"一路麦克风"

设备暴露的是 8 声道 PDM 原始数据（`hw:0,3`），**没有 DSP 波束成形的输出**。
选 ch0 而不是 6 路求和的原因（实测）：

| 方案 | SNR |
|---|---|
| 单路 ch0 | 4.8 dB |
| 6 路直接平均 | 4.7 dB |
| 6 路延迟对齐相加 | 4.7 dB |

因为噪声本身在 6 路之间是**相关**的（静音段互相关 0.40），多路平均只能消独立噪声，
理论上限 `6/(1+5×0.4)=3 dB`，实测还被相位误差吃掉一部分。**不值得为此重编译客户端。**

> 小爱的前端确实更强（DOA 波束成形 + 更好的 AEC，唤醒时自报 SNR 14.9 dB），
> 但那是联发科闭源 SDK 跑在同一颗 SoC 上，搬不过来；
> 而且设备已经满载（118 MB 内存，常态只剩 5~8 MB，load 6~10）。

---

## 3. 功能模块现状

### 3.1 唤醒词

- 主词 **你好小七**，备用 **小七小七**
- `keywords_score: 3.0`（上游写死 2.0）、`keywords_threshold: 0.20`、`max_active_paths: 8`
- 用真实录音做的离线回归（同一批 4 条真人「你好小七」+ 74 条真实负样本）：

| 配置 | 命中 | 误唤醒 |
|---|---|---|
| 小七小七 · 打分 2.0 | 1/4 | 0 |
| 你好小七 · 打分 2.0 | 3/4 | 0 |
| **你好小七 · 打分 3.0** | **4/4** | 1（"你好小齐"，同音） |

**两个反直觉的点**：
1. `max_active_paths` 调大反而掉召回（8→16 时 4/4 → 2/4）
2. `keywords.txt` 里逐行的 `:boost` 在这个 sherpa-onnx 版本**不生效**，只能改全局值
   → 所以加了 `deploy/xiaoqi/bridge/kws_sherpa.py` 从 config 读参数

### 3.2 唤醒提示语

- 内容「我在」，0.53 秒，**24 kHz 单声道 s16le**
- 必须 24k：设备播放通道是 24k，用 16k 会变速变调（听起来"换了个人"）
- 生成方式和**服务端完全相同的 EdgeTTS 参数**（`zh-CN-XiaoxiaoNeural` + `rate:+0%` + `volume:+50%` + `pitch:+0Hz`），
  在 `xiaozhi-server` 容器里合成 mp3，再用 ffmpeg 转 24k/单声道/s16le 裸 PCM：

  ```bash
  docker exec xiaozhi-server python -c "
  import asyncio, edge_tts
  async def m(): await edge_tts.Communicate('我在','zh-CN-XiaoxiaoNeural',
      rate='+0%', volume='+50%', pitch='+0Hz').save('/tmp/p.mp3')
  asyncio.run(m())"
  docker cp xiaozhi-server:/tmp/p.mp3 /tmp/p.mp3
  ffmpeg -y -i /tmp/p.mp3 -ar 24000 -ac 1 -f s16le deploy/xiaoqi/bridge/prompt.pcm
  ```
- 告别语「主人再见，拜拜」同理（2.23 s），走同一条 PCM 通道

### 3.3 静音窗口（防"幽灵轮次"）

唤醒提示语播放期间**直接掐掉麦克风**（不送 VAD）。

原因：AEC 只削 27 dB，而 Silero VAD 是幅度归一化的，削完的残留人声照样打 **0.9+ 分**，
和真人说话分布完全重叠（实测：回声 >0.9 的帧占 11~28%，真人 14~29%）。**任何阈值都分不开。**

窗口 = 提示语时长 + 0.8 s，并且会自动延长到「设备回采报告播放结束」（`echo_until`）。

### 3.4 连续对话

- 回答完**不退出**，继续听 `timeout: 5` 秒
- 有人说话就续期，一直没人说话才静默退出（不播告别语）
- 倒计时可被"出声"刷新（`note_voice`）

### 3.5 灯环

| 状态 | 颜色 | 样式 |
|---|---|---|
| 唤醒 | 白→金 | 从 12 点向外铺开 + 白闪（一次性） |
| **在听** | **青白** | 一个亮头带长尾绕圈扫描，底光很暗 |
| 快超时 | **橙色** | 两短一停的心跳 |
| 在想 | **紫色** | 两个光点反向对流 |
| 在说 | **蓝色** | 整环呼吸 |
| 连接中 | 白色 | 单点亮珠跑马 |
| 出错 | 红色 | 闪两下 |

帧文件在 `deploy/xiaoqi/device/led_frames/`，由 `gen_led_frames.py` 生成，推到设备
`/data/open-xiaoai/led_frames/`。设备端 `led.sh` 只做 read+write，省 CPU。

### 3.6 打断（barge-in）

小七说话时喊「你好小七」→ 立刻停、灯转青白扫描、接着听。**已修好，实测可用。**

三道过滤条件：

| 条件 | 值 | 说明 |
|---|---|---|
| `echo_reference` | true | 设备同时发回播放回采，桥接据此消回声 |
| `barge_in_ready()` | — | 回采在 2 s 内有更新才允许打断 |
| `near_end_min_db` | **−17** | 消完回声后剩下的人声 / 被消掉的回声；低于它当"自己的声音"忽略 |

实测近端值：**+7.5 dB / −1.5 dB**（与 `aec.py` 注释里的 −13/−20 不是同一量级，别照抄）。

### 3.7 声纹识别

**采集侧**（桥接）：每句话算一次 192 维 embedding，写 `speakers.jsonl`；
同时在对话记录里写一条 `{"role":"speaker","file":...,"name":...,"score":...}`。

**识别侧**（服务端）：xiaozhi-server 自带 voiceprint 模块，每句话**并行**调
`POST /voiceprint/identify`，拿到名字后注入系统提示词：

```xml
<speakers_info>
当前说话人：示例用户
- 示例用户：家里的男主人
</speakers_info>
```

实现：`deploy/xiaoqi/bridge/voiceprint_svc.py`（NAS 上，CAM++ + 管理页登记的**设备录音**）

**判定**：

- 声纹服务只返回相似度，阈值在服务端配置（`similarity_threshold: 0.45`）
- 桥接侧另有一套规则用于页面标签：`≥0.45` 直接认；同一轮对话里刚确认过的人，
  下一句 `≥0.38` 也认（`carry_threshold`），解决"唱首歌"那次 0.424 的漏判

**实测数据（同一个人、同一段音频对比两种登记方式）**：

| 登记方式 | 真人语音分数 | 杂音 | 间隔 |
|---|---|---|---|
| **设备录**（对小七说） | **0.50 ~ 0.76** | ≤0.38 | **0.12** |
| 手机录（上传） | 0.27 ~ 0.62 | ≤0.23 | **0.04** |

手机录系统性低 **0.17**，间隔只剩 0.04 —— 多人场景必错。
**结论：门禁级别的识别必须用设备录。**

> 域迁移（把手机声纹映射到设备域）做过留一验证，平均 +0.194 有效，
> 但**跨人迁移没验证过**（只有一个登记人），暂不做。

### 3.8 网页

| 页面 | 地址 | 作用 |
|---|---|---|
| 对话记录 | http://<NAS_IP>:8098 | 每轮文字 + 原始录音 + **说话人标签** |
| 声纹管理 | http://<NAS_IP>:8097 | 加人、录/上传、试匹配 |
| 声纹管理（HTTPS） | https://<NAS_IP>:8444 | 同上，**网页录音必须走这个** |

> 浏览器只在 HTTPS 或 localhost 下允许 `getUserMedia`。
> `http://` + 内网 IP 时 `navigator.mediaDevices` 是 `undefined`。

---

## 4. 关键参数速查

`deploy/xiaoqi/bridge/config.py`：

```python
wakeup:
  keywords: ["你好小七", "小七小七"]
  keywords_score: 3.0          # 上游写死 2.0，实测 3.0 才能 4/4
  keywords_threshold: 0.20
  keyword_thresholds: {"小七小七": 0.30}
  max_active_paths: 8          # 调 16 反而掉召回
  stream_offsets_ms: [0]
  timeout: 5                   # 连续对话倾听窗口（秒）

vad:
  threshold: 0.10
  min_silence_duration: 700
  short_utterance_silence: 1300
  prepend_lookback_ms: 600     # 补回"开口被吃掉"的部分
  tts_end_guard_ms: 300
  no_reply_prompt: ""          # 没听清时不出声，静默重听

barge_in:
  echo_reference: True
  near_end_min_db: -17.0       # 实测真人 +7.5/-1.5 dB
  echo_vad_threshold: 0.7      # 播报余音期间提高 VAD 门槛
  capture: {pcm: noop, channels: 8, sample_rate: 48000,
            mic_channel: 0, ref_channel: 6, mic_shift: 16, ref_shift: 16}
  echo_canceller: {taps: 8, tau_s: 3.0, output_gain: 256}

speaker:                       # 桥接侧标签用
  threshold: 0.45
  min_seconds: 1.0
  carry_threshold: 0.38
  carry_window_s: 120

music:
  enabled: False               # 本地音乐还没开（需要挂歌单目录）
```

`deploy/xiaoqi/xiaozhi/data/.config.yaml` 里额外加了：

```yaml
voiceprint:
  url: http://xiaoqi-voiceprint:8096/voiceprint/identify?key=xiaoqi
  similarity_threshold: 0.45
  speakers:
    - "p_xxxxxxxx,示例用户,家里的男主人"    # ⚠️ 人员 id 变了要同步改
```

---

## 5. 文件清单

### 桥接补丁（挂载进镜像，覆盖原文件）

| 文件 | 覆盖到 | 作用 |
|---|---|---|
| `bridge/config.py` | `/app/config.py` | 全部配置 + 唤醒提示语/告别语逻辑 |
| `bridge/event.py` | `/app/xiaozhi/event.py` | 会话状态机、录音落盘、声纹写入、打断 |
| `bridge/xiaozhi_main.py` | `/app/xiaozhi/xiaozhi.py` | 灯环、STT 处理、TTS 门控 |
| `bridge/xiaoai.py` | `/app/xiaozhi/xiaoai.py` | AEC、metadata=null 修复 |
| `bridge/stream.py` | `/app/xiaozhi/services/audio/stream.py` | 音频环形缓冲 + 录音 |
| `bridge/vad_hook.py` | `/app/xiaozhi/services/audio/vad/__init__.py` | VAD + **提示语静音窗口** |
| `bridge/config_util.py` | `/app/xiaozhi/utils/config.py` | OTA 带 Client-Id |
| `bridge/kws_sherpa.py` | `/app/xiaozhi/services/audio/kws/sherpa.py` | 唤醒参数从 config 读 |
| `bridge/kws_init.py` | `/app/xiaozhi/services/audio/kws/__init__.py` | 唤醒判定日志 + 用检测时刻的状态 |
| `bridge/speaker_id.py` | `/app/xiaozhi/speaker_id.py` | 声纹采集/标签 |
| `bridge/prompt.pcm` | `/app/prompt.pcm` | 「我在」（24k） |
| `bridge/farewell.pcm` | `/app/farewell.pcm` | 「主人再见，拜拜」（24k） |
| `bridge/models/campplus.onnx` | — | 声纹模型（28 MB） |

### 独立服务

| 文件 | 作用 |
|---|---|
| `bridge/dialog_web.py` | 对话记录页（8098） |
| `bridge/speaker_web.py` | 声纹管理页（8097/8444） |
| `bridge/speaker_lib.py` | 声纹公共逻辑（模型、存储） |
| `bridge/voiceprint_svc.py` | 声纹识别 HTTP 服务（8096） |
| `bridge/speaker_report.py` | 离线观察工具（聚类/回填） |
| `bridge/kws_eval.py` / `kws_verify.py` | 唤醒词离线回归脚本 |

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

| 现象 | 根因 | 解法 |
|---|---|---|
| 麦克风只有 −81 dBFS | 老客户端 `-f S16_LE` 抓不到 S32 的高位 | 换 fork 客户端，`-f S32_LE` |
| Silero 概率恒 0 | 用了 v4 模型 | 必须 v5（2.3 MB） |

### 6.2 唤醒

| 现象 | 根因 | 解法 |
|---|---|---|
| 叠词「小七小七」叫不醒 | 模型对叠词召回差（1/4） | 换回「你好小七」（4/4） |
| 调 `max_active_paths` 越调越差 | 6→8→16 呈非单调 | 保持 8 |
| `keywords.txt` 改 `:3.0` 无效 | 该版本不支持逐行 boost | 改全局 + 挂 `kws_sherpa.py` |

### 6.3 TTS 音色

| 现象 | 根因 | 解法 |
|---|---|---|
| 提示语和回复"不是一个人" | ①漏了服务端 `volume:+50%` ②告别语走的是设备自带 `tts_play.sh` | 用服务端同参数生成 PCM，告别语改走桥接 PCM 通道 |
| 提示语变调/语速快 | 文件 16k、播放通道 24k | 统一 24k |

### 6.4 幽灵轮次（把自己提示语当用户说话）

| 现象 | 根因 | 解法 |
|---|---|---|
| 唤醒后立刻出现"我说：你好主人" | Silero 归一化 + AEC 只削 27 dB，残留照样 0.9+ 分 | **播放期间硬静音 VAD**，播完再收音 |

### 6.5 打断（花了最久）

**三层故障串在一起，每修一个才露出下一个：**

1. `near_end_min_db` 写成正数 `8.0` → 真实打断全被判成"自己的声音"
2. 判定用的状态在**异步派发时**才读 → 那时小七已说完，状态变了被当过期结果丢弃
   → 改成在**检测那一刻**把 `device_state` 一起传下去
3. **`_stop_prompt(speaker)` 里的 `speaker` 从来没赋值** → `NameError`
   → 而它是 `asyncio.run_coroutine_threadsafe` 调起的，**异常存进 Future 无人读取**，
   静默崩溃。表现就是"喊了完全没反应"

> **教训：** 遇到"完全没反应"先想办法把异常打出来，别去猜阈值。
> 给 `barge_in` 包一层 try/except 打印 traceback 是解决问题的关键一步。

**排查手段**（已内置）：`kws_init.py` 会打印

```
🔎 KWS命中 '你好小七' rev 4/4 mode=active state=speaking barge_ready=True paused=False -> 处理
```

### 6.6 声纹

| 现象 | 根因 | 解法 |
|---|---|---|
| 手机录的声纹搬到音箱上完全认不出（中位 0.237） | 通道差异（麦克风/距离/混响/AEC） | **必须设备录** |
| 短句漏判（"唱首歌" 0.424） | 短句 + 语气变化，单人阈值会漏 | 连续对话内放宽到 0.38 |
| 录入了但问"我是谁"答不上 | 桥接只做了**记录**，从没告诉大模型 | 接服务端原生 voiceprint 接口 |

### 6.7 设备资源

LX06 只有 **118 MB 内存**，常态剩 5~8 MB，load 6~10。
我做 8 声道 × 16 kHz 实时采集时直接把它压垮：**OOM killer 连杀了 29 次**
小爱的 `MdsWakeup` / `mipns-xiaomi`，小爱整整 8 分钟不能唤醒。

> **教训：不要在这台音箱上做任何额外的实时音频处理。**
> 能搬到 NAS 的一律搬。

### 6.8 运维

- **改 `config.py` 必须 `up -d --force-recreate`**，`restart` 不刷新挂载
- 设备 `/tmp` 是 tmpfs，重启就没了，测试 wav 会消失
- `aplay -q` 会静默失败，排查时去掉 `-q`
- 开着 `echo_reference` 时，`aplay` 播的声音会被 AEC 消掉，**没法做合成唤醒测试**
- 设备 SSH 要 `-oHostKeyAlgorithms=+ssh-rsa`，密码 `open-xiaoai`

---

## 7. 已知限制

1. **唤醒距离**：远场 SNR 只有 4.8 dB，3 米内还行，更远要靠喊
2. **单人声纹**：只登记了一个人，"认错人"的概率还没法测
3. **人物 id 写死**：`.config.yaml` 里的 `p_xxxxxxxx` 在管理页删人重建后会失效
4. **音乐功能未启用**：`music.enabled: false`，说"放首歌"只有回复没有声音
5. **打断尾音**：`abort_tts_output()` 在 sherpa 模式下是空实现，靠关流停声，尾音有 0.3~0.5 s
6. **声纹只在每轮开头有效**：服务端要求音频 > 28800 字节（约 0.9 s），太短的句子不认

---

## 8. 后续迭代方向

按性价比排序：

1. **补第二个人的声纹**（妈妈）
   → 能算"认错率"和"top1-top2 margin"，这才叫真正的准确率
2. **声纹影响对话**：现在已经注入 `<speakers_info>`，可以进一步做
   - 称呼（"早，爸爸"）
   - 儿童模式（识别到小孩换简单表达）
   - 个人偏好记忆
3. **打开本地音乐**：挂一个歌单目录 → `music.enabled: true` → 支持"放首歌/下一首/暂停"
   （已有的"说话时音乐降 20 dB"逻辑会自动生效）
4. **降低远场噪声**：
   - 桥接侧加 WebRTC APM 的 NS/AGC（不碰设备，可控可回退）
   - 或者评估波束成形（要重编译客户端，收益不确定）
5. **唤醒词再调**：现在 4/4 命中，但 74 条真实负样本里有 1 条误唤醒（"你好小齐"，同音字）。
   可以补充更多负样本做回归，或评估加第二个词做二次确认
6. **声纹自适应**：每次识别成功的高分片段自动补进质心（解决时间漂移）

---

## 9. 常用命令

```bash
# 看桥接日志（含唤醒/打断/声纹判定）
docker logs -f xiaoai-bridge

# 改了 config.py / event.py 后必须重建
cd stack && docker compose up -d --force-recreate bridge

# 改了网页/声纹服务
docker compose restart dialog-web speaker-web voiceprint

# 改了 .config.yaml（服务端配置）
docker compose restart xiaozhi-server

# 看声纹识别实时结果
docker logs -f xiaoqi-voiceprint

# 唤醒词离线回归（容器内跑）
docker exec xiaoai-bridge /app/.venv/bin/python /tmp/kwseval/kws_verify.py

# 声纹数据观察
docker exec xiaoai-bridge /app/.venv/bin/python /app/speaker_report.py

# 备份
tar czf backup-$(date +%F).tgz deploy/xiaoqi/logs deploy/xiaoqi/xiaozhi/data/.config.yaml deploy/xiaoqi/bridge
```

---

## 10. 一次性验证过的关键数字

留作回归基线：

| 指标 | 值 |
|---|---|
| 唤醒词命中（你好小七） | 4/4（打分 3.0 / 阈值 0.20） |
| 唤醒提示语时长 | 0.53 s（24k） |
| 静音窗口 | ≈1.3 s（提示语 + 0.8 s） |
| ASR → 首句音频延迟 | ≈1.6 s（整体 2~3 s） |
| AEC 回声抑制 | ≈27 dB |
| 打断近端值 | +7.5 / −1.5 dB |
| 远场语音 SNR（ch0） | 4.8 dB |
| 声纹 embedding 耗时 | ≈110 ms / 句 |
| 声纹同人相似度（设备录） | 0.50 ~ 0.76 |
| 声纹异人/杂音 | ≤0.38 |
| 设备内存 | 118 MB 总 / 常态剩 5~8 MB |
