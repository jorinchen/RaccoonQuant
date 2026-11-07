"""
搜索模块 - 拼音搜索 + 实时下拉匹配 + 板块概念
"""
import asyncio
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                              QPushButton, QProgressBar, QComboBox, QListView, QAbstractItemView)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QStringListModel, QTimer
from PyQt6.QtGui import QColor
from data_fetcher import MultiSourceFetcher, new_async_loop, match_stock_by_pinyin, get_pinyin_initials
from stock_list_loader import load_stock_list
from log_manager import get_logger
from ui.widgets.stock_table import StockTableWidget
from ui.thread_utils import keep_alive, kill_thread, retire_thread

logger = get_logger('search')


def _to_float(val, default=0.0):
    """行情/列表接口偶发返回 '-'、'--'、'' 等脏值，统一安全转 float，避免比较时崩溃"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

HEADERS = ['代码', '名称', '竞价涨幅', '实时涨幅', '昨日收盘价', '现价',
           '换手率', '成交额', '量比', '市值', '主力净额', '5日涨幅',
           '10日涨幅', '20日涨幅', '动态市盈', '买卖区间', '板块概念']
HEADER_MAP = {
    '代码': 'code', '名称': 'name',
    '竞价涨幅': 'auction_change', '实时涨幅': 'real_time_change',
    '昨日收盘价': 'yesterday_close', '现价': 'current',
    '换手率': 'turnover_rate', '成交额': 'amount',
    '量比': 'volume_ratio', '市值': 'market_cap', '主力净额': 'main_net',
    '5日涨幅': 'change_5d', '10日涨幅': 'change_10d',
    '20日涨幅': 'change_20d', '动态市盈': 'pe', '买卖区间': 'price_range', '板块概念': 'sector_concept',
}


class LoadStockListThread(QThread):
    result_ready = pyqtSignal(list)

    def run(self):
        try:
            loop = new_async_loop()
            asyncio.set_event_loop(loop)
            async def do():
                async with MultiSourceFetcher() as f:
                    stocks, _src = await load_stock_list(f)
                    logger.info('搜索栏目股票列表加载：%d 支，来源=%s', len(stocks), _src)
                    return stocks
            r = loop.run_until_complete(do()); loop.close()
            self.result_ready.emit(r)
        except Exception:
            logger.exception('搜索栏目股票列表加载失败')
            self.result_ready.emit([])


class SearchDataThread(QThread):
    result_ready = pyqtSignal(list)
    progress_update = pyqtSignal(str)

    def __init__(self, keyword):
        super().__init__()
        self.keyword = keyword

    def run(self):
        try:
            loop = new_async_loop()
            asyncio.set_event_loop(loop)
            async def do():
                async with MultiSourceFetcher() as fetcher:
                    self.progress_update.emit("正在加载股票数据...")
                    stocks, _src = await load_stock_list(fetcher, progress_cb=self.progress_update.emit)
                    logger.info('搜索(实时)股票列表加载：%d 支，来源=%s', len(stocks), _src)
                    if len(stocks) < 50:
                        # 股票列表 API 不可用，使用搜索建议 API 兜底
                        self.progress_update.emit("正在在线搜索...")
                        stocks = await fetcher.search_stock_suggest(self.keyword)
                    self.progress_update.emit("正在搜索匹配股票...")
                    results = match_stock_by_pinyin(stocks, self.keyword)
                    if not results:
                        # 本地匹配无结果（如搜索深市股票但列表无深市数据），兜底在线搜索
                        self.progress_update.emit("本地无匹配，正在在线搜索...")
                        results = await fetcher.search_stock_suggest(self.keyword)
                    self.progress_update.emit(f"找到 {len(results)} 支匹配股票，获取实时行情...")
                    if results:
                        codes = [r['code'] for r in results]
                        rt = await fetcher.fetch_real_time_quotes(codes)
                        # 批量获取板块概念（限前 30 支）
                        self.progress_update.emit("正在获取板块概念...")
                        concept_tasks = [fetcher.fetch_stock_concepts(c) for c in codes[:30]]
                        concept_results = await asyncio.gather(*concept_tasks, return_exceptions=True)
                        concept_map = {}
                        for i, c in enumerate(codes[:30]):
                            cr = concept_results[i] if i < len(concept_results) else None
                            concept_map[c] = '、'.join(cr) if isinstance(cr, list) and cr else ''
                        # 获取资金流向
                        self.progress_update.emit("正在获取资金流向...")
                        flow_tasks = [fetcher.fetch_money_flow(c) for c in codes[:50]]
                        flow_results = await asyncio.gather(*flow_tasks, return_exceptions=True)
                        flow_map = {}
                        for i, c in enumerate(codes[:50]):
                            fr = flow_results[i]
                            if isinstance(fr, dict) and fr:
                                flow_map[c] = fr

                        for s in results:
                            code = s['code']
                            if code in rt:
                                q = rt[code]
                                yc = _to_float(q.get('yesterday_close', 0))
                                op = _to_float(q.get('open', 0))
                                s['current'] = _to_float(q.get('current', 0))
                                s['change_pct'] = _to_float(q.get('change_pct', 0))
                                s['yesterday_close'] = yc
                                s['turnover_rate'] = _to_float(q.get('turnover_rate', 0))
                                s['amount'] = _to_float(q.get('amount', 0))
                                s['pe'] = _to_float(q.get('pe', 0))
                                s['market_cap'] = _to_float(q.get('market_cap', 0))
                                s['volume_ratio'] = _to_float(q.get('volume_ratio', 0))
                                # 竞价涨幅 = (开盘价 - 昨收) / 昨收 * 100
                                s['auction_change'] = ((op - yc) / yc * 100) if yc > 0 else 0
                            else:
                                s.setdefault('auction_change', 0)
                            s.setdefault('real_time_change', s.get('change_pct', 0))
                            s.setdefault('volume_ratio', s.get('volume_ratio', 0))
                            flow = flow_map.get(code, {})
                            s.setdefault('main_net', flow.get('main', 0) if flow.get('data_available') else 0)
                            s.setdefault('main_net_5d', 0)
                            s.setdefault('main_net_10d', 0)
                            s.setdefault('main_net_20d', 0)
                            s.setdefault('sector_concept', concept_map.get(code, ''))
                            s.setdefault('price_range', '-')
                            s.setdefault('change_5d', 0)
                            s.setdefault('change_10d', 0)
                            s.setdefault('change_20d', 0)

                        # 获取多日涨跌幅（并行请求，限前 50 支避免匹配过多时网络请求爆炸）
                        self.progress_update.emit("正在获取涨跌幅...")
                        price_changes = {}
                        pc_targets = results[:50]
                        pc_tasks = [self._safe_fetch_price_changes(fetcher, s['code']) for s in pc_targets]
                        pc_results = await asyncio.gather(*pc_tasks, return_exceptions=True)
                        for i, s in enumerate(results):
                            code = s['code']
                            cr = pc_results[i] if i < len(pc_results) else None
                            chg = cr if isinstance(cr, dict) else {}
                            if chg:
                                price_changes[code] = chg
                        for s in results:
                            code = s['code']
                            chg = price_changes.get(code, {})
                            if chg:
                                s['change_5d'] = chg.get('change_5d', 0)
                                s['change_10d'] = chg.get('change_10d', 0)
                                s['change_20d'] = chg.get('change_20d', 0)

                        # 获取买卖区间（并行请求，最多20个避免超时）
                        self.progress_update.emit("正在计算买卖区间...")
                        pr_tasks = [self._safe_fetch_price_range(fetcher, s['code']) for s in results[:20]]
                        pr_results = await asyncio.gather(*pr_tasks, return_exceptions=True)
                        for i, s in enumerate(results[:20]):
                            pr = pr_results[i] if i < len(pr_results) else None
                            pr = pr if isinstance(pr, dict) else {}
                            if pr and pr.get('buy_low') and pr.get('sell_high'):
                                s['price_range'] = (
                                    f"买 {pr['buy_low']:.2f} ~ {pr['buy_high']:.2f}\n"
                                    f"卖 {pr['sell_low']:.2f} ~ {pr['sell_high']:.2f}"
                                )
                            else:
                                s.setdefault('price_range', '-')

                    logger.info('搜索“%s”完成，返回 %d 支结果', self.keyword, len(results))
                    return results
            r = loop.run_until_complete(do()); loop.close()
            self.result_ready.emit(r)
        except Exception as e:
            logger.exception('搜索“%s”失败: %s', self.keyword, e)
            self.result_ready.emit([])
            self.progress_update.emit(f"搜索出错: {str(e)}")

    @staticmethod
    async def _safe_fetch_price_changes(fetcher, code):
        """并行安全获取涨跌幅，异常静默返回空"""
        try:
            return await fetcher.fetch_price_changes(code) or {}
        except Exception:
            return {}

    @staticmethod
    async def _safe_fetch_price_range(fetcher, code):
        """并行安全获取买卖区间，异常静默返回空"""
        try:
            return await fetcher.fetch_price_range(code) or {}
        except Exception:
            return {}


class SuggestSearchThread(QThread):
    """在线搜索建议线程（股票列表未加载时使用）"""
    result_ready = pyqtSignal(list)

    def __init__(self, keyword):
        super().__init__()
        self.keyword = keyword

    def run(self):
        try:
            loop = new_async_loop()
            asyncio.set_event_loop(loop)
            async def do():
                async with MultiSourceFetcher() as f:
                    return await f.search_stock_suggest(self.keyword)
            r = loop.run_until_complete(do()); loop.close()
            self.result_ready.emit(r)
        except Exception:
            import traceback; traceback.print_exc()
            self.result_ready.emit([])


class CompleterPopup(QListView):
    item_selected = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setStyleSheet("""
            QListView { background-color: #16213E; color: white; border: 2px solid #F39C12;
                border-radius: 6px; font-size: 13px; outline: none; }
            QListView::item { padding: 8px 12px; border-bottom: 1px solid #2A2A4A; }
            QListView::item:hover { background-color: #1F3A5F; color: #F39C12; }
            QListView::item:selected { background-color: #0F3460; color: #F39C12; }
        """)
        self.clicked.connect(self._on_clicked)
        self._items = []

    def set_items(self, items):
        self._items = items
        model = QStringListModel()
        texts = []
        for item in items:
            code = item.get('code', '')
            name = item.get('name', '')
            py = get_pinyin_initials(name)
            texts.append(f"  {code}  {name}  ({py})")
        model.setStringList(texts)
        self.setModel(model)
        h = min(len(items), 10) * 36 + 4
        self.setFixedHeight(h)
        self.setFixedWidth(360)

    def _on_clicked(self, index):
        if 0 <= index.row() < len(self._items):
            self.item_selected.emit(self._items[index.row()])
            self.hide()

    def keyPressEvent(self, event):
        # 不让 popup 拦截键盘输入，所有键事件转发给父窗口的搜索输入框
        parent = self.parent()
        if parent and hasattr(parent, 'search_input'):
            parent.search_input.setFocus()
            from PyQt6.QtCore import QCoreApplication
            QCoreApplication.sendEvent(parent.search_input, event)
        return

    def mousePressEvent(self, event):
        # 点击 popup 时也不抢夺焦点，手动处理选中逻辑
        idx = self.indexAt(event.pos())
        if idx.isValid():
            self.setCurrentIndex(idx)
            self._on_clicked(idx)
        super().mousePressEvent(event)


class SearchTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_stocks = []
        self._completer_popup = None
        self._quick_timer = QTimer(); self._quick_timer.setSingleShot(True)
        self._quick_timer.timeout.connect(self._do_quick_search)
        self._suggest_timer = QTimer(); self._suggest_timer.setSingleShot(True)
        self._suggest_timer.timeout.connect(self._do_suggest_search)
        self._suggest_thread = None
        self._init_ui(); self._preload_stocks()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(10)

        sl = QHBoxLayout()
        title = QLabel("股票搜索")
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#F39C12;")
        sl.addWidget(title)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入股票名称、代码或拼音首字母搜索...")
        self.search_input.setStyleSheet("""
            QLineEdit { background-color: #16213E; color: white; border: 2px solid #2A2A4A;
                border-radius: 6px; padding: 10px 15px; font-size: 14px; min-width: 300px; }
            QLineEdit:focus { border-color: #F39C12; }
        """)
        self.search_input.textChanged.connect(self._on_text_changed)
        self.search_input.returnPressed.connect(self._on_return_pressed)
        sl.addWidget(self.search_input, 1)

        self.search_btn = QPushButton("搜索")
        self.search_btn.setStyleSheet("""
            QPushButton { background-color: #F39C12; color: #1A1A2E; border: none;
                border-radius: 6px; padding: 10px 24px; font-size: 14px; font-weight: bold; }
            QPushButton:hover { background-color: #F5B041; }
            QPushButton:disabled { background-color: #555; color: #999; }
        """)
        self.search_btn.clicked.connect(self._on_return_pressed)
        sl.addWidget(self.search_btn)
        layout.addLayout(sl)

        fl = QHBoxLayout()
        fl.addWidget(QLabel("市值筛选:", styleSheet="color:#AAAAAA; font-size:13px;"))
        self.cap_filter = QComboBox()
        self.cap_filter.addItems(['全部', '>50亿', '>80亿', '>100亿', '>200亿', '>500亿', '>1000亿'])
        self.cap_filter.setStyleSheet("""
            QComboBox { background-color: #16213E; color: white; border: 1px solid #2A2A4A;
                border-radius: 4px; padding: 6px 12px; font-size: 12px; min-width: 100px; }
            QComboBox::drop-down { border: none; }
        """)
        fl.addWidget(self.cap_filter)
        fl.addWidget(QLabel("板块:", styleSheet="color:#AAAAAA; font-size:13px;"))
        self.sector_filter = QComboBox()
        self.sector_filter.addItems(['全部', '主板', '创业板', '科创板'])
        self.sector_filter.setStyleSheet("""
            QComboBox { background-color: #16213E; color: white; border: 1px solid #2A2A4A;
                border-radius: 4px; padding: 6px 12px; font-size: 12px; min-width: 100px; }
            QComboBox::drop-down { border: none; }
        """)
        fl.addWidget(self.sector_filter); fl.addStretch()
        layout.addLayout(fl)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #2A2A4A; border-radius: 4px;
                background-color: #16213E; height: 20px; }
            QProgressBar::chunk { background-color: #F39C12; border-radius: 3px; }
        """)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("输入股票名称、代码或拼音首字母开始搜索")
        self.status_label.setStyleSheet("color:#AAAAAA; font-size:13px; padding:5px;")
        layout.addWidget(self.status_label)

        self.table = StockTableWidget(headers=HEADERS)
        layout.addWidget(self.table, 1)

    def _preload_stocks(self):
        retire_thread(self)
        self.thread = LoadStockListThread()
        self.thread.result_ready.connect(self._on_stocks_loaded)
        keep_alive(self.thread)
        self.thread.start()

    def _on_stocks_loaded(self, stocks):
        self._all_stocks = stocks
        # 预热拼音首字母缓存：避免首次按键对全量列表实时计算 pypinyin 造成卡顿
        if stocks:
            from data_fetcher import get_pinyin_initials_cached
            for s in stocks:
                get_pinyin_initials_cached(str(s.get('name', '')))
            self.status_label.setText(f"股票列表已加载 ({len(stocks)} 支)，可输入关键词搜索")

    def _on_text_changed(self, text):
        text = text.strip()
        if not text: self._hide_completer(); return
        if not self._all_stocks:
            # 股票列表未加载完成，使用在线搜索建议兜底
            self._suggest_timer.stop(); self._suggest_timer.start(350)
            return
        self._suggest_timer.stop()
        self._quick_timer.stop(); self._quick_timer.start(200)

    def _do_quick_search(self):
        text = self.search_input.text().strip()
        if not text: self._hide_completer(); return
        # 纯拼音输入（全小写英文，无数字/汉字）→ 本地列表不含深市，直接在线搜
        if text.isascii() and text.islower():
            self._hide_completer()
            self._do_suggest_search()
            return
        if not self._all_stocks:
            # 股票列表仍未就绪，调用在线建议
            self._do_suggest_search()
            return
        results = match_stock_by_pinyin(self._all_stocks, text)
        if results:
            self._show_completer(results[:10])
        else:
            # 本地匹配未命中（列表可能不含深市/后台未补充完），隐藏旧下拉兜底在线搜索
            self._hide_completer()
            self._do_suggest_search()

    def _do_suggest_search(self):
        """在线搜索建议（当本地股票列表未加载时使用）"""
        text = self.search_input.text().strip()
        if not text: return
        if self._suggest_thread:
            try: self._suggest_thread.result_ready.disconnect()
            except (TypeError, RuntimeError): pass
            retire_thread(self, '_suggest_thread')
        self._suggest_keyword = text  # 记录请求关键字，用于校验结果
        self._suggest_thread = SuggestSearchThread(text)
        self._suggest_thread.result_ready.connect(self._on_suggest_result)
        keep_alive(self._suggest_thread)
        self._suggest_thread.start()

    def _on_suggest_result(self, results):
        # 忽略过时结果：用户输入已变化
        if getattr(self, '_suggest_keyword', '') != self.search_input.text().strip():
            return
        if results:
            self._show_completer(results[:10])
        else:
            self._hide_completer()

    def _show_completer(self, items):
        if not items: self._hide_completer(); return
        if self._completer_popup is None:
            self._completer_popup = CompleterPopup(self)
            self._completer_popup.item_selected.connect(self._on_completer_selected)
        self._completer_popup.set_items(items)
        pos = self.search_input.mapToGlobal(self.search_input.rect().bottomLeft())
        self._completer_popup.move(pos); self._completer_popup.show()

    def _hide_completer(self):
        if self._completer_popup and self._completer_popup.isVisible():
            self._completer_popup.hide()

    def _on_completer_selected(self, stock):
        self._quick_timer.stop()
        self.search_input.setText(stock.get('name', ''))
        self._hide_completer()
        self._on_return_pressed()

    def _on_return_pressed(self):
        self._quick_timer.stop()
        self._suggest_timer.stop()
        self._hide_completer()
        kw = self.search_input.text().strip()
        if not kw: self.status_label.setText("请输入股票名称、代码或拼音首字母搜索"); return
        self.search_btn.setEnabled(False); self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0); self.status_label.setText(f"正在搜索: {kw} ...")
        retire_thread(self)
        self.thread = SearchDataThread(kw)
        self.thread.progress_update.connect(self._on_progress)
        self.thread.result_ready.connect(self._on_result)
        keep_alive(self.thread)
        self.thread.start()

    def _on_progress(self, msg): self.status_label.setText(msg)

    def _on_result(self, results):
        self.search_btn.setEnabled(True); self.progress_bar.setVisible(False)
        if results:
            cap_map = {'>50亿': 50, '>80亿': 80, '>100亿': 100, '>200亿': 200, '>500亿': 500, '>1000亿': 1000}
            ct = self.cap_filter.currentText()
            if ct in cap_map: results = [r for r in results if r.get('market_cap', 0) >= cap_map[ct]]
            sec = self.sector_filter.currentText()
            if sec == '创业板': results = [r for r in results if str(r.get('code', '')).startswith('3')]
            elif sec == '科创板': results = [r for r in results if str(r.get('code', '')).startswith('68')]
            elif sec == '主板': results = [r for r in results if not str(r.get('code', '')).startswith(('3', '68'))]
            if results:
                self.status_label.setText(f"找到 {len(results)} 支匹配股票")
                self.table.set_data(results, HEADER_MAP)
            else:
                self.status_label.setText("找到股票但过滤后无结果，请调整筛选条件")
                self.table.set_data([], HEADER_MAP)
        else:
            self.status_label.setText(f"未找到匹配 '{self.search_input.text()}' 的股票")
            self.table.set_data([], HEADER_MAP)

    def _terminate_thread(self, t):
        """直接终止 QThread（asyncio run_until_complete 不受 quit 控制）；
        kill_thread 保证线程真正结束后才返回，超时则保留引用避免运行中 QThread 被销毁"""
        if t is None:
            return
        kill_thread(t)

    def stop_thread(self):
        """断开工作线程信号连接并终止线程，防止退出时进程残留"""
        self._quick_timer.stop()
        self._suggest_timer.stop()
        if self._completer_popup:
            self._completer_popup.hide()
            self._completer_popup = None
        if hasattr(self, '_suggest_thread') and self._suggest_thread is not None:
            try:
                self._suggest_thread.result_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._terminate_thread(self._suggest_thread)
            self._suggest_thread = None
        # isinstance 检查：QObject 有内置 thread() 方法，hasattr 始终为 True
        if isinstance(getattr(self, 'thread', None), QThread):
            if hasattr(self.thread, 'progress_update'):
                try:
                    self.thread.progress_update.disconnect()
                except (TypeError, RuntimeError):
                    pass
            try:
                self.thread.result_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._terminate_thread(self.thread)
            self.thread = None
