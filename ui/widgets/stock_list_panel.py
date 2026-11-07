"""
股票列表栏目（主页独立 Tab）

- 表头：代码、名称、拼音首字母、现价、板块概念
- 数据源：MySQL（stock 库 thk_wx_stock_list 表），分页每页 10 行
- 检索：代码/名称/拼音首字母/板块概念 模糊查询
- "更新股票"按钮：从腾讯、东方财富、同花顺、通达信、雪球等多源拉取列表入库（缺失添加、已存在更新）
- MySQL 连接失败时弹出连接配置输入框（主机/端口/用户名/密码/数据库名），保存后重连，配置持久化
"""
import asyncio
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QDialog, QFormLayout, QMessageBox, QProgressBar, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QBrush

import mysql_store
import stock_list_updater

TABLE_ROW_HEIGHT = 40

COLOR_RED = QColor("#E74C3C")
COLOR_GREEN = QColor("#2ECC71")
COLOR_WHITE = QColor("#FFFFFF")
COLOR_GRAY = QColor("#AAAAAA")
COLOR_DARK_BG = QColor("#1A1A2E")
COLOR_CARD_BG = QColor("#16213E")
COLOR_SELECTION_BG = QColor("#1F3A5F")
COLOR_HEADER = QColor("#0F3460")

HEADERS = ['代码', '名称', '拼音首字母', '现价', '板块概念']
HEADER_MAP = {
    '代码': 'code', '名称': 'name', '拼音首字母': 'name_initials',
    '现价': 'current_price', '板块概念': 'sector_concept',
}
PAGE_SIZE = 10


