"""
主窗口 - 6个Tab + 系统托盘 + 定时调度
"""
from PyQt6.QtWidgets import (QMainWindow, QTabWidget, QWidget, QSystemTrayIcon,
                              QMenu, QApplication, QLabel, QStatusBar)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QAction
from PyQt6.QtNetwork import QLocalServer
from ui.tabs.morning_tab import MorningTab
from ui.widgets.stock_list_panel import StockListPanel

SERVER_NAME = "RaccoonQuantStock"


def _load_app_version():
    """读取项目根目录 VERSION 文件，与打包自动递增共用同一版本号。

    打包后（onefile）VERSION 以 DATA 形式位于 sys._MEIPASS；
    源码模式下位于项目根目录（本文件在 <root>/ui/main_window.py）。
    """
    import os
    import re
    import sys
    candidates = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(meipass)
    candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for base in candidates:
        try:
            with open(os.path.join(base, 'VERSION'), 'r', encoding='utf-8') as f:
                txt = f.read().strip()
            if re.fullmatch(r'\d+\.\d+\.\d+', txt):
                return 'v' + txt
        except Exception:
            continue
    return 'v3.2.0'


VERSION = _load_app_version()

from ui.tabs.afternoon_tab import AfternoonTab
from ui.tabs.closing_tab import ClosingTab
from ui.tabs.search_tab import SearchTab
from ui.tabs.daily_pick_tab import DailyPickTab
from ui.tabs.dividend_tab import DividendTab
from ui.tabs.telegraph_tab import TelegraphTab
from ui.detail_window import DetailWindow
from scheduler import StockScheduler
import os
import threading

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._detail_windows = []
        self._was_maximized = True  # 默认启动时最大化
        self._last_geometry = None
        self._init_ui()
        self._init_scheduler()
        self._init_tray()
        self._init_local_server()

    def _init_ui(self):
        self.setWindowTitle(f"小浣熊量化选股 {VERSION}")
        self.setMinimumSize(1000, 650)

        # 主Tab
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet("""
            QTabWidget::pane { border: none; background-color: #1A1A2E; }
            QTabBar::tab { background-color: #16213E; color: #AAAAAA; padding: 12px 24px;
                font-size: 14px; border: none; margin-right: 2px; }
            QTabBar::tab:selected { background-color: #0F3460; color: #F39C12; font-weight: bold;
                border-bottom: 3px solid #F39C12; }
            QTabBar::tab:hover { background-color: #1F3A5F; color: #FFFFFF; }
        """)

        self.morning_tab = MorningTab()
        self.afternoon_tab = AfternoonTab()
        self.closing_tab = ClosingTab()
        self.search_tab = SearchTab()
        self.daily_pick_tab = DailyPickTab()
        self.dividend_tab = DividendTab()
        self.telegraph_tab = TelegraphTab()
        self.stock_list_tab = StockListPanel()

        self.tab_widget.addTab(self.stock_list_tab, "股票列表")
        self.tab_widget.addTab(self.morning_tab, "早盘竞价")
        self.tab_widget.addTab(self.afternoon_tab, "尾盘2点半")
        self.tab_widget.addTab(self.closing_tab, "首板推荐")
        self.tab_widget.addTab(self.daily_pick_tab, "每日推荐")
        self.tab_widget.addTab(self.dividend_tab, "分红推荐")
        self.tab_widget.addTab(self.search_tab, "搜索")
        self.tab_widget.addTab(self.telegraph_tab, "财联电报")

        self.setCentralWidget(self.tab_widget)

        # 连接表格点击信号 -> 打开详情
        self.stock_list_tab.stock_clicked.connect(self._open_detail)
        self.morning_tab.table.stock_clicked.connect(self._open_detail)
        self.afternoon_tab.table.stock_clicked.connect(self._open_detail)
        self.closing_tab.table.stock_clicked.connect(self._open_detail)
        self.daily_pick_tab.table.stock_clicked.connect(self._open_detail)
        self.dividend_tab.table.stock_clicked.connect(self._open_detail)
        self.search_tab.table.stock_clicked.connect(self._open_detail)

        # 底部状态栏 - 作者信息
        self.statusBar().setStyleSheet("""
            QStatusBar { background-color: #16213E; color: #888888; font-size: 11px; border-top: 1px solid #0F3460; }
        """)
        author_label = QLabel("作者：Jorin    微信：17607648868")
        author_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.statusBar().addPermanentWidget(author_label)

    def _init_scheduler(self):
        """初始化定时调度器"""
        self.scheduler = StockScheduler(self)
        self.scheduler.morning_trigger.connect(self.morning_tab.run_analysis)
        self.scheduler.afternoon_trigger.connect(self.afternoon_tab.run_analysis)
        self.scheduler.closing_trigger.connect(self.closing_tab.run_analysis)

    def _init_tray(self):
        """系统托盘"""
        if QSystemTrayIcon.isSystemTrayAvailable():
            from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QFont
            from PyQt6.QtCore import Qt
            # 绘制小浣熊托盘图标
            s = 32
            pix = QPixmap(s, s)
            pix.fill(Qt.GlobalColor.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            cx, cy = s / 2, s / 2
            # 耳朵
            ew, eh = s * 0.22, s * 0.28
            for side in [-1, 1]:
                ex = cx + side * s * 0.22; ey = s * 0.15
                p.setBrush(QColor("#7A7A7A")); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(int(ex - ew / 2), int(ey - eh / 2), int(ew), int(eh))
                p.setBrush(QColor("#D4A574"))
                p.drawEllipse(int(ex - ew / 3.5), int(ey - eh / 3.5), int(ew * 0.55), int(eh * 0.55))
            # 脸部
            fw, fh = s * 0.58, s * 0.52
            p.setBrush(QColor("#B0B0B0"))
            p.drawEllipse(int(cx - fw / 2), int(cy - fh / 2 + s * 0.02), int(fw), int(fh))
            # 白色脸颊
            cw, ch = s * 0.36, s * 0.28
            p.setBrush(QColor("#E8E8E8"))
            p.drawEllipse(int(cx - cw / 2), int(cy - ch / 2 + s * 0.03), int(cw), int(ch))
            # 眼罩
            mw, mh = s * 0.19, s * 0.13
            p.setBrush(QColor("#2C2C2C"))
            for side in [-1, 1]:
                mx = cx + side * s * 0.095; my = cy - s * 0.06
                p.drawEllipse(int(mx - mw / 2), int(my - mh / 2), int(mw), int(mh))
            # 眼睛
            ew2, eh2 = s * 0.095, s * 0.1
            p.setBrush(QColor("#FFFFFF"))
            for side in [-1, 1]:
                eex = cx + side * s * 0.095; eey = cy - s * 0.06
                p.drawEllipse(int(eex - ew2 / 2), int(eey - eh2 / 2), int(ew2), int(eh2))
            pr = s * 0.04
            p.setBrush(QColor("#111111"))
            for side in [-1, 1]:
                ppx = cx + side * s * 0.1; ppy = cy - s * 0.055
                p.drawEllipse(int(ppx - pr), int(ppy - pr), int(pr * 2), int(pr * 2))
            # 鼻子
            nr = s * 0.04
            p.setBrush(QColor("#1A1A1A"))
            p.drawEllipse(int(cx - nr), int(cy - nr + s * 0.05), int(nr * 2), int(nr * 2))
            # 嘴巴
            p.setPen(QPen(QColor("#555555"), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            my = cy + s * 0.1; mhl = s * 0.06
            p.drawArc(int(cx - mhl), int(my - mhl * 0.5), int(mhl * 2), int(mhl), 0, 180 * 16)
            p.end()
            self.tray = QSystemTrayIcon(self)
            self.tray.setIcon(QIcon(pix))
            self.tray.setToolTip("小浣熊量化选股")
            self.tray.show()
            menu = QMenu()
            show_action = QAction("显示", self)
            show_action.triggered.connect(self.show)
            menu.addAction(show_action)
            quit_action = QAction("退出", self)
            quit_action.triggered.connect(self._quit_app)
            menu.addAction(quit_action)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._tray_activated)

    def changeEvent(self, event):
        """捕获窗口状态变化，记住最小化前的尺寸"""
        if event.type() == event.Type.WindowStateChange:
            if not self.isMinimized():
                # 窗口非最小化时，记录当前状态
                self._was_maximized = self.isMaximized()
                if not self.isMaximized():
                    self._last_geometry = self.geometry()
        super().changeEvent(event)

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                       QSystemTrayIcon.ActivationReason.DoubleClick):
            self._bring_to_front()

    def _bring_to_front(self):
        """将窗口带到前台（托盘/新实例触发）"""
        if self._was_maximized:
            self.showMaximized()
        elif self._last_geometry is not None:
            self.setGeometry(self._last_geometry)
            self.showNormal()
        else:
            self.showNormal()
        self.activateWindow()
        self.raise_()

    def _init_local_server(self):
        """单实例本地服务器：新实例启动时通知本窗口显示"""
        QLocalServer.removeServer(SERVER_NAME)
        self._local_server = QLocalServer(self)
        self._local_server.newConnection.connect(self._on_new_instance_connected)
        self._local_server.listen(SERVER_NAME)

    def _on_new_instance_connected(self):
        while self._local_server.hasPendingConnections():
            sock = self._local_server.nextPendingConnection()
            if sock and sock.waitForReadyRead(500):
                data = sock.readAll().data()
                if data == b"show":
                    self._bring_to_front()
            if sock:
                sock.disconnectFromServer()
                sock.deleteLater()

    def _open_detail(self, code: str, name: str):
        """打开股票详情窗口"""
        if not code or not isinstance(code, str):
            return
        try:
            # 检查是否已打开（已销毁的窗口会抛 RuntimeError，跳过并重建）
            alive = []
            for dw in self._detail_windows:
                try:
                    if getattr(dw, 'code', None) == code:
                        dw.show(); dw.raise_()
                        return
                    alive.append(dw)
                except RuntimeError:
                    continue
            detail = DetailWindow(code, name)
            detail.show()
            alive.append(detail)
            self._detail_windows = alive
        except Exception:
            import traceback; traceback.print_exc()

    def closeEvent(self, event):
        """关闭事件 - 最小化到系统托盘"""
        if hasattr(self, 'tray') and self.tray.isVisible():
            self.hide()
            event.ignore()  # 不关闭，仅隐藏到托盘
        else:
            # 无托盘时正常退出
            self._quit_app()
            event.accept()

    def _quit_app(self):
        """彻底退出应用（从托盘菜单调用）。
        多路兜底：任何一步异常都不阻塞退出；正常 quit 后 3 秒内
        进程仍未结束则 os._exit 强杀，杜绝托盘退出后进程/任务栏残留。"""
        # 0. 立即隐藏主窗口，让任务栏图标马上消失
        try:
            self.hide()
        except Exception:
            pass

        # 1. 停止调度器
        if hasattr(self, 'scheduler'):
            try:
                self.scheduler.timer.stop()
            except Exception:
                pass

        # 2. 终止所有 tab 的工作线程，防止进程残留
        for tab in [self.stock_list_tab, self.morning_tab, self.afternoon_tab,
                     self.closing_tab, self.search_tab, self.daily_pick_tab,
                     self.telegraph_tab]:
            try:
                if hasattr(tab, 'stop_thread'):
                    tab.stop_thread()
            except Exception:
                import traceback; traceback.print_exc()

        # 3. 关闭所有股票详情窗口（其内部线程也会被终止）
        for dw in self._detail_windows:
            try:
                if hasattr(dw, 'stop_thread'):
                    dw.stop_thread()
                dw.close()
            except Exception:
                pass
        self._detail_windows.clear()

        # 4. 隐藏托盘图标
        if hasattr(self, 'tray'):
            try:
                self.tray.hide()
            except Exception:
                pass
        if hasattr(self, '_local_server'):
            try:
                self._local_server.close()
                QLocalServer.removeServer(SERVER_NAME)
            except Exception:
                pass

        # 5. 统一等待所有后台线程真正结束（含 terminate 超时的网络线程），
        #    防止进程退出时运行中的 QThread 被析构触发 0xC0000409 崩溃。
        #    shutdown_all 已对僵尸 QThread（C++ 对象已销毁）免疫。
        remaining = []
        try:
            from ui import thread_utils
            remaining = thread_utils.shutdown_all(timeout_ms=5000)
        except Exception:
            import traceback; traceback.print_exc()

        # 6. 退出
        try:
            QApplication.processEvents()
        except Exception:
            pass
        if remaining:
            # 极少数无法结束的线程：绕过 Python 清理直接终止进程，
            # 避免 QThread C++ 对象析构时线程仍在运行导致崩溃
            os._exit(0)
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)
        # 兜底：用独立守护线程强杀（不依赖 Qt 事件循环）。
        # 注意：不能用 QTimer 做兜底——app.quit 使事件循环退出后
        # QTimer 永不触发；若此时非 daemon 线程（如 asyncio.to_thread
        # 中的 tdx 连接）仍阻塞，进程会永久残留。
        try:
            threading.Timer(3.0, os._exit, args=(0,)).start()
        except Exception:
            os._exit(0)
