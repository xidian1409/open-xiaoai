import asyncio


async def before_wakeup(speaker, text, source):
    """
    处理收到的用户消息，并决定是否唤醒小智 AI

    - source: 唤醒来源
        - 'kws': 关键字唤醒
        - 'xiaoai': 小爱同学收到用户指令
    """
    if source == "kws":
        # 提示语走桥接自己的音频通道（24 kHz PCM buffer），不是设备本地 TTS。
        # 本地 TTS（tts_play.sh -> miplayer）的声音回采根本消不掉。
        #
        # 但走 PCM 通道也不够：AEC 只能削 ~27 dB，而 Silero 是幅度归一化的，
        # 削过的残留照样打 0.9+ 分，和真人说话分不开（见 vad_hook.py 里的实测）。
        # 所以这里在播放的同时把 VAD 直接掐掉，播完再开始收音——
        # 否则每唤醒一次就会多出一轮"我说：你好主人"的幽灵对话。
        pcm = _prompt_pcm()
        asyncio.ensure_future(speaker.play(buffer=pcm))
        try:
            from xiaozhi.services.audio.vad import mute_for_prompt

            # 24 kHz 单声道 s16le：每毫秒 48 字节。多留 0.6 s 余量，
            # 并且会自动延长到音箱回采报告的播放结束点。
            mute_for_prompt(len(pcm) / 48 / 1000 + 0.8)
        except Exception as error:  # noqa: BLE001
            print(f"⚠️ 提示语静音窗口设置失败: {error}", flush=True)
        return True

    if source == "xiaoai" and text == "召唤小智":
        # 打断原来的小爱同学
        await speaker.abort_xiaoai()
        # 等待 2 秒，让小爱 TTS 恢复可用
        await asyncio.sleep(2)
        # 播放唤醒提示语（如果你不使用自带的小爱 TTS，可以去掉上面的延时）
        await speaker.play(text="小智来了，主人有什么吩咐？")
        # 唤醒小智 AI
        return True


async def after_wakeup(speaker):
    """退出唤醒时道别。

    走桥接的 PCM 通道（和小智正常回复同一条链路、同一个 EdgeTTS 音色）。
    原来用 speaker.play(text=...) 会调设备自带的 /usr/sbin/tts_play.sh，
    那是小米自己的语音引擎，音色和回复完全不是一个人。
    """
    pcm = _farewell_pcm()
    if not pcm:
        print("⚠️ 没有 farewell.pcm，退回设备自带 TTS（音色会不一样）", flush=True)
        await speaker.play(text="主人再见，拜拜")
        return
    seconds = len(pcm) / 48 / 1000
    print(f"👋 播放告别语 {seconds:.2f}s（EdgeTTS 同音色）", flush=True)
    # 告别语还在说，灯环就按"在说"的蓝色呼吸亮着，说完再灭。
    try:
        from xiaozhi.ref import get_xiaozhi

        get_xiaozhi()._led("speak")
    except Exception:  # noqa: BLE001
        pass
    await speaker.play(buffer=pcm)
    # play(buffer=) 是塞进输出流就返回，这里等它播完再结束会话。
    await asyncio.sleep(seconds + 0.2)
    try:
        from xiaozhi.ref import get_xiaozhi

        get_xiaozhi()._led("off")
    except Exception:  # noqa: BLE001
        pass


