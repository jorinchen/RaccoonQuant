"""
定时调度器 - 法定工作日自动执行 + 语音播报
"""
import threading
from datetime import datetime, time, date
from PyQt6.QtCore import QTimer, QObject, pyqtSignal

try:
    import chinese_calendar
    _HAS_CALENDAR = True
except ImportError:
    _HAS_CALENDAR = False


def _speak(text: str):
    """在后台线程中调用 Windows SAPI 语音播报（不阻塞 UI）"""
    def _do_speak():
        try:
            import win32com.client
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Speak(text)
        except Exception:
            pass  # 静默失败，不影响主流程
    t = threading.Thread(target=_do_speak, daemon=True)
    t.start()


class StockScheduler(QObject):
    """股票分析定时调度器"""

    morning_trigger = pyqtSignal()  # 9:25 早盘竞价
    afternoon_trigger = pyqtSignal()  # 14:30 尾盘2点半
    closing_trigger = pyqtSignal()  # 15:00 收盘推荐

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._check_schedule)
        self.timer.start(30000)  # 每30秒检查
        self._last_run = {  # 记录今日已触发
            'morning': None,
            'afternoon': None,
            'closing': None,
        }

    def _is_workday(self) -> bool:
        """判断是否为法定工作日"""
        if _HAS_CALENDAR:
            try:
                return chinese_calendar.is_workday(date.today())
            except:
                pass
        return date.today().weekday() < 5  # fallback: 周一到周五

    def _check_schedule(self):
        """检查是否需要触发定时任务"""
        if not self._is_workday():
            return

        now = datetime.now()
        today = now.date()

        # 9:25 早盘竞价
        target_morning = datetime.combine(today, time(9, 25))
        if (self._last_run['morning'] != today and
                target_morning <= now < target_morning.replace(hour=9, minute=30)):
            self._last_run['morning'] = today
            _speak("早盘竞价结束了，请注意查收竞价推荐")
            self.morning_trigger.emit()

        # 14:30 尾盘2点半
        target_afternoon = datetime.combine(today, time(14, 30))
        if (self._last_run['afternoon'] != today and
                target_afternoon <= now < target_afternoon.replace(hour=14, minute=35)):
            self._last_run['afternoon'] = today
            _speak("尾盘2点半到了，请注意查收尾盘推荐")
            self.afternoon_trigger.emit()

        # 首板推荐已改为全天手动执行，不再 15:00 自动触发

        # 跨日重置
        if self._last_run.get('morning') and self._last_run['morning'] != today:
            self._last_run = {'morning': None, 'afternoon': None, 'closing': None}
