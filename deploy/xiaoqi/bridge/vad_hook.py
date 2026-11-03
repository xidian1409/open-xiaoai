import threading
import time

from config import APP_CONFIG
from xiaozhi.event import EventManager
from xiaozhi.ref import get_xiaoai, set_vad
from xiaozhi.services.audio.stream import MyAudio
from xiaozhi.services.audio.vad.silero import Silero
from xiaozhi.services.protocols.typing import AudioConfig
from xiaozhi.utils.base import get_env

PCM_SAMPLE_WIDTH_BYTES = 2

# 唤醒提示语播放期间干脆不收音。
#
# 为什么不是"调阈值"：AEC 对提示语只能削 ~27 dB 回声，而 Silero VAD 是按幅度
# 归一化的——削掉 27 dB 的残留人声，它照样打 0.9+ 分。实测三轮回声录音
# 和真人说话打分完全重叠（回声中位帧 >0.9 的占比 11%~28%，真人 14%~29%），
# 任何阈值都分不开。所以只能在这段时间把麦克风直接掐掉。
_PROMPT_MUTE = {"on": False, "until": 0}


def mute_for_prompt(seconds: float) -> None:
    """接下来 seconds 秒内不把麦克风送进 VAD。

    实际时长会取 max(这个期限, 音箱回采报告的播放结束点)，所以设备
    起播偏晚、或者回采声道有延迟时也不会漏掉尾巴。
    """
    try:
        from xiaozhi.services.audio.stream import GlobalStream

        _PROMPT_MUTE["until"] = GlobalStream.samples + int(float(seconds) * 16000)
        _PROMPT_MUTE["on"] = True
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ 提示语静音窗口设置失败: {error}", flush=True)


def _prompt_muted(position) -> bool:
    """提示语还在播（或刚播完）时返回 True，并把 VAD 状态保持在干净状态。"""
    if not _PROMPT_MUTE["on"]:
        return False
    now = position
    if now is None:
        try:
            from xiaozhi.services.audio.stream import GlobalStream

            now = GlobalStream.samples
        except Exception:  # noqa: BLE001
            now = None
    if now is None:
        _PROMPT_MUTE["on"] = False
        return False
    until = max(_PROMPT_MUTE["until"], int(getattr(get_xiaoai(), "echo_until", -1)))
    if now < until:
        return True
    _PROMPT_MUTE["on"] = False
    return False


def frame_sample_count(frames: bytes) -> int:
    """Return mono S16_LE sample count instead of the raw byte count."""
    return len(frames) // PCM_SAMPLE_WIDTH_BYTES