APP_CONFIG = {
    "wakeup": {
        # 自定义唤醒词列表（英文字母要全小写）
        # 2026-11-02 用 4 条真实"你好小七"录音 + 74 条真实负样本离线复测：
        #   "你好小七" 打分 3.0 / 阈值 0.20 → 4/4 全中，误唤 1 条（"你好小齐"，同音）
        #   "小七小七" 同样条件下          → 只有 1/4
        # 叠词对这个小模型明显更难，所以主唤醒词换回"你好小七"，
        # 叠词留在后面当备用，习惯性喊了也能醒。
        "keywords": [
            "你好小七",
            "小七小七",
        ],
        # 唤醒关键词的打分激励，越大越容易命中（上游默认 2.0）。
        # 实测 2.0 → 3/4，3.0 → 4/4，再往上误唤明显变多。
        "keywords_score": 3.0,
        # 唤醒触发阈值，越小越容易唤醒（上游默认 0.2）。
        "keywords_threshold": 0.20,
        # 单独调整某个唤醒词的检测阈值（0-1，越小越容易唤醒，也越容易误唤醒），
        # 例如 {"你好小智": 0.1}；没列出的词使用上面的 keywords_threshold。
        "keyword_thresholds": {"小七小七": 0.30},
        # 唤醒检测保留的候选数，越大越不容易漏，也越耗 CPU（默认 8）。
        # 实测 16 反而掉召回（同一批音频 4/4 → 2/4），保持上游默认 8。
        "max_active_paths": 8,
        # 同时运行几路唤醒检测，各路起点错开多少毫秒；任一路认出即唤醒。
        # 模型按 320 ms 一块处理音频，错开半块的两路漏掉的不是同一批；[0] 只用一路。
        "stream_offsets_ms": [0],
        # 静音多久后自动退出唤醒（秒）
        # 连续对话的倾听窗口：每轮回答完都接着听这么久，有人说话就续上，
        # 一直没人说话才静默退出。5 秒够接话，又不会让麦克风一直开着。
        "timeout": 5,
        # 对话中只说这些话（可带“好的”“谢谢”或唤醒词，如“你好小七，拜拜”）时马上退出唤醒，
        # 不再等超时；小七说话时可以先喊唤醒词打断再说。“关灯，拜拜”这样带着请求的，
        # 回答完再退出（见 README）。设成 [] 关闭。
        "exit_words": ["拜拜", "再见", "退出", "退下", "没事了", "不用了", "结束对话", "bye", "goodbye"],
        # 退出播报后的余音保护时间（ms），结束后清理唤醒识别状态。
        "exit_guard_ms": 300,
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
    },
    # 声纹认人（只影响"记录/称呼"，不做权限）
    # 实测：设备登记后真人语音相似度 0.50~0.76，杂音 <=0.38，取 0.45 居中。
    # 不确定时宁可说"没认出来"，也不要把人叫错。
    "speaker": {
        "enabled": True,
        "threshold": 0.45,
        # 太短的句子不算（声纹在 1 秒以下很不稳）
        "min_seconds": 1.0,
        # 同一轮对话里刚确认过这个人，紧接着那句可以放宽到 0.38
        "carry_threshold": 0.38,
        "carry_window_s": 120,
    },
    "vad": {
        # 语音检测阈值（0-1，越小越灵敏）
        "threshold": 0.10,
        # 最小语音时长（ms）
        "min_speech_duration": 250,
        # 最小静默时长（ms）
        "min_silence_duration": 700,
        # 只说了很短一句（如“啊”“嗯”）时，多等一会儿再判定说完，
        # 这样“啊……国庆不是八天”不会被截成“啊”。
        "short_utterance_duration": 800,
        "short_utterance_silence": 1300,
        # 小七说完后只留这段时间避开余音，然后马上开始听（ms）
        # 送去识别前，往回调这么多毫秒补回开口被吃掉的部分
        "prepend_lookback_ms": 600,
        "tts_end_guard_ms": 300,
        # 说完后服务端多久没有识别结果，就提示重说并重新开始听（秒）
        "no_reply_timeout": 5,
        # 没识别出内容时不要出声打扰，静默重新开始听（连错两次就安静退出）
        "no_reply_prompt": "",
    },
    "barge_in": {
        # 小七说话时喊唤醒词可以打断她，然后直接说新的问题（需要 echo_reference）。
        "enabled": True,
        # 让音箱同时发回它正在播放的声音，桥接据此消除回声。需要本仓库的音箱客户端；
        # 旧客户端会忽略这个请求，照旧发送单声道录音（此时不能打断）。
        # LX06 实测可用（2026-11-02）：pcm=noop、8ch/48k、mic=ch0、ref=ch6、
        # mic_shift=ref_shift=16、echo_canceller.output_gain=256。
        # 打断会消掉 27 dB 回声，小七说话时喊唤醒词能立刻接管。
        "echo_reference": True,
        # 打断后播放的提示音（音箱上的文件，留空则不播放）
        "prompt_tone": "/usr/share/common_sound/wakeup.opus",
        # 一口气说“你好小七，明天天气怎么样”时，唤醒词认出时已经过去一小段，
        # 从认出的位置往回多取这么多毫秒接着听，不丢开头。
        "keyword_tail_ms": 100,
        # 送去识别的声音再往前多取这么多毫秒（唤醒词结束到被认出最多晚约 0.4 s；
        # 带上一点“七”的尾音不影响识别）。
        "speech_lookback_ms": 500,
        # 刚放过声音的那一小段（播放中、停播后 echo_vad_hangover_ms 内），消除后剩下的小七的声音
        # 仍可能被当成说话；这段时间里说话检测用这个更高的阈值（真人说话约 1.0）。
        "echo_vad_threshold": 0.7,
        "echo_vad_hangover_ms": 300,
        # 打断时比较“回声消除后剩下的声音”和“消掉的回声”（dB）。设成数字（如 -17）后，
        # 低于它的唤醒当作小七自己的声音忽略；None 只记录在健康日志里。
        # 打断时比较"消掉回声后剩下的人声"和"被消掉的回声"（dB，通常为负）：
        # 低于这个值就当是小七自己的声音漏进来，忽略这次唤醒。
        # aec.py 的实测值：真人 3 米插话约 -13 dB，小七自己漏过来约 -20 dB，
        # 所以门槛取中间的 -17。
        # ⚠️ 这里原来写的是 8.0（正数），把**所有**打断都判成"自己的声音"了——
        # 打断一直是失效的，原因就在这个值。
        "near_end_min_db": -17.0,
        # 音箱录音：一个麦克风 + 播放回采声道（OH2P 的 hw:0,3 第 4 声道）
        "capture": {
            # LX06：noop = plug -> dsnoop(hw:0,3)，8 声道 48k S32
            # ch0-5 是麦克风阵列，ch6/ch7 是播放回采
            "pcm": "noop",
            "channels": 8,
            "sample_rate": 48000,
            "mic_channel": 0,
            "ref_channel": 6,
            "mic_shift": 16,
            "ref_shift": 16,
        },
        "echo_canceller": {
            # 回声尾巴长度（帧，每帧 16 ms）；8 帧在 3 米、音量 75 时也能认出唤醒词
            "taps": 8,
            # 回声路径的平均时长（秒）
            "tau_s": 3.0,
            # 消除后放大多少倍，回到原来单声道录音的音量
            "output_gain": 256,
        },
    },
    "music": {
        # 小七放本地音乐：“播放梁静茹的勇气”“放首歌”“暂停”“下一首”“继续播放”“这是什么歌”。
        # 音乐目录挂载到容器里的 library（见 deploy/xiaozhi/docker-compose.yml）。
        # 音箱用自带播放器从桥接拉歌；只在 OH2P 上验证过。
        "enabled": False,
        "library": "/music",
        # 音箱从这个端口拉歌，xiaozhi 的 provider 和 Hermes 也从这里点歌
        "http_port": 4400,
        # 音箱访问桥接的地址，如 "http://192.168.5.10:4400"；留空则用音箱连桥接时的地址
        "base_url": "",
        # 放歌时喊“你好小七”，音乐降低多少分贝，对话结束再恢复；小七的声音不受影响
        "duck_db": 20,
        # 音乐比小七的声音低多少分贝（0 为一样响）
        "music_db": 0,
        # 能放的格式，同一首歌有多个文件时按这个顺序挑（音箱放不了 wma）
        "formats": ["mp3", "flac", "wav", "m4a"],
        # 不收进曲库的文件夹
        "exclude_dirs": ["Podcasts", "Lyrics", "Images", "Album Artwork"],
        # 同一位歌手的别名，例如 {"田馥甄": ["Hebe"]}
        "aliases": {},
        # 小七的回答，可以改；留空则不说话（例如 "stop": ""）
        "replies": {},
    },
    "tts_output": {
        # 可选："sherpa"（服务端音频流）或 "native_xiaomi"（音箱原生音色）
        # Docker 部署可通过 XIAOZHI_TTS_OUTPUT_MODE 环境变量覆盖。
        "mode": "native_xiaomi",
        "native_xiaomi": {
            # 播放改走 PCM 通道后不再需要"合并成一段省去重起播放器"，
            # 反而要切小一点：句子越短合成越快，预取才跟得上，不会中途断流。
            "target_chars": 12,
            "max_chars": 60,
            "flush_delay_ms": 80,
            # 已合成但尚未播放的最大段数（句子切小后可以多预取几段，抵抗设备卡顿）。
            "prefetch_segments": 4,
            "generate_timeout_ms": 5000,
            "play_timeout_ms": 10 * 60 * 1000,
            # miplayer 偶发不退出时，按音频时长动态终止播放。
            "play_min_timeout_ms": 15000,
            "play_timeout_grace_ms": 8000,
            "max_file_bytes": 8 * 1024 * 1024,
            "max_total_bytes": 8 * 1024 * 1024,
        },
    },
    "xiaozhi": {
        # ⚠️ 占位示例，换成你自己 NAS 的地址
        "OTA_URL": "http://192.168.1.100:8003/xiaozhi/ota/",
        "WEBSOCKET_URL": "ws://192.168.1.100:8000/xiaozhi/v1/",
        "WEBSOCKET_ACCESS_TOKEN": "", #（可选）一般用不到这个值
        "VERIFICATION_CODE": "", # 首次对话时，验证码会在这里更新
        "DEVICE_ID": "", # 如果没有提示绑定设备，则将 DEVICE_ID 清空后，重启应用再次尝试
    },
}


_PROMPT_PCM = None


def _prompt_pcm():
    """唤醒提示语的 PCM（24k 单声道 s16le），首次调用时读盘。

    采样率必须和设备的播放通道一致（24 kHz），否则会变速变调，听起来"换了个人"。
    """
    global _PROMPT_PCM
    if _PROMPT_PCM is None:
        try:
            with open("/app/prompt.pcm", "rb") as f:
                _PROMPT_PCM = f.read()
        except OSError:
            _PROMPT_PCM = b""
    return _PROMPT_PCM


_FAREWELL_PCM = None


def _farewell_pcm():
    """退出告别的 PCM（24k 单声道 s16le），由 gen_prompts 用服务端同一音色生成。"""
    global _FAREWELL_PCM
    if _FAREWELL_PCM is None:
        try:
            with open("/app/farewell.pcm", "rb") as f:
                _FAREWELL_PCM = f.read()
        except OSError:
            _FAREWELL_PCM = b""
    return _FAREWELL_PCM


def prompt_seconds() -> float:
    """唤醒提示语有多长（秒）。24 kHz 单声道 s16le：每毫秒 48 字节。"""
    return len(_prompt_pcm()) / 48 / 1000
