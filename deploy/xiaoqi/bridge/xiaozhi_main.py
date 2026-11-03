import asyncio
import json
import re
import threading
import time

from config import APP_CONFIG
from xiaozhi.event import EventManager, exit_intent
from xiaozhi.ref import set_xiaozhi
from xiaozhi.services.audio.kws import KWS
from xiaozhi.services.audio.vad import VAD
from xiaozhi.services.music import start_music
from xiaozhi.services.native_tts import (
    NativeTTSSettings,
    NativeXiaomiTTS,
    resolve_tts_output_mode,
)
from xiaozhi.services.protocols.typing import (
    AbortReason,
    DeviceState,
    EventType,
    ListeningMode,
)
from xiaozhi.services.protocols.websocket_protocol import WebsocketProtocol
from xiaozhi.utils.base import get_env
from xiaozhi.utils.config import ConfigManager
from xiaozhi.xiaoai import XiaoAI


class XiaoZhi:
    """智能音箱应用程序主类"""

    _instance = None

    @classmethod
    def instance(cls):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = XiaoZhi()
        return cls._instance

    def __init__(self):
        """初始化应用程序"""
        # 确保单例模式
        if XiaoZhi._instance is not None:
            raise Exception("XiaoZhi是单例类，请使用instance()获取实例")
        XiaoZhi._instance = self

        # 获取配置管理器实例
        self.config = ConfigManager.instance()
        tts_output_config = APP_CONFIG.get("tts_output", {})
        self.tts_output_mode = resolve_tts_output_mode(
            tts_output_config,
            get_env("XIAOZHI_TTS_OUTPUT_MODE"),
        )
        native_config = tts_output_config.get("native_xiaomi", {})
        self.native_tts = NativeXiaomiTTS(
            XiaoAI.speaker,
            NativeTTSSettings.from_config(native_config),
        )
        self._native_finish_task = None
        # After a barge-in the server may still be sending the interrupted
        # reply; drop its TTS until it recognizes what the user says next.
        self._tts_gate = False
        print(f"🔈 TTS 输出模式: {self.tts_output_mode}")

        # 状态变量
        self.device_state = DeviceState.IDLE
        self.voice_detected = False
        self.current_text = ""
        self.current_emotion = "neutral"

        # 音频处理相关
        self.audio_codec = None

        # 事件循环和线程
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.running = False

        # 任务队列和锁
        self.main_tasks = []
        self.mutex = threading.Lock()

        # 协议实例
        self.protocol = None

        # 回调函数
        self.on_state_changed_callbacks = []

        # 初始化事件对象
        self.events = {
            EventType.SCHEDULE_EVENT: threading.Event(),
            EventType.AUDIO_INPUT_READY_EVENT: threading.Event(),
        }

        # 创建显示界面
        self.display = None
        set_xiaozhi(self)

    def run(self):
        self.protocol = WebsocketProtocol()

        # 创建并启动事件循环线程
        self.loop_thread = threading.Thread(target=self._run_event_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        # 等待事件循环准备就绪
        time.sleep(0.1)

        # 初始化应用程序
        asyncio.run_coroutine_threadsafe(XiaoAI.init_xiaoai(), self.loop)
        asyncio.run_coroutine_threadsafe(self._initialize_xiaozhi(), self.loop)

        # 启动主循环线程
        main_loop_thread = threading.Thread(target=self._main_loop)
        main_loop_thread.daemon = True
        main_loop_thread.start()

        VAD.start()
        KWS.start()

        if get_env("CLI"):
            # 小七放本地音乐（见 config.py 的 music）
            start_music(
                APP_CONFIG.get("music"), self.loop, XiaoAI.run_shell,
                EventManager.session_active, EventManager.request_end_after_reply,
                APP_CONFIG.get("wakeup", {}).get("keywords", ()),
            )

        # 启动 GUI
        self._initialize_display()
        self.display.start()

    def _run_event_loop(self):
        """运行事件循环的线程函数"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _initialize_xiaozhi(self):
        """初始化应用程序组件"""

        # 初始化音频编解码器
        self._initialize_audio()

        # 设置协议回调
        self.protocol.on_network_error = self._on_network_error
        self.protocol.on_incoming_audio = self._on_incoming_audio
        self.protocol.on_incoming_json = self._on_incoming_json
        self.protocol.on_audio_channel_opened = self._on_audio_channel_opened
        self.protocol.on_audio_channel_closed = self._on_audio_channel_closed

        # 打开音频通道
        self.device_state = DeviceState.CONNECTING
        await self.protocol.open_audio_channel()

    def _initialize_audio(self):
        """初始化音频设备和编解码器"""
        try:
            from xiaozhi.services.audio.codec import AudioCodec

            self.audio_codec = AudioCodec()
        except Exception as e:
            self.alert("错误", f"初始化音频设备失败: {e}")

        threading.Thread(target=self._audio_input_event_trigger, daemon=True).start()

    def _initialize_display(self):
        """初始化显示界面"""
        if get_env("CLI"):
            from xiaozhi.services.display import no_display

            self.display = no_display.NoDisplay()
        else:
            from xiaozhi.services.display import gui_display

            self.display = gui_display.GuiDisplay()

        # 设置回调函数
        self.display.set_callbacks(
            press_callback=self.start_listening,
            release_callback=self.stop_listening,
            status_callback=self._get_status_text,
            text_callback=self._get_current_text,
            emotion_callback=self._get_current_emotion,
            mode_callback=self._on_mode_changed,
            auto_callback=self.toggle_chat_state,
            abort_callback=lambda: self.abort_speaking(AbortReason.WAKE_WORD_DETECTED),
        )

    def _main_loop(self):
        """应用程序主循环"""
        self.running = True

        while self.running:
            # 等待事件
            for event_type, event in self.events.items():
                if event.is_set():
                    event.clear()

                    if event_type == EventType.AUDIO_INPUT_READY_EVENT:
                        self._handle_input_audio()
                    elif event_type == EventType.SCHEDULE_EVENT:
                        self._process_scheduled_tasks()

            time.sleep(0.01)

    def _process_scheduled_tasks(self):
        """处理调度任务"""
        with self.mutex:
            tasks = self.main_tasks.copy()
            self.main_tasks.clear()

        for task in tasks:
            try:
                task()
            except Exception:
                pass

    def schedule(self, callback):
        """调度任务到主循环"""
        with self.mutex:
            # 如果是中止语音的任务，检查是否已经存在相同类型的任务
            if "abort_speaking" in str(callback):
                # 如果已经有中止任务在队列中，就不再添加
                if any("abort_speaking" in str(task) for task in self.main_tasks):
                    return
            self.main_tasks.append(callback)
        self.events[EventType.SCHEDULE_EVENT].set()

    def _handle_input_audio(self):
        """处理音频输入"""
        if self.device_state != DeviceState.LISTENING:
            return

        encoded_data = self.audio_codec.read_audio()
        if encoded_data and self.protocol and self.protocol.is_audio_channel_opened():
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_audio(encoded_data), self.loop
            )

    def _on_network_error(self, message):
        """网络错误回调"""
        print(f"🌐 网络连接异常，将自动重试：{message}")
        self.set_device_state(DeviceState.IDLE)

    def _on_incoming_audio(self, data):
        """接收音频数据回调"""
        if self.tts_output_mode == "native_xiaomi" or self._tts_gate:
            return
        if self.device_state == DeviceState.SPEAKING:
            self.audio_codec.write_audio(data)

    async def _on_incoming_json(self, json_data):
        """接收JSON数据回调"""
        try:
            if not json_data:
                return

            # 解析JSON数据
            if isinstance(json_data, str):
                data = json.loads(json_data)
            else:
                data = json_data

            # 处理不同类型的消息
            msg_type = data.get("type", "")
            if msg_type == "tts":
                await self._handle_tts_message(data)
            elif msg_type == "stt":
                self._handle_stt_message(data)
            elif msg_type == "llm":
                self._handle_llm_message(data)
        except Exception as error:
            print(f"❌ 处理服务端消息失败: {error}")

    def ignore_tts_until_stt(self):
        self._tts_gate = True

    async def _handle_tts_message(self, data):
        """处理TTS消息"""
        state = data.get("state", "")
        if self._tts_gate:
            if state in ("start", "sentence_start", "stop"):
                print(f"🔇 丢弃被打断回答的播报: {state} {data.get('text', '')}")
            return
        if state == "start":
            if self.tts_output_mode == "native_xiaomi":
                await self.native_tts.start(data.get("session_id"))
            EventManager.on_tts_start(data.get("session_id"))
            self.schedule(lambda: self._handle_tts_start())
        elif state == "stop":
            if self.tts_output_mode == "native_xiaomi":
                self._native_finish_task = asyncio.create_task(
                    self._finish_native_tts(data.get("session_id"))
                )
            else:
                EventManager.on_tts_end(data.get("session_id"))
                self.schedule(lambda: self._handle_tts_stop())
        elif state == "sentence_start":
            text = data.get("text", "")
            if text:
                print(f"🤖 小智：{text}")
                _log_dialog("assistant", text)
                # 最后一句是问句 -> 她在主动问用户，需要继续听
                stripped = text.strip()
                self._reply_asked = stripped.endswith(("？", "?", "吗", "呢"))

                if self.tts_output_mode == "native_xiaomi":
                    await self.native_tts.add_text(text)

                verification_code = re.search(r"验证码.*?(\d+)", text) or re.search(
                    r"控制面板.*?(\d+)", text
                )
                if verification_code:
                    self.config.update_config_file(
                        "VERIFICATION_CODE", verification_code.group(1)
                    )

                self.schedule(lambda: self.set_chat_message("assistant", text))

    async def _finish_native_tts(self, session_id):
        current_task = asyncio.current_task()
        try:
            completed = await self.native_tts.finish()
            if not completed:
                return
            if self.native_tts.error:
                print(f"⚠️ 原生小爱 TTS 会话存在错误: {self.native_tts.error}")

            # Clear the task reference before starting the next listening
            # session. Otherwise that session can race with this task and try
            # to cancel/await it from another callback path.
            if self._native_finish_task is current_task:
                self._native_finish_task = None
            EventManager.on_tts_end(session_id)
            self.schedule(lambda: self._handle_tts_stop())
        finally:
            if self._native_finish_task is current_task:
                self._native_finish_task = None

    async def abort_tts_output(self):
        if self.tts_output_mode != "native_xiaomi":
            return
        finish_task = self._native_finish_task
        self._native_finish_task = None
        if finish_task and not finish_task.done():
            finish_task.cancel()
            await asyncio.gather(finish_task, return_exceptions=True)
        await self.native_tts.abort()

    def _handle_tts_start(self):
        """处理TTS开始事件"""
        if (
            self.device_state == DeviceState.IDLE
            or self.device_state == DeviceState.LISTENING
        ):
            self.set_device_state(DeviceState.SPEAKING)

    def _handle_tts_stop(self):
        """处理TTS停止事件"""
        pass

    def _handle_stt_message(self, data):
        """处理STT消息"""
        text = _strip_fillers(_normalize_terms(data.get("text", "")))
        # 小爱式：回答完就退出；只有小七反问你时才继续听
        self._answer_pending = True
        self._reply_asked = False
        if text:
            print(f"💬 我说：{text}")
            if not text.startswith("%"):  # 工具调用不是用户说的话
                _log_dialog("user", text)
            intent, _ = exit_intent(text)
            if intent == "now":
                # The server answers "拜拜" too; that answer is not played.
                self._tts_gate = True
                EventManager.on_exit_words(text)
            else:
                self._tts_gate = False
                if intent == "after_answer":
                    print(f"👋 回答完就退出: {text}")
                EventManager.on_stt(exit_after_answer=intent == "after_answer")
            self.schedule(lambda: self.set_chat_message("user", text))
            EventManager._note_stt_ok()
            # 说"拜拜"这种立刻退出的，会话正在收尾（要说告别语），
            # 这里再切"在想"会把灯环点亮一下又灭，很跳。
            if intent != "now":
                self._led("think")

    def _handle_llm_message(self, data):
        """处理LLM消息"""
        emotion = data.get("emotion", "")
        if emotion:
            self.schedule(lambda: self.set_emotion(emotion))

    async def _on_audio_channel_opened(self):
        """音频通道打开回调"""
        self.set_device_state(DeviceState.IDLE)

    def _audio_input_event_trigger(self):
        """音频输入事件触发器"""
        while self.running:
            if self.audio_codec.input_stream.is_active():
                self.events[EventType.AUDIO_INPUT_READY_EVENT].set()
            time.sleep(0.01)

    async def _on_audio_channel_closed(self):
        """音频通道关闭回调"""
        await self.abort_tts_output()
        self.set_device_state(DeviceState.IDLE)
        self.audio_codec.stop_streams()

    def _led(self, mode: str) -> None:
        """把灯环切到某个动画（fire-and-forget，绝不等 RPC 回来）。

        "off" 延迟 0.35 秒再发：状态机在唤醒/切听的瞬间会先闪一次 IDLE，
        立刻关灯会让灯环黑一下再亮，很难看。
        """
        try:
            loop = getattr(self, "loop", None)
            if loop is None:
                return
            self._led_seq = getattr(self, "_led_seq", 0) + 1
            seq = self._led_seq
            if mode == "off":
                async def delayed_off():
                    await asyncio.sleep(0.35)
                    if getattr(self, "_led_seq", 0) == seq:
                        await self._send_led("off")
                asyncio.run_coroutine_threadsafe(delayed_off(), loop)
                return
            asyncio.run_coroutine_threadsafe(self._send_led(mode), loop)
        except Exception as error:  # noqa: BLE001 - 灯坏了不能影响对话
            print(f"⚠️ 灯环设置失败({mode}): {error}", flush=True)

    async def _send_led(self, mode: str) -> None:
        from xiaozhi.ref import get_speaker

        speaker = get_speaker()
        if speaker is None:
            return
        print(f"💡 灯环 → {mode}", flush=True)
        try:
            await speaker.run_shell(f"sh /data/open-xiaoai/led.sh {mode}", timeout=2000)
        except Exception as error:  # noqa: BLE001
            print(f"⚠️ 灯环下发失败({mode}): {error}", flush=True)

    def _schedule_led_timeout(self) -> None:
        """倾听窗口快结束时把灯环压暗，让人一眼看出"她快不听了"。"""
        try:
            loop = getattr(self, "loop", None)
            if loop is None:
                return
            window = float(APP_CONFIG.get("wakeup", {}).get("timeout", 5))
            # 窗口剩 2 秒时压暗（5 秒窗口 -> 听 3 秒亮、2 秒暗）。
            warn_at = max(0.8, window - 2.0)
            seq = getattr(self, "_led_seq", 0)

            async def later():
                await asyncio.sleep(warn_at)
                # 只认灯环序号：这期间只要又切过别的灯效（think/speak/off），
                # 序号就变了，压暗自动作废。
                if getattr(self, "_led_seq", 0) == seq:
                    await self._send_led("listen_low")

            asyncio.run_coroutine_threadsafe(later(), loop)
        except Exception:  # noqa: BLE001
            pass

    def listening_light(self) -> None:
        """倾听窗口亮灯：只动灯环，不动设备状态和音频流。

        原来只有检测到人开口（set_device_state(LISTENING)）才亮灯，所以
        "等她说话"的那 5 秒是黑的——看不出来她在听。这里单独点亮。
        """
        try:
            self.device_state = DeviceState.LISTENING
            self._led("listen")
            self._schedule_led_timeout()
        except Exception as error:  # noqa: BLE001
            print(f"⚠️ 倾听灯效设置失败: {error}", flush=True)

    def set_device_state(self, state):
        """设置设备状态"""
        self.device_state = state
        # 灯环跟着状态走：听=绿扫光，说=淡蓝呼吸，空闲=灭
        self._led({DeviceState.LISTENING: "listen",
                   DeviceState.SPEAKING: "speak",
                   DeviceState.CONNECTING: "busy",
                   DeviceState.IDLE: "off"}.get(state, "off"))
        if state == DeviceState.LISTENING:
            self._schedule_led_timeout()

        VAD.pause()  # 停用 VAD
        self.audio_codec.stop_streams()  # 停用输入输出流

        if state == DeviceState.IDLE:
            self.display.update_status("待命")
            self.display.update_emotion("😶")
        elif state == DeviceState.CONNECTING:
            self.display.update_status("连接中...")
        elif state == DeviceState.LISTENING:
            self.display.update_status("聆听中...")
            self.display.update_emotion("🙂")
            # 停止输出流
            if self.audio_codec.output_stream.is_active():
                self.audio_codec.output_stream.stop_stream()
            # 打开输入流
            if not self.audio_codec.input_stream.is_active():
                self.audio_codec.input_stream.start_stream()
        elif state == DeviceState.SPEAKING:
            self.display.update_status("说话中...")
            # 停止输入流
            if self.audio_codec.input_stream.is_active():
                self.audio_codec.input_stream.stop_stream()
            # 打开输出流
            if not self.audio_codec.output_stream.is_active():
                self.audio_codec.output_stream.start_stream()

        # 通知状态变化
        for callback in self.on_state_changed_callbacks:
            try:
                callback(state)
            except Exception:
                pass

    def _get_status_text(self):
        """获取当前状态文本"""
        states = {
            DeviceState.IDLE: "待命",
            DeviceState.CONNECTING: "连接中...",
            DeviceState.LISTENING: "聆听中...",
            DeviceState.SPEAKING: "说话中...",
        }
        return states.get(self.device_state, "未知")

    def _get_current_text(self):
        """获取当前显示文本"""
        return self.current_text

    def _get_current_emotion(self):
        """获取当前表情"""
        emotions = {
            "neutral": "😶",
            "happy": "🙂",
            "laughing": "😆",
            "funny": "😂",
            "sad": "😔",
            "angry": "😠",
            "crying": "😭",
            "loving": "😍",
            "embarrassed": "😳",
            "surprised": "😲",
            "shocked": "😱",
            "thinking": "🤔",
            "winking": "😉",
            "cool": "😎",
            "relaxed": "😌",
            "delicious": "🤤",
            "kissy": "😘",
            "confident": "😏",
            "sleepy": "😴",
            "silly": "😜",
            "confused": "🙄",
        }
        return emotions.get(self.current_emotion, "😶")

    def set_chat_message(self, role, message):
        """设置聊天消息"""
        self.current_text = message
        # 更新显示
        if self.display:
            self.display.update_text(message)

    def set_emotion(self, emotion):
        """设置表情"""
        self.current_emotion = emotion
        # 更新显示
        if self.display:
            self.display.update_emotion(self._get_current_emotion())

    def start_listening(self):
        """开始监听"""
        self.schedule(self._start_listening_impl)

    def _start_listening_impl(self):
        """开始监听的实现"""
        if not self.protocol:
            return

        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self._abort_output_and_notify_server(),
            self.loop,
        )
        asyncio.run_coroutine_threadsafe(
            self.protocol.send_start_listening(ListeningMode.MANUAL), self.loop
        )
        self.set_device_state(DeviceState.LISTENING)

    def stop_listening(self):
        """停止监听"""
        self.schedule(self._stop_listening_impl)

    def _stop_listening_impl(self):
        """停止监听的实现"""
        asyncio.run_coroutine_threadsafe(self.protocol.send_stop_listening(), self.loop)
        self.set_device_state(DeviceState.IDLE)

    def abort_speaking(self, reason):
        """中止语音输出"""
        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self._abort_output_and_notify_server(),
            self.loop,
        )

    async def _abort_output_and_notify_server(self):
        await self.abort_tts_output()
        await self.protocol.send_abort_speaking(AbortReason.ABORT)

    def alert(self, title, message):
        """显示警告信息"""
        if self.display:
            self.display.update_text(f"{title}: {message}")

    def on_state_changed(self, callback):
        """注册状态变化回调"""
        self.on_state_changed_callbacks.append(callback)

    def shutdown(self):
        """关闭应用程序"""
        self.running = False

        if self.loop and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self.abort_tts_output(), self.loop
            )
            try:
                future.result(timeout=2)
            except Exception:
                pass

        # 关闭音频编解码器
        if self.audio_codec:
            self.audio_codec.close()

        # 关闭协议
        if self.protocol:
            asyncio.run_coroutine_threadsafe(
                self.protocol.close_audio_channel(), self.loop
            )

        # 停止事件循环
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        # 等待事件循环线程结束
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1.0)

    def toggle_chat_state(self):
        """切换聊天状态"""
        pass

    def _on_mode_changed(self, auto_mode):
        """处理对话模式变更"""
        pass


# ---------------------------------------------------------------- 对话记录
def _log_dialog(role: str, text: str) -> None:
    """追加一条对话到 JSONL，供对话记录页读取。"""
    import json as _json
    import os
    import time

    try:
        path = os.environ.get("DIALOG_LOG", "/data/logs/dialog.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"ts": time.time(), "role": role, "text": text},
                                ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 对话记录失败: {exc}", flush=True)


# ---------------------------------------------------------------- 口误清理
_FILLERS = ("嗯嗯", "呃呃", "啊啊", "嗯", "呃", "额", "啊", "哦", "噢", "唉",
            "那个", "内个", "就是", "然后", "所以说")


def _strip_fillers(text: str) -> str:
    """去掉识别结果开头的口误填充词（"嗯现在几点" -> "现在几点"）。

    只动开头，且去掉之后必须还有内容，避免把"啊"这种正常语气词吃掉。
    """
    import re

    out = (text or "").lstrip(" ，,。.、！!？?~～")
    # 句首的"半个字 + 呃/额"（"今呃，现在的节气…"）把前面那一两个字一起丢掉
    m = re.match(r"^([\u4e00-\u9fff]{1})[呃额噢哦啊][，,、]?\s*(?=.{3,})", out)
    if m:
        out = out[m.end():]
    changed = True
    while changed and out:
        changed = False
        for word in _FILLERS:
            if out.startswith(word) and len(out) > len(word):
                rest = out[len(word):].lstrip(" ，,。.、！!？?~～")
                if any(ch.isalnum() for ch in rest):
                    out = rest
                    changed = True
                    break
    return out


# ---------------------------------------------------------------- 专有名词纠错
# SenseVoice 对英文术语很差（"DeepSeek" 会听成 "deepepick honey"），
# 这里把常见的几种写法归一化回正确词形，避免模型被不存在的词带偏。
_TERM_FIXES = (
    # 带 honey 的写法必须排在前面，否则只替换掉前半截
    (r"deepe?pick\s*(honey|huny|money)|deep\s*(seek|sick|pick|speak)\s*(honey|huny|money)", "DeepSeek"),
    (r"deep\s*seek|deep\s*sick|deep\s*pick|deep\s*speak|deepe?pick|深度求索", "DeepSeek"),
    (r"wai\s*fai|why\s*fai|wi\s*fi|wifi", "WiFi"),
    (r"home\s*assistant|home\s*ass?ist", "Home Assistant"),
    (r"nas|n\s*a\s*s", "NAS"),
    (r"rui|瑞\s*i", "rui"),
)


def _normalize_terms(text: str) -> str:
    """把听错的英文/专有名词纠正回来。只做词形替换，不改句子结构。"""
    import re as _re

    out = text or ""
    for pattern, target in _TERM_FIXES:
        out = _re.sub(pattern, target, out, flags=_re.IGNORECASE)
    return out