class _VAD:
    def __init__(self):
        set_vad(self)

        config = APP_CONFIG.get("vad", {})

        # 参数设置
        self.sample_rate = 16000
        self.frame_size = 512
        self.threshold = config.get("threshold", 0.01)
        self.min_speech_duration = config.get("min_speech_duration", 250)
        self.min_silence_duration = config.get("min_silence_duration", 500)
        # A very short utterance ("啊", "嗯"...) is usually followed by the real
        # sentence after a pause, so it needs a longer silence before the turn
        # ends. Normal-length speech keeps min_silence_duration.
        self.short_utterance_duration = config.get("short_utterance_duration", 800)
        self.short_utterance_silence = config.get("short_utterance_silence", 1000)
        # Right after playback, what is left of 小七's voice after echo
        # cancellation still scores up to ~0.9 as speech; a caller scores
        # ~1.0. Frames that may hold that residue need the higher threshold.
        echo = APP_CONFIG.get("barge_in", {}).get("echo_vad_threshold")
        self.echo_threshold = self.threshold if echo is None else echo

        # 状态变量
        self.paused = True
        self.thread = None
        self.speech_count = 0
        self.silence_count = 0
        # Speech heard while waiting for the end of the current utterance.
        self.voiced_count = 0

        self.audio = None
        self.stream = None

        # Input sample just after the last frame read (None without positions).
        self.read_position = None

        # 暂存的语音片段
        self.silence_frames = []  # 静音片段
        self.speech_frames = []  # 语音片段
        self.target = None  # 检测目标 speech/silence

    def _reset_state(self):
        """重置状态"""
        self.speech_count = 0
        self.silence_count = 0
        self.voiced_count = 0
        self.speech_frames = []
        self.silence_frames = []

    def start(self):
        """启动VAD检测器"""
        if not get_env("CLI"):
            return

        self._initialize_audio_stream()

        # 启动检测线程
        self.paused = False
        self.thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.thread.start()

    def pause(self):
        """暂停VAD检测"""
        if not get_env("CLI"):
            return

        self.paused = True
        self._reset_state()
        self.stream.stop_stream()

    def resume(self, target: str, since=None):
        """恢复VAD检测

        since: 从这个输入采样点开始听（打断时接上唤醒词后面已经说出的话）。
        """
        if not get_env("CLI"):
            return

        if target == "speech":
            # A new listening turn starts from a fresh Silero state. Carried
            # over from earlier turns (it was never reset), it scored a clearly
            # spoken "现在冰箱几度" below the threshold after hours of running,
            # and the question was taken for silence. "silence" continues the
            # same utterance, so its state is kept.
            Silero.model.reset_states()

        self.paused = False
        self.target = target
        self.voiced_count = 0
        if since is None:
            self.stream.start_stream()
        else:
            self.stream.start_stream(since)

    def _handle_speech_frame(self, frames):
        """处理语音帧"""
        try:
            from xiaozhi.event import EventManager

            EventManager.note_voice()
        except Exception:
            pass
        self.speech_count += frame_sample_count(frames)
        self.silence_count = 0
        if self.target == "silence":
            self.voiced_count += frame_sample_count(frames)

        if self.target == "speech":
            if not self.speech_frames:
                # 加入静音片段（潜在的语音片段）
                self.speech_frames.extend(self.silence_frames)

        # 加入语音片段
        self.speech_frames.extend(frames)

        speech_bytes = bytes(self.speech_frames)

        if (
            self.target == "speech"
            and self.speech_count > self.min_speech_duration * self.sample_rate / 1000
        ):
            self.pause()
            EventManager.on_speech(speech_bytes)

    def _handle_silence_frame(self, frames):
        """处理静音帧"""
        self.silence_count += frame_sample_count(frames)
        self.speech_count = 0

        if self.target == "speech":
            if not self.speech_frames:
                # 如果之前没有语音片段，则将当前帧加入静音片段
                self.silence_frames.extend(frames)
                # 确保静音片段长度不超过 1s
                self.silence_frames = self.silence_frames[
                    -1 * 1 * 2 * self.sample_rate :
                ]
            else:
                # 如果之前有语音片段，则将当前帧加入语音片段
                self.speech_frames.extend(frames)

        if (
            self.target == "silence"
            and self.silence_count > self.required_silence() * self.sample_rate / 1000
        ):
            self.pause()
            EventManager.on_silence()

    def frame_threshold(self, position):
        """Speech threshold for the frame starting at input sample `position`."""
        if position is not None and position < getattr(get_xiaoai(), "echo_until", -1):
            return self.echo_threshold
        return self.threshold

    def required_silence(self):
        """Silence (ms) that ends the current utterance."""
        # on_speech fired after min_speech_duration of speech, so count it too.
        voiced_ms = self.min_speech_duration + self.voiced_count * 1000 / self.sample_rate
        if voiced_ms < self.short_utterance_duration:
            return max(self.min_silence_duration, self.short_utterance_silence)
        return self.min_silence_duration

    def _initialize_audio_stream(self):
        """初始化独立的音频流"""
        try:
            # 创建 PyAudio 实例
            self.audio = MyAudio.create()
            # 创建输入流
            self.stream = self.audio.open(
                format=AudioConfig.FORMAT,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.frame_size,
                start=True,
            )
            return True
        except Exception:
            return False

    def _close_audio_stream(self):
        """关闭音频流"""
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None

            if self.audio:
                self.audio.terminate()
                self.audio = None

        except Exception:
            pass

    def _detection_loop(self):
        """VAD检测主循环"""
        while True:
            # 如果暂停或者音频流未初始化，则跳过
            if self.paused or not self.stream:
                time.sleep(0.1)
                continue

            # 读取缓冲区音频数据
            position = self.stream.position() if hasattr(self.stream, "position") else None
            frames = self.stream.read(
                self.frame_size, exception_on_overflow=False
            )
            if len(frames) != self.frame_size * 2:
                time.sleep(0.01)
                continue

            self.read_position = None if position is None else position + len(frames) // 2

            if _prompt_muted(position):
                # 提示语还没播完：整帧丢弃，别让残留回声变成一轮"我说：你好主人"。
                self._reset_state()
                if not getattr(self, "_muted", False):
                    self._muted = True
                    print("🔇 提示语播放中，暂停收音", flush=True)
                time.sleep(0.01)
                continue
            if getattr(self, "_muted", False):
                self._muted = False
                Silero.model.reset_states()
                # 提示语这 3 秒多没有"人声"，倒计时没被续期；这里补一次，
                # 让用户听到提示语之后仍然有完整的倾听窗口。
                try:
                    EventManager.note_voice()
                except Exception:  # noqa: BLE001
                    pass
                print("🎙️ 提示语结束，开始收音", flush=True)

            # 检测是否是语音
            speech_prob = Silero.vad(frames, self.sample_rate) or 0
            is_speech = speech_prob >= self.frame_threshold(position)
            if is_speech:
                self._handle_speech_frame(frames)
            else:
                self._handle_silence_frame(frames)

            time.sleep(0.01)


VAD = _VAD()