# ============================================================ MySQL 连接配置弹窗
class MysqlConfigDialog(QDialog):
    """MySQL 连接配置输入框（非模态置顶，弹出时可切换上方栏目）"""

    saved = pyqtSignal(dict)  # 保存成功（连接测试通过）后发出

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MySQL 连接设置")
        self.setWindowFlags(Qt.WindowType.Window |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(False)
        self.setMinimumWidth(420)
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet(f"""
            QDialog {{ background-color: {COLOR_CARD_BG.name()}; }}
            QLabel {{ color: {COLOR_WHITE.name()}; font-size: 13px; }}
            QLineEdit {{
                background-color: #0F3460; color: {COLOR_WHITE.name()};
                border: 1px solid #2A2A4A; border-radius: 4px;
                padding: 6px 8px; font-size: 13px;
            }}
            QPushButton {{
                background-color: #F39C12; color: #1A1A2E; border: none;
                border-radius: 6px; padding: 8px 22px; font-size: 13px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #F5B041; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        tip = QLabel("请配置 MySQL 连接信息，测试通过后保存：")
        tip.setStyleSheet("color:#F39C12; font-size:13px; font-weight:bold;")
        layout.addWidget(tip)

        form = QFormLayout()
        form.setSpacing(8)

        self.host_edit = QLineEdit('127.0.0.1')
        self.port_edit = QLineEdit('3306')
        self.user_edit = QLineEdit('stock')
        self.pwd_edit = QLineEdit('stock@123456')
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.db_edit = QLineEdit('stock')

        form.addRow("主机:", self.host_edit)
        form.addRow("端口:", self.port_edit)
        form.addRow("用户名:", self.user_edit)
        form.addRow("密码:", self.pwd_edit)
        form.addRow("数据库名:", self.db_edit)
        layout.addLayout(form)

        # 预填已保存配置
        cfg = mysql_store.load_config()
        self.host_edit.setText(str(cfg.get('host', '127.0.0.1')))
        self.port_edit.setText(str(cfg.get('port', 3306)))
        self.user_edit.setText(str(cfg.get('user', 'stock')))
        self.pwd_edit.setText(str(cfg.get('password', 'stock@123456')))
        self.db_edit.setText(str(cfg.get('database', 'stock')))

        self.btn_row = QHBoxLayout()
        self.save_btn = QPushButton("保存并连接")
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setStyleSheet(
            "QPushButton { background-color:#555; color:#CCC; }")
        self.btn_row.addStretch()
        self.btn_row.addWidget(self.cancel_btn)
        self.btn_row.addWidget(self.save_btn)
        layout.addLayout(self.btn_row)

        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.reject)

    def _collect(self):
        try:
            port = int(self.port_edit.text().strip() or 3306)
        except ValueError:
            port = 3306
        return {
            'host': self.host_edit.text().strip() or '127.0.0.1',
            'port': port,
            'user': self.user_edit.text().strip() or 'stock',
            'password': self.pwd_edit.text(),
            'database': self.db_edit.text().strip() or 'stock',
        }

    def _on_save(self):
        cfg = self._collect()
        ok, err = mysql_store.test_connection(cfg)
        if not ok:
            QMessageBox.warning(self, "连接失败",
                                f"无法连接 MySQL：\n{err}\n\n请检查配置后重试。")
            return
        mysql_store.save_config(cfg)
        self.saved.emit(cfg)
        self.accept()


# ============================================================ 后台线程：分页加载
class StockListLoadThread(QThread):
    loaded = pyqtSignal(int, list)   # total, rows
    error = pyqtSignal(str)

    def __init__(self, keyword='', page=1, page_size=PAGE_SIZE, parent=None):
        super().__init__(parent)
        self.keyword = keyword
        self.page = page
        self.page_size = page_size

    def run(self):
        try:
            total, rows = mysql_store.query_page(
                self.keyword, self.page, self.page_size)
            self.loaded.emit(total, rows)
        except Exception as e:
            self.error.emit(str(e))


# ============================================================ 后台线程：更新股票
class StockListUpdateThread(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(int, int)      # total, added
    error = pyqtSignal(str)

    def run(self):
        try:
            from data_fetcher import MultiSourceFetcher, new_async_loop
            loop = new_async_loop()
            asyncio.set_event_loop(loop)

            async def do_update():
                async with MultiSourceFetcher() as fetcher:
                    total, added = await stock_list_updater.update_stock_list(
                        fetcher, progress_cb=lambda m: self.progress.emit(m))
                    return total, added

            total, added = loop.run_until_complete(do_update())
            loop.close()
            self.done.emit(total, added)
        except Exception as e:
            self.error.emit(str(e))


# ============================================================ 股票列表面板
class StockListPanel(QWidget):
    """股票列表栏目：表头 + 分页 + 检索 + 更新股票 + 连接失败弹窗"""

    stock_clicked = pyqtSignal(str, str)  # code, name（双击行打开详情）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page = 1
        self.total = 0
        self.keyword = ''
        self.load_thread = None
        self.update_thread = None
        self.config_dialog = None
        self._db_ok = False  # 数据库连接状态：正常才可更新股票
        self._init_ui()
        self.update_btn.setEnabled(False)  # 初始未连接，禁用更新
        self.reload_data()

    def _init_ui(self):
        self.setMinimumHeight(320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # ---------- 标题行 ----------
        title_row = QHBoxLayout()
        title = QLabel("股票列表")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#F39C12;")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        # ---------- 数据库连接 / 更新股票按钮行（位于检索按钮上方） ----------
        update_row = QHBoxLayout()
        self.db_btn = QPushButton("数据库连接")
        self.db_btn.setStyleSheet("""
            QPushButton { background-color: #0F3460; color: #FFFFFF; border: 1px solid #2A2A4A;
                border-radius: 6px; padding: 8px 20px; font-size: 13px; font-weight: bold; }
            QPushButton:hover { background-color: #1F3A5F; }
        """)
        self.db_btn.clicked.connect(self._show_config_dialog)
        self.update_btn = QPushButton("更新股票")
        self.update_btn.setStyleSheet("""
            QPushButton { background-color: #F39C12; color: #1A1A2E; border: none;
                border-radius: 6px; padding: 8px 20px; font-size: 13px; font-weight: bold; }
            QPushButton:hover { background-color: #F5B041; }
            QPushButton:disabled { background-color: #555; color: #999; }
        """)
        self.update_btn.clicked.connect(self.update_stocks)
        update_row.addWidget(self.db_btn)
        update_row.addWidget(self.update_btn)
        update_row.addStretch()
        layout.addLayout(update_row)

        # ---------- 检索行 ----------
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入代码 / 名称 / 拼音首字母 / 板块概念 检索")
        self.search_edit.setStyleSheet("""
            QLineEdit { background-color: #0F3460; color: #FFFFFF;
                border: 1px solid #2A2A4A; border-radius: 4px;
                padding: 6px 10px; font-size: 13px; }
        """)
        self.search_edit.returnPressed.connect(self.do_search)
        self.search_btn = QPushButton("检索")
        self.search_btn.setStyleSheet("""
            QPushButton { background-color: #0F3460; color: #FFFFFF; border: 1px solid #2A2A4A;
                border-radius: 6px; padding: 8px 20px; font-size: 13px; font-weight: bold; }
            QPushButton:hover { background-color: #1F3A5F; }
        """)
        self.search_btn.clicked.connect(self.do_search)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_btn)
        layout.addLayout(search_row)

        # ---------- 表格 ----------
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {COLOR_DARK_BG.name()};
                color: {COLOR_WHITE.name()};
                gridline-color: #2A2A4A;
                border: 1px solid #2A2A4A; border-radius: 4px; font-size: 13px;
            }}
            QTableWidget::item {{ padding: 4px 8px; border-bottom: 1px solid #2A2A4A; }}
            QTableWidget::item:hover {{ background-color: #1A2F4A; }}
            QTableWidget::item:selected {{ background-color: {COLOR_SELECTION_BG.name()}; }}
            QTableWidget::item:alternate {{ background-color: #11182B; }}
            QHeaderView::section {{
                background-color: {COLOR_HEADER.name()}; color: {COLOR_WHITE.name()};
                padding: 7px 6px; border: 1px solid #2A2A4A;
                font-weight: bold; font-size: 12px;
            }}
        """)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStretchLastSection(False)
        for i in range(len(HEADERS)):
            # 板块概念内容较长（行业|概念1,概念2,...），占满剩余空间自动省略，
            # 完整内容见 tooltip；其余列按内容自适应
            mode = (QHeaderView.ResizeMode.Stretch if HEADERS[i] == '板块概念'
                    else QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(i, mode)
        self.table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        layout.addWidget(self.table, 1)

        # ---------- 分页行 ----------
        page_row = QHBoxLayout()
        self.prev_btn = QPushButton("上一页")
        self.next_btn = QPushButton("下一页")
        for b in (self.prev_btn, self.next_btn):
            b.setStyleSheet("""
                QPushButton { background-color: #0F3460; color: #FFFFFF;
                    border: 1px solid #2A2A4A; border-radius: 6px;
                    padding: 6px 18px; font-size: 12px; }
                QPushButton:hover { background-color: #1F3A5F; }
                QPushButton:disabled { color: #666; }
            """)
        self.page_label = QLabel("第 1 / 1 页")
        self.total_label = QLabel("共 0 条")
        for lb in (self.page_label, self.total_label):
            lb.setStyleSheet("color:#AAAAAA; font-size:12px;")
        self.prev_btn.clicked.connect(self.prev_page)
        self.next_btn.clicked.connect(self.next_page)
        page_row.addWidget(self.prev_btn)
        page_row.addWidget(self.page_label)
        page_row.addWidget(self.next_btn)
        page_row.addStretch()
        page_row.addWidget(self.total_label)
        layout.addLayout(page_row)

        # ---------- 状态行 ----------
        self.status = QLabel("正在加载数据...")
        self.status.setStyleSheet("color:#AAAAAA; font-size:12px; padding:2px;")
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar { border: 1px solid #2A2A4A; border-radius: 4px;
                background-color: #16213E; height: 14px; }
            QProgressBar::chunk { background-color: #F39C12; border-radius: 3px; }
        """)
        layout.addWidget(self.progress)

    # ------------------------------------------------------------ 数据库连接状态
    def _set_db_ok(self, ok):
        """更新数据库连接状态：连接正常才允许点击「更新股票」"""
        self._db_ok = bool(ok)
        self.update_btn.setEnabled(self._db_ok)
        if not self._db_ok:
            self.status.setText("数据库连接失败，请点击\"数据库连接\"配置后重试")

    # ------------------------------------------------------------ 数据加载
    def reload_data(self):
        """重新加载当前页（带检索关键词）"""
        self.status.setText("正在加载数据...")
        self._start_load_thread()

    def _start_load_thread(self):
        """启动分页查询线程。使用序号令牌丢弃过期结果，
        旧线程让其自然结束（不阻塞 UI），由 thread_utils 引用池回收。"""
        self._load_seq = getattr(self, '_load_seq', 0) + 1
        seq = self._load_seq
        self.load_thread = StockListLoadThread(self.keyword, self.page)
        self.load_thread.loaded.connect(
            lambda total, rows, s=seq: self._on_loaded(s, total, rows))
        self.load_thread.error.connect(self._on_load_error)
        from ui.thread_utils import keep_alive
        keep_alive(self.load_thread)
        self.load_thread.start()

    def _retire_load_thread(self):
        # 不 kill：让旧线程自然结束（MySQL 查询很快），结果由序号令牌丢弃
        self.load_thread = None

    def _on_loaded(self, seq, total, rows):
        if seq != getattr(self, '_load_seq', 0):
            return  # 过期结果，丢弃
        self.total = int(total or 0)
        total_pages = max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)
        if self.page > total_pages:
            self.page = total_pages
            self._start_load_thread()
            return
        self._fill_table(rows)
        self.page_label.setText(f"第 {self.page} / {total_pages} 页")
        self.total_label.setText(f"共 {self.total} 条")
        self.prev_btn.setEnabled(self.page > 1)
        self.next_btn.setEnabled(self.page < total_pages)
        self.status.setText(f"加载完成，共 {self.total} 条")
        self._set_db_ok(True)

    def _on_load_error(self, err):
        self.status.setText("数据库连接失败")
        self.table.setRowCount(0)
        self.total_label.setText("共 0 条")
        self.page_label.setText("第 - / - 页")
        self._set_db_ok(False)
        self._show_config_dialog()

    def _fill_table(self, rows):
        self.table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            for c_idx, header in enumerate(HEADERS):
                key = HEADER_MAP[header]
                value = row.get(key) if isinstance(row, dict) else ''
                text = self._fmt(header, value)
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if header == '代码':
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    # 存整行原始数据，双击任意列均可取到 code/name 打开详情
                    item.setData(Qt.ItemDataRole.UserRole, {
                        'code': row.get('code', '') if isinstance(row, dict) else '',
                        'name': row.get('name', '') if isinstance(row, dict) else '',
                    })
                if header == '现价':
                    try:
                        v = float(value)
                        item.setForeground(QBrush(COLOR_RED if v > 0 else COLOR_WHITE))
                    except (TypeError, ValueError):
                        pass
                if header == '板块概念' and value:
                    item.setToolTip(str(value))
                self.table.setItem(r_idx, c_idx, item)

    def _on_cell_double_clicked(self, row, col):
        """双击行 -> 发出 stock_clicked 信号，由主窗口打开详情页"""
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        code = str(data.get('code', '') or '')
        if code:
            self.stock_clicked.emit(code, str(data.get('name', '') or ''))

    @staticmethod
    def _fmt(header, value):
        if value is None or value == '':
            return '-'
        if header in ('现价', 'current_price'):
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return '-'
        return str(value)

    # ------------------------------------------------------------ 检索
    def do_search(self):
        self.keyword = self.search_edit.text().strip()
        self.page = 1
        self.reload_data()

    # ------------------------------------------------------------ 分页
    def prev_page(self):
        if self.page > 1:
            self.page -= 1
            self.reload_data()

    def next_page(self):
        if self.page * PAGE_SIZE < self.total:
            self.page += 1
            self.reload_data()

    # ------------------------------------------------------------ 更新股票
    def update_stocks(self):
        self.update_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("正在更新股票列表（多源拉取）...")
        self._retire_update_thread()
        self.update_thread = StockListUpdateThread()
        self.update_thread.progress.connect(self._on_update_progress)
        self.update_thread.done.connect(self._on_update_done)
        self.update_thread.error.connect(self._on_update_error)
        from ui.thread_utils import keep_alive
        keep_alive(self.update_thread)
        self.update_thread.start()

    def _retire_update_thread(self):
        from ui.thread_utils import kill_thread
        if isinstance(self.update_thread, QThread):
            kill_thread(self.update_thread)
        self.update_thread = None

    def _on_update_progress(self, msg):
        self.status.setText(msg)

    def _on_update_done(self, total, added):
        self._set_db_ok(True)
        self.progress.setVisible(False)
        self.status.setText(f"更新完成：共 {total} 只股票，新增 {added} 只")
        self.page = 1
        self.reload_data()

    def _on_update_error(self, err):
        self.progress.setVisible(False)
        # 更新过程中连不上库（如配置错误）→ 置为断连状态并弹连接配置框
        if 'connect' in str(err).lower() or 'access' in str(err).lower() \
                or 'denied' in str(err).lower():
            self._set_db_ok(False)
            self._show_config_dialog()
        else:
            self._set_db_ok(self._db_ok)  # 非连接类错误，保持当前连接状态
            self.status.setText(f"更新失败：{err}")

    # ------------------------------------------------------------ 连接配置弹窗
    def _show_config_dialog(self):
        """弹出数据库连接输入框（按钮主动打开 / 连接失败自动打开，非模态可切换栏目）"""
        if self.config_dialog is not None and self.config_dialog.isVisible():
            return
        self.config_dialog = MysqlConfigDialog(self.window())
        self.config_dialog.saved.connect(self._on_config_saved)
        self.config_dialog.show()

    def _on_config_saved(self, cfg):
        self.status.setText("数据库连接成功，重新加载数据...")
        self._set_db_ok(True)
        self.page = 1
        self.reload_data()

    # ------------------------------------------------------------ 线程清理
    def stop_thread(self):
        """退出/切换时安全清理工作线程"""
        from ui.thread_utils import kill_thread
        if isinstance(self.load_thread, QThread):
            kill_thread(self.load_thread)
        self.load_thread = None
        self._retire_update_thread()
