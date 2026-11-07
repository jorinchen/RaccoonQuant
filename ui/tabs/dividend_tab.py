"""
分红推荐模块 - 推荐明日及之后要分红的科技股
"""
import asyncio
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar
from PyQt6.QtCore import QThread, pyqtSignal
from data_fetcher import MultiSourceFetcher, new_async_loop
from stock_filter import analyze_dividend_picks
from stock_list_loader import load_stock_list
from log_manager import get_logger
from ui.widgets.stock_table import StockTableWidget
from ui.thread_utils import keep_alive, kill_thread, retire_thread

logger = get_logger('dividend')


def _to_float(val, default=0.0):
    """行情/列表接口偶发返回 '-'、'--'、'' 等脏值，统一安全转 float，避免比较时崩溃"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

HEADERS = ['代码', '名称', '现价', '含税前', '含税/扣税后', '送转股', '分红日期', '买卖区间', '板块概念']
HEADER_MAP = {
    '代码': 'code', '名称': 'name', '现价': 'current',
    '含税前': 'dividend', '含税/扣税后': 'after_tax', '送转股': 'bonus_share',
    '分红日期': 'div_date',
    '买卖区间': 'price_range', '板块概念': 'concept',
}


class DividendAnalysisThread(QThread):
    progress = pyqtSignal(str)
    result_ready = pyqtSignal(list)

    def run(self):
        try:
            logger.info('开始执行分红推荐分析')
            loop = new_async_loop()
            asyncio.set_event_loop(loop)

            async def do_analysis():
                async with MultiSourceFetcher() as fetcher:
                    # 1. 加载股票列表（用于过滤出主板非 ST/创业板/科创板候选代码）
                    self.progress.emit("正在加载股票列表...")
                    stocks, _src = await load_stock_list(fetcher, progress_cb=self.progress.emit)
                    logger.info('股票列表加载完成：%d 支，来源=%s', len(stocks), _src)

                    # 2. 主板非 ST/创业板/科创板过滤
                    candidate_codes = [s['code'] for s in stocks
                                       if s.get('code', '').isdigit()
                                       and self._is_a_share_ok(s)]
                    logger.info('主板候选股：%d 支', len(candidate_codes))

                    if not candidate_codes:
                        return []

                    # 3. 双接口结合拉取（主源 RPT_LICO_FN_CPD + 副源 np-anotice-stock 真实除权日）
                    self.progress.emit(f"正在扫描 {len(candidate_codes)} 支候选股的分红预案...")
                    dividend_items = await fetcher.fetch_dividend_calendar(
                        codes=candidate_codes,
                        start_date_str='', lookback_days=540, batch_size=80)
                    logger.info('分红数据获取完成：%d 条（主源含每10股派X元 + 副源含真实除权日）',
                                len(dividend_items))

                    if not dividend_items:
                        return []

                    dividend_map = {d['code']: d for d in dividend_items}

                    # 4. 从候选中筛选出现在 dividend_map 的股票
                    candidates = [s for s in stocks if s.get('code', '') in dividend_map]
                    logger.info('主板分红候选股：%d 支', len(candidates))

                    if not candidates:
                        return []

                    # 4. 拉取实时行情，补全 current/market_cap
                    self.progress.emit(f"正在获取 {len(candidates)} 支分红候选行情...")
                    codes = [s['code'] for s in candidates]
                    quotes = await fetcher.fetch_real_time_quotes(codes)
                    logger.info('行情获取完成：%d 支', len(quotes))
                    for s in candidates:
                        q = quotes.get(s['code'])
                        if not q:
                            continue
                        price = _to_float(q.get('current', 0))
                        if price <= 0:
                            continue
                        s['current'] = price
                        s['change_pct'] = _to_float(q.get('change_pct', 0))
                        s['yesterday_close'] = _to_float(q.get('yesterday_close', 0))
                        s['market_cap'] = _to_float(q.get('market_cap', 0)) or _to_float(s.get('market_cap', 0))
                        s['pe'] = _to_float(q.get('pe', 0)) or _to_float(s.get('pe', 0))
                    # 移除无价格的
                    candidates = [s for s in candidates if s.get('current', 0) > 0]

                    # 5. 拉概念（用于科技股判定）
                    self.progress.emit("正在获取板块概念...")
                    csem = asyncio.Semaphore(10)

                    async def _concept(code):
                        async with csem:
                            return code, await fetcher.fetch_stock_concepts(code)
                    cres = await asyncio.gather(*[_concept(s['code']) for s in candidates])
                    concepts = {code: con for code, con in cres if con}

                    # 6. 拉财务数据（财报正增长 + 现金流 + 负债率）
                    self.progress.emit("正在获取财务数据...")
                    fin_sem = asyncio.Semaphore(8)

                    async def _finance(code):
                        async with fin_sem:
                            return code, await fetcher.fetch_financial_data(code)
                    fres = await asyncio.gather(*[_finance(s['code']) for s in candidates])
                    finances = {code: fin for code, fin in fres if fin}
                    logger.info('财务数据获取完成：%d 支', len(finances))

                    # 7. 量化筛选
                    self.progress.emit("正在执行分红量化分析...")
                    results = analyze_dividend_picks(
                        candidates, dividend_map, concepts, finances)
                    logger.info('分红分析完成，生成候选 %d 支', len(results))

                    # 8. 计算买卖区间
                    self.progress.emit("正在计算买卖区间...")
                    for r in results:
                        try:
                            pr = await fetcher.fetch_price_range(r['code'])
                            if pr and pr.get('buy_low') and pr.get('sell_high'):
                                r['price_range'] = (
                                    f"买 {pr['buy_low']:.2f} ~ {pr['buy_high']:.2f}\n"
                                    f"卖 {pr['sell_low']:.2f} ~ {pr['sell_high']:.2f}"
                                )
                            else:
                                r['price_range'] = '-'
                        except Exception:
                            r['price_range'] = '-'

                    return results

            results = loop.run_until_complete(do_analysis())
            loop.close()
            logger.info('分红推荐完成，推荐 %d 支：%s',
                        len(results),
                        [(r.get('code'), r.get('name'), r.get('dividend'))
                         for r in results])
            self.result_ready.emit(results)
        except Exception as e:
            logger.exception('分红推荐分析失败: %s', e)
            self.result_ready.emit([])
            self.progress.emit(f"分析出错: {str(e)}")

    @staticmethod
    def _is_a_share_ok(stock):
        """主板非 ST/非创业板/非科创板（避免 import 循环，直接复用判定）"""
        from stock_filter import _is_a_share_eligible
        return _is_a_share_eligible(stock)


class DividendTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("分红推荐")
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#F39C12;")
        top.addWidget(title)

        desc = QLabel("未来真实除权分红 | 每股>0.2元 | 主板 | 市值>100亿 | 股价>10元 | 排除ST/创业板/科创板 | 财报正增长（双击行可查看详情）")
        desc.setStyleSheet("color:#AAAAAA; font-size:13px; padding-left:15px;")
        top.addWidget(desc)
        top.addStretch()

        self.run_btn = QPushButton("执行分析")
        self.run_btn.setStyleSheet("""
            QPushButton { background-color: #F39C12; color: #1A1A2E; border: none;
                border-radius: 6px; padding: 10px 24px; font-size: 14px; font-weight: bold; }
            QPushButton:hover { background-color: #F5B041; }
            QPushButton:disabled { background-color: #555; color: #999; }
        """)
        self.run_btn.clicked.connect(self.run_analysis)
        top.addWidget(self.run_btn)
        layout.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar { border: 1px solid #2A2A4A; border-radius: 4px;
                background-color: #16213E; height: 20px; }
            QProgressBar::chunk { background-color: #F39C12; border-radius: 3px; }
        """)
        layout.addWidget(self.progress)

        self.status = QLabel('点击"执行分析"获取近期分红推荐（双击行可查看详情）')
        self.status.setStyleSheet("color:#AAAAAA; font-size:13px; padding:5px;")
        layout.addWidget(self.status)

        self.table = StockTableWidget(headers=HEADERS)
        layout.addWidget(self.table, 1)

    def run_analysis(self):
        self.run_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("正在获取分红数据，请稍候...")
        retire_thread(self)
        self.thread = DividendAnalysisThread()
        self.thread.progress.connect(self._on_progress)
        self.thread.result_ready.connect(self._on_result)
        keep_alive(self.thread)
        self.thread.start()

    def _on_progress(self, msg):
        self.status.setText(msg)

    def _on_result(self, results):
        self.run_btn.setEnabled(True)
        self.progress.setVisible(False)
        if results:
            self.status.setText(f"分红分析完成，推荐 {len(results)} 支分红科技股")
            self.table.set_data(results, HEADER_MAP)
        else:
            self.status.setText("未找到符合条件的分红股（非分红季或数据获取异常）")
            self.table.set_data([], HEADER_MAP)

    def stop_thread(self):
        """断开工作线程信号连接并终止线程"""
        if isinstance(getattr(self, 'thread', None), QThread):
            if hasattr(self.thread, 'progress'):
                try:
                    self.thread.progress.disconnect()
                except (TypeError, RuntimeError):
                    pass
            if hasattr(self.thread, 'result_ready'):
                try:
                    self.thread.result_ready.disconnect()
                except (TypeError, RuntimeError):
                    pass
            kill_thread(self.thread)
            self.thread = None