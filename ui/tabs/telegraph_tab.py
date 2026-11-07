"""
财联电报模块 - 财联社电报实时快讯 + 自动语音读报

语音引擎：
  1. edge-tts（微软神经语音，在线，音质自然甜美）-> 生成 mp3 用 QMediaPlayer 播放
  2. QTextToSpeech（系统 SAPI，离线兜底）
"""
import asyncio, os, re, tempfile, time, uuid
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QListWidget, QListWidgetItem, QCheckBox, QMessageBox, QComboBox)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QTimer, QLocale, QUrl
from PyQt6.QtGui import QColor
from data_fetcher import MultiSourceFetcher, new_async_loop
from ui.thread_utils import keep_alive

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
except Exception:
    QMediaPlayer = QAudioOutput = None

try:
    from PyQt6.QtTextToSpeech import QTextToSpeech
except Exception:  # 语音模块缺失时降级为无语音
    QTextToSpeech = None

# edge-tts 是否可用（在线神经语音）
try:
    import edge_tts
    _EDGE_AVAILABLE = True
except Exception:
    edge_tts = None
    _EDGE_AVAILABLE = False

# 消息级别 → 颜色（财联社 A 重要 / B 次重要 / C 普通）
_LEVEL_COLOR = {
    'A': '#E74C3C',
    'B': '#F39C12',
    'C': '#9BD0FF',
}
_LEVEL_TAG = {'A': '【重要】', 'B': '【关注】', 'C': ''}

# 在线神经语音（微软 Edge TTS，ShortName -> 中文名）——下拉列表仅保留用户常用三选
_EDGE_VOICES = [
    ("晓晓（温柔甜美）", "zh-CN-XiaoxiaoNeural"),
    ("香港晓佳（粤语）", "zh-HK-HiuGaaiNeural"),
]


class TelegraphFetchThread(QThread):
    result_ready = pyqtSignal(list)

    def run(self):
        try:
            loop = new_async_loop()
            asyncio.set_event_loop(loop)

            async def do():
                async with MultiSourceFetcher() as f:
                    return await f.fetch_telegraph(count=30)

            r = loop.run_until_complete(do()); loop.close()
            self.result_ready.emit(r if isinstance(r, list) else [])
        except Exception:
            self.result_ready.emit([])


class TTSSpeechThread(QThread):
    """后台线程：edge-tts 合成语音 -> 临时 mp3 文件"""
    done = pyqtSignal(str, str)  # (mp3_path, error)；error 非空表示失败

    def __init__(self, text, voice):
        super().__init__()
        self._text = text
        self._voice = voice

    def run(self):
        mp3_path = ''
        error = ''
        try:
            import edge_tts
            loop = new_async_loop()
            asyncio.set_event_loop(loop)

            async def do():
                comm = edge_tts.Communicate(self._text, self._voice)
                await comm.save(mp3_path)

            # 语音文件统一存放：%TEMP%\RaccoonQuant\voice
            voice_dir = os.path.join(tempfile.gettempdir(), 'RaccoonQuant', 'voice')
            os.makedirs(voice_dir, exist_ok=True)
            # 控制目录体积：仅保留最近 100 个语音文件，超出则删除最旧的
            try:
                files = sorted(
                    (os.path.join(voice_dir, f) for f in os.listdir(voice_dir)
                     if f.endswith('.mp3')),
                    key=os.path.getmtime, reverse=True)
                for old in files[100:]:
                    try:
                        os.remove(old)
                    except Exception:
                        pass
            except Exception:
                pass
            mp3_path = os.path.join(voice_dir, f'voice_{uuid.uuid4().hex}.mp3')
            loop.run_until_complete(do()); loop.close()
        except Exception as e:
            error = str(e)
            if mp3_path and os.path.exists(mp3_path):
                try:
                    os.remove(mp3_path)
                except Exception:
                    pass
            mp3_path = ''
        self.done.emit(mp3_path, error)


class TelegraphTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []          # [{id, time, title, content, level}] 新→旧
        self._ids = set()         # 已展示 id
        self._text_keys = set()   # 无 id 条目去重键 (time, title)
        self._first_load = True   # 首次加载不朗读，避免启动轰炸
        self._fetch_thread = None
        self._tts = None
        self._speech_thread = None
        self._player = None
        self._zombie_threads = []  # 保留已终止但可能未完全结束的线程引用，防止 "QThread destroyed while running"
        self._audio_path = None
        self._speak_queue = []             # 待朗读文本队列（逐条朗读，避免互相打断）
        self._reading = False              # 显式朗读占用标志（事件驱动，避免瞬时状态竞争）
        self._edge_stage = None            # edge 阶段：'synth' 合成中 / 'play' 播放中 / None 空闲
        self._sapi_started = False         # SAPI 本次 say() 是否已真正发声（区分初始 Ready 与读完 Ready）
        self._sapi_ts = 0.0                # SAPI 最近一次 say() 的时间戳（看门狗防误判用）
        self._voice_kind = 'sapi'          # 默认系统语音（微软 Yaoyao）
        self._edge_voice = _EDGE_VOICES[0][1] if _EDGE_AVAILABLE else None  # 在线兜底：晓晓
        self._sapi_voice = None
        # 语音引擎（QMediaPlayer/QTextToSpeech）延迟到首次朗读时再创建：
        # 它们的后台 C++ 线程（DirectShow/SAPI COM）在启动时即创建会在不稳定的
        # 环境（打包 exe、无音频设备、SAPI 异常）下触发 access violation 崩溃
        self._speech_ready = False
        self._init_ui()
        # 兜底看门狗：SAPI/edge 若因信号未触发而卡住朗读，每 800ms 检查一次强制恢复
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._watch)
        self._watch_timer.start(800)
        # 自动刷新：每 30 秒
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self.refresh)
        self._auto_timer.start(30000)
        self.refresh()

    # ---------------- 线程安全 ----------------
    def _track_thread(self, t):
        """跟踪线程生命周期：结束时自动清理引用，避免 QThread 销毁时仍在运行（幂等）"""
        if not isinstance(t, QThread):
            return
        if t not in self._zombie_threads:
            self._zombie_threads.append(t)
        try:
            t.finished.connect(lambda: self._on_thread_finished(t))
        except (TypeError, RuntimeError):
            pass

    def _on_thread_finished(self, t):
        """线程自然结束后：释放当前引用并安排 Qt 侧销毁"""
        if self._fetch_thread is t:
            self._fetch_thread = None
        if self._speech_thread is t:
            self._speech_thread = None
        try:
            self._zombie_threads.remove(t)
        except ValueError:
            pass
        try:
            t.deleteLater()
        except Exception:
            pass

    def _kill_thread(self, t):
        """安全终止线程：委托 thread_utils.kill_thread（terminate 后循环等待、
        超时二次终止并登记到全局引用池），退出时由 shutdown_all 统一兜底"""
        from ui.thread_utils import kill_thread as _safe_kill
        if not isinstance(t, QThread):
            return
        if t.isRunning():
            _safe_kill(t)
        if t.isRunning():  # 仍未结束：保留本 tab 引用，由 finished 信号兜底回收
            self._track_thread(t)
        else:
            self._on_thread_finished(t)

    # ---------------- 语音 ----------------
    def _ensure_speech(self):
        """延迟初始化语音引擎（首次朗读时才创建 QMediaPlayer/QTextToSpeech）。
        避免启动即创建多媒体后台线程导致 access violation 崩溃。幂等。"""
        if self._speech_ready:
            return
        self._speech_ready = True
        try:
            self._init_speech()
        except Exception:
            self._tts = None
            self._player = None
        # 若无可用系统语音（Yaoyao 等），则回退到在线神经语音
        if self._voice_kind == 'sapi' and self._tts is None and _EDGE_AVAILABLE:
            self._voice_kind = 'edge'
        # SAPI 语音此时才可用，重建语音下拉框以显示「系统·微软Yaoyao」选项
        try:
            self._fill_voices()
        except Exception:
            pass

    def _init_speech(self):
        # 播放器（播放 edge-tts 生成的 mp3）
        if QMediaPlayer is not None:
            try:
                self._player = QMediaPlayer(self)
                self._audio_output = QAudioOutput(self)
                self._player.setAudioOutput(self._audio_output)
                self._player.mediaStatusChanged.connect(self._on_media_status)
            except Exception:
                self._player = None
        # 系统 SAPI 兜底
        if QTextToSpeech is None:
            self._tts = None
            return
        try:
            self._tts = QTextToSpeech(self)
            for loc in self._tts.availableLocales():
                if loc.language() == QLocale.Language.Chinese:
                    self._tts.setLocale(loc)
                    break
            # 优先选择微软 Yaoyao（丫丫/瑶瑶，系统内置女声）
            voices = self._tts.availableVoices()
            for v in voices:
                try:
                    nm = v.name() or ''
                except Exception:
                    nm = ''
                if 'yaoyao' in nm.lower():
                    self._tts.setVoice(v)
                    self._sapi_voice = v
                    break
            # SAPI 朗读完一条（回到 Ready）后，自动开始读下一条
            self._tts.stateChanged.connect(self._on_sapi_state)
        except Exception:
            self._tts = None

    def _fill_voices(self):
        """下拉框：仅保留「系统·微软Yaoyao / 在线·晓晓 / 在线·香港晓佳」三选。
        系统·微软Yaoyao 固定放在第一位——即使 SAPI 引擎延迟到首次朗读才创建
        （_tts 尚为 None），也始终占位显示该选项；选择后朗读时自动选用系统语音。"""
        self.voice_combo.clear()
        # 1) 系统 SAPI 语音：微软 Yaoyao（固定第一位）
        sapi_voice = None
        if self._tts is not None:
            try:
                voices = self._tts.availableVoices()
            except Exception:
                voices = []
            for v in voices:
                try:
                    nm = v.name() or str(v)
                except Exception:
                    nm = str(v)
                if 'yaoyao' in nm.lower():
                    sapi_voice = v
                    break
        self.voice_combo.addItem("系统·微软Yaoyao", ('sapi', sapi_voice))
        # 2) 在线 edge-tts 神经语音（分隔线后）
        if _EDGE_AVAILABLE:
            self.voice_combo.insertSeparator(self.voice_combo.count())
            for name, short in _EDGE_VOICES:
                self.voice_combo.addItem(f"在线·{name}", ('edge', short))
        # 3) 默认选中：系统·微软Yaoyao（第一项）> 在线晓晓 > 第一个
        sel = 0  # 系统·微软Yaoyao 始终在第一位
        if self._voice_kind == 'edge' and _EDGE_AVAILABLE:
            for i in range(self.voice_combo.count()):
                data = self.voice_combo.itemData(i)
                if data and data[0] == 'edge' and data[1] == self._edge_voice:
                    sel = i
                    break
        if self.voice_combo.count() > sel:
            self.voice_combo.setCurrentIndex(sel)

    def _on_voice_changed(self, idx):
        data = self.voice_combo.itemData(idx)
        if not data:
            return
        kind, val = data
        if kind == 'edge':
            self._voice_kind = 'edge'
            self._edge_voice = val
        elif kind == 'sapi':
            self._voice_kind = 'sapi'
            self._sapi_voice = val

    def _speak(self, text):
        if not text:
            return
        self._ensure_speech()  # 首次朗读前才初始化语音引擎（延迟创建，避免启动期多媒体线程崩溃）
        try:
            clean = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()
        except Exception:
            return
        if not clean:
            return
        clean = clean[:120]  # 控制朗读长度
        # 入队逐条朗读：不打断当前播报，避免"只读一点就停"
        self._speak_queue.append(clean)
        self._try_next()

    def _try_next(self):
        """显式状态机：空闲时才从队列取一条朗读。
        用 self._reading 标志标记"正在朗读/合成/播放"，
        绝不依赖 say() 或 play() 后可能滞后的瞬时状态，避免误判空闲而互相打断。"""
        if self._reading:
            return
        if not self._speak_queue:
            return
        self._reading = True  # 先占位，再启动朗读
        clean = self._speak_queue.pop(0)
        if self._voice_kind == 'edge' and _EDGE_AVAILABLE and QMediaPlayer is not None:
            self._edge_stage = 'synth'  # 合成中
            t = TTSSpeechThread(clean, self._edge_voice)
            t.done.connect(self._on_speech_done)
            self._track_thread(t)  # 先跟踪（加入僵尸列表），再启动，确保任何时刻都有引用
            self._speech_thread = t
            keep_alive(t)  # 全局引用池兜底：任意竞态误杀引用也不会销毁运行中的 QThread
            t.start()
        elif self._tts is not None:
            # SAPI：优先用户选中的系统语音
            try:
                if self._sapi_voice is not None:
                    self._tts.setVoice(self._sapi_voice)
                self._tts.say(clean)
                self._sapi_started = True  # 已真正发声（say 成功即视为启动）
                self._sapi_ts = time.time()
            except Exception:
                self._reading = False  # say 失败，释放占用，继续下一条
                self._try_next()
        else:
            self._reading = False  # 无任何可用引擎，丢弃本条

    def _on_sapi_state(self, state):
        """SAPI 朗读完一条（回到 Ready）后，释放占用并继续读队列下一条"""
        try:
            if state == QTextToSpeech.State.Ready and self._sapi_started:
                self._sapi_started = False
                self._reading = False
                self._try_next()
        except Exception:
            pass

    def _watch(self):
        """兜底看门狗：若底层引擎信号（SAPI stateChanged / 播放器 EndOfMedia）因
        Qt/系统异常未触发，导致 _reading 一直占用而停止朗读，则强制恢复。"""
        if not self._reading:
            return
        try:
            if self._voice_kind == 'sapi' and self._tts is not None:
                st = self._tts.state()
                # 已发声超过 1 秒仍停在 Ready/Error（正常读完或引擎异常），强制释放；
                # 1 秒内不作判断，避免 say() 启动窗口的 Ready 初始态被误判
                if (self._sapi_started and st in (QTextToSpeech.State.Ready,
                                                  QTextToSpeech.State.Error)
                        and time.time() - self._sapi_ts > 1.0):
                    self._sapi_started = False
                    self._reading = False
                    self._try_next()
            elif self._voice_kind == 'edge' and self._player is not None:
                # 仅当已进入播放阶段（合成完成）且播放器回到 Stopped 才算播完
                if self._edge_stage == 'play':
                    ps = self._player.playbackState()
                    if ps == QMediaPlayer.PlaybackState.StoppedState:
                        self._edge_stage = None
                        self._reading = False
                        self._try_next()
        except Exception:
            pass

    def _on_speech_done(self, mp3_path, error):
        # 注意：这里不置 None self._speech_thread——线程可能仍在收尾，
        # 引用由 _track_thread 的 finished 信号机制负责回收，避免销毁运行中的线程
        if error or not mp3_path:
            # 在线语音失败（离线/网络异常）：释放占用，跳过本条继续下一条
            self._edge_stage = None
            self._reading = False
            self._try_next()
            return
        try:
            self._edge_stage = 'play'  # 进入播放阶段
            self._audio_path = mp3_path
            self._player.setSource(QUrl.fromLocalFile(mp3_path))
            self._player.play()
        except Exception:
            try:
                os.remove(mp3_path)
            except Exception:
                pass
            self._audio_path = None
            self._edge_stage = None
            self._reading = False  # 播放异常也继续读下一条
            self._try_next()

    def _on_media_status(self, status):
        if QMediaPlayer is None:
            return
        # 仅在真正播放结束或媒体无效时清理临时文件。
        # 注意：不能清理 NoMedia——播放器切换新音频时会经过 NoMedia 中间态，
        # 此时删除文件会导致播放中断（只读一点就停）。
        if status in (QMediaPlayer.MediaStatus.EndOfMedia,
                      QMediaPlayer.MediaStatus.InvalidMedia):
            if status == QMediaPlayer.MediaStatus.InvalidMedia:
                try:
                    self._player.stop()
                except Exception:
                    pass
            self._cleanup_audio()
            self._edge_stage = None
            self._reading = False  # 一条播完，释放占用，继续读下一条
            self._try_next()

    def _cleanup_audio(self):
        # 播放结束后保留 mp3 文件在 %TEMP%\RaccoonQuant\voice 目录下供查看，
        # 不再删除（历史产物由 TTSSpeechThread 生成侧按数量上限自动清理）
        self._audio_path = None

    def _stop_audio(self):
        # 停止当前播放并清理临时文件
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self._cleanup_audio()
        t = self._speech_thread
        if isinstance(t, QThread):
            try:
                t.done.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._kill_thread(t)  # 安全终止，保证线程结束后才回收

    def _stop_speak(self):
        self._speak_queue.clear()  # 手动停止：丢弃未朗读的排队文本
        self._reading = False      # 释放占用，保证停止后重新朗读不受阻
        self._edge_stage = None
        self._sapi_started = False
        self._stop_audio()
        if self._tts is not None:
            try:
                self._tts.stop()
            except Exception:
                pass

    def _speak_latest(self):
        """朗读最新一条消息"""
        if self._items:
            it = self._items[0]
            self._speak(f"{it['time']}，{it['title']}，{it['content']}")

    # ---------------- UI ----------------
    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(10)

        hl = QHBoxLayout()
        title = QLabel("财联电报")
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#F39C12;")
        hl.addWidget(title)

        self.status_label = QLabel("正在加载财联社电报...")
        self.status_label.setStyleSheet("color:#AAAAAA; font-size:13px;")
        hl.addWidget(self.status_label, 1)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setStyleSheet("""
            QPushButton { background-color: #F39C12; color: #1A1A2E; border: none;
                border-radius: 6px; padding: 8px 18px; font-size: 13px; font-weight: bold; }
            QPushButton:hover { background-color: #F5B041; }
            QPushButton:disabled { background-color: #555; color: #999; }
        """)
        self.refresh_btn.clicked.connect(self.refresh)
        hl.addWidget(self.refresh_btn)

        self.auto_cb = QCheckBox("自动刷新")
        self.auto_cb.setChecked(True)
        self.auto_cb.setToolTip("每 30 秒自动获取最新电报")
        self.auto_cb.setStyleSheet("color:#AAAAAA; font-size:13px;")
        self.auto_cb.toggled.connect(self._on_auto_toggled)
        hl.addWidget(self.auto_cb)

        self.voice_cb = QCheckBox("语音读报")
        self.voice_cb.setChecked(True)
        self.voice_cb.setToolTip("新消息到达时自动语音播报")
        self.voice_cb.setStyleSheet("color:#AAAAAA; font-size:13px;")
        hl.addWidget(self.voice_cb)

        self.speak_btn = QPushButton("朗读最新")
        self.speak_btn.setToolTip("朗读列表最新一条消息")
        self.speak_btn.setStyleSheet("""
            QPushButton { background-color: #16213E; color: #F39C12; border: 1px solid #2A2A4A;
                border-radius: 6px; padding: 8px 14px; font-size: 13px; }
            QPushButton:hover { border-color: #F39C12; }
        """)
        self.speak_btn.clicked.connect(self._speak_latest)
        hl.addWidget(self.speak_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setToolTip("停止当前语音播报")
        self.stop_btn.setStyleSheet("""
            QPushButton { background-color: #16213E; color: #E74C3C; border: 1px solid #2A2A4A;
                border-radius: 6px; padding: 8px 14px; font-size: 13px; }
            QPushButton:hover { border-color: #E74C3C; }
        """)
        self.stop_btn.clicked.connect(self._stop_speak)
        hl.addWidget(self.stop_btn)

        self.voice_combo = QComboBox()
        self.voice_combo.setToolTip("选择朗读声音：默认微软 Yaoyao（系统内置女声）；在线·晓晓、在线·香港晓佳为微软神经语音（需联网）")
        self.voice_combo.setStyleSheet("""
            QComboBox { background-color:#16213E; color:#DDDDDD; border:1px solid #2A2A4A;
                border-radius:6px; padding:6px 10px; font-size:13px; }
            QComboBox::drop-down { border:none; width:20px; }
            QComboBox QAbstractItemView { background-color:#16213E; color:#DDDDDD;
                border:1px solid #2A2A4A; selection-background-color:#0F3460; }
        """)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        hl.addWidget(self.voice_combo)
        self._fill_voices()
        layout.addLayout(hl)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget { background-color: #16213E; color: #DDDDDD; border: 1px solid #2A2A4A;
                border-radius: 6px; font-size: 13px; }
            QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #1F2A44; }
            QListWidget::item:hover { background-color: #1F3A5F; }
            QListWidget::item:selected { background-color: #0F3460; }
        """)
        self.list_widget.itemDoubleClicked.connect(self._show_detail)
        layout.addWidget(self.list_widget, 1)

    # ---------------- 数据 ----------------
    def refresh(self):
        if self._fetch_thread is not None and self._fetch_thread.isRunning():
            return  # 上一次请求未完成，跳过本次
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("正在获取财联社电报...")
        t = TelegraphFetchThread()
        self._fetch_thread = t
        t.result_ready.connect(self._on_result)
        self._track_thread(t)  # 僵尸列表双重保障：即使 _on_result 清理竞态，也保引用
        keep_alive(t)  # 全局引用池兜底：任意竞态误杀引用也不会销毁运行中的 QThread
        t.start()

    def _on_result(self, news):
        self.refresh_btn.setEnabled(True)
        # 仅当发出本信号的线程仍是当前线程时才清引用，防止竞态误杀新线程：
        # 自动刷新(30s)/手动刷新 可能在本槽执行前已启动新线程 T2，
        # 若此处无条件置 None，会释放运行中 T2 的唯一引用 → GC 销毁 → 0xC0000409 崩溃
        sender = self.sender()
        if sender is not None:
            if self._fetch_thread is sender:
                self._fetch_thread = None
        else:
            # 极少数情况下 sender() 拿不到：仅在线程已结束时才安全清理
            cur = self._fetch_thread
            try:
                if cur is not None and cur.isFinished():
                    self._fetch_thread = None
            except RuntimeError:
                self._fetch_thread = None
        if not news:
            self.status_label.setText("获取失败，请检查网络后重试")
            return
        self.status_label.setText(
            f"财联社电报 · {len(news)} 条 · {self._fmt_now()}")
        # 合并去重（按 id），新消息在前
        new_items = []
        for it in news:
            iid = str(it.get('id') or '')
            if iid and iid in self._ids:
                continue
            if iid:
                self._ids.add(iid)
            else:  # 无 id 的条目用时间+内容做键，避免重复插入
                key = (it.get('time', ''), it.get('title', ''))
                if key in self._text_keys:
                    continue
                self._text_keys.add(key)
            new_items.append(it)
        self._items = new_items + self._items
        if len(self._items) > 200:  # 限制列表长度，防止长期运行内存膨胀
            self._items = self._items[:200]
        self._rebuild_list()
        # 新消息语音播报（首次加载不朗读）
        if new_items and not self._first_load and self.voice_cb.isChecked():
            for it in new_items[:3]:
                self._speak(f"{it['time']}，{it['title']}")
        self._first_load = False

    def _rebuild_list(self):
        self.list_widget.clear()
        for it in self._items:
            lv = str(it.get('level', 'C') or 'C').upper()
            tag = _LEVEL_TAG.get(lv, '')
            t = it.get('title', '') or it.get('content', '')[:40]
            line = f"[{it.get('time', '')}] {tag}{t}"
            item = QListWidgetItem(line)
            item.setData(Qt.ItemDataRole.UserRole, it)
            item.setToolTip(it.get('content', ''))
            item.setForeground(QColor(_LEVEL_COLOR.get(lv, _LEVEL_COLOR['C'])))
            self.list_widget.addItem(item)

    def _show_detail(self, item):
        it = item.data(Qt.ItemDataRole.UserRole) or {}
        content = it.get('content', '') or it.get('title', '')
        if not content:
            return
        QMessageBox.information(self, f"财联社电报 · {it.get('time', '')}",
                                content, QMessageBox.StandardButton.Ok)

    def _on_auto_toggled(self, checked):
        if checked:
            self._auto_timer.start(30000)
            self.status_label.setText("自动刷新已开启（每 30 秒）")
        else:
            self._auto_timer.stop()
            self.status_label.setText("自动刷新已关闭")

    @staticmethod
    def _fmt_now():
        import time as _t
        return _t.strftime('%H:%M:%S')

    # ---------------- 生命周期 ----------------
    def stop_thread(self):
        """停止定时器、终止后台线程、停止语音播报"""
        self._auto_timer.stop()
        try:
            self._watch_timer.stop()
        except Exception:
            pass
        self._stop_speak()
        t = self._fetch_thread
        if isinstance(t, QThread):
            try:
                t.result_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._kill_thread(t)  # 安全终止，保证线程结束后才回收
        st = self._speech_thread
        if isinstance(st, QThread):
            self._kill_thread(st)
        if self._tts is not None:
            try:
                self._tts.stop()
            except Exception:
                pass
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self._cleanup_audio()
