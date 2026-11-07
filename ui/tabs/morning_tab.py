"""
早盘竞价模块 - 推荐3支涨停潜力股 + 推荐缘由
"""
import asyncio
from datetime import datetime, time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from data_fetcher import MultiSourceFetcher, new_async_loop
from stock_filter import analyze_morning_auction
from stock_list_loader import load_stock_list
from log_manager import get_logger
from ui.widgets.stock_table import StockTableWidget
from ui.thread_utils import keep_alive, kill_thread, retire_thread

logger = get_logger('morning')


def _to_float(val, default=0.0):
    """行情/列表接口偶发返回 '-'、'--'、'' 等脏值，统一安全转 float，避免比较时崩溃"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

HEADERS = ['代码', '名称', '竞价涨幅', '实时涨幅', '昨日收盘价', '现价',
           '换手率', '成交额', '量比', '市值', '主力净额', '5日涨幅', '10日涨幅',
           '20日涨幅', '动态市盈', '买卖区间', '板块概念', '推荐缘由']
HEADER_MAP = {
    '代码': 'code', '名称': 'name',
    '竞价涨幅': 'auction_change', '实时涨幅': 'real_time_change',
    '昨日收盘价': 'yesterday_close', '现价': 'current',
    '换手率': 'turnover_rate', '成交额': 'amount',
    '量比': 'volume_ratio', '市值': 'market_cap', '主力净额': 'main_net',
    '5日涨幅': 'change_5d', '10日涨幅': 'change_10d',
    '20日涨幅': 'change_20d', '动态市盈': 'pe', '买卖区间': 'price_range', '板块概念': 'sector_concept', '推荐缘由': 'reason',
}


class MorningAnalysisThread(QThread):
    progress = pyqtSignal(str)
    result_ready = pyqtSignal(list)

    def run(self):
        try:
            logger.info('开始执行早盘竞价分析')
            loop = new_async_loop()
            asyncio.set_event_loop(loop)

            async def do_analysis():
                async with MultiSourceFetcher() as fetcher:
                    self.progress.emit("正在加载股票列表...")
                    stocks, _src = await load_stock_list(fetcher, progress_cb=self.progress.emit)
                    logger.info('股票列表加载完成：%d 支，来源=%s', len(stocks), _src)

                    # 不在此处按市值过滤：TDX 返回的列表中 market_cap=0；
                    # 市值数据在 fetch_real_time_quotes 中再补充
                    candidates = stocks

                    self.progress.emit(f"正在获取 {len(candidates)} 支股票实时行情...")
                    codes = [s['code'] for s in candidates]
                    quotes = await fetcher.fetch_real_time_quotes(codes)
                    logger.info('实时行情获取完成：%d 支', len(quotes))

                    # 合并数据
                    for s in candidates:
                        code = s['code']
                        if code in quotes:
                            q = quotes[code]
                            s['current'] = _to_float(q.get('current', 0))
                            s['change_pct'] = _to_float(q.get('change_pct', 0))
                            s['yesterday_close'] = _to_float(q.get('yesterday_close', 0))
                            s['turnover_rate'] = _to_float(q.get('turnover_rate', 0))
                            s['amount'] = _to_float(q.get('amount', 0))
                            s['volume_ratio'] = _to_float(q.get('volume_ratio', 0))
                            s['pe'] = _to_float(q.get('pe', 0))
                            mc = _to_float(q.get('market_cap', 0))
                            if mc > 0:
                                s['market_cap'] = mc
                            op = _to_float(q.get('open', 0))
                            yc = _to_float(q.get('yesterday_close', 0))
                            s['auction_change'] = ((op - yc) / yc * 100) if yc > 0 else 0

                    # 筛选竞价条件：竞价涨幅 0~5%（不能涨停，最多涨5个点）
                    total = len(candidates)
                    mc_zero = sum(1 for s in candidates if s.get('market_cap', 0) <= 0)
                    min_mc = 0 if (total > 0 and mc_zero > total * 0.8) else 80

                    filtered = [s for s in candidates if
                                s.get('market_cap', 0) >= min_mc and
                                0 < s.get('auction_change', 0) <= 5.0 and
                                s.get('volume_ratio', 0) > 0.5]

                    if not filtered:
                        filtered = [s for s in candidates if
                                    s.get('market_cap', 0) >= min_mc and
                                    0 < s.get('auction_change', 0) <= 5.0]
                    logger.info('竞价条件筛选后候选股：%d 支（取前 100 分析）', len(filtered))

                    batch = filtered[:100]
                    sem = asyncio.Semaphore(15)

                    self.progress.emit(f"正在获取 {len(batch)} 支候选股K线数据...")
                    async def _kline(code):
                        async with sem:
                            return code, await fetcher.fetch_kline_data(code, count=30)
                    kres = await asyncio.gather(*[_kline(s['code']) for s in batch])
                    klines = {code: kline for code, kline in kres if kline}
                    logger.info('K线数据获取完成：%d 支', len(klines))

                    self.progress.emit("正在获取资金流向...")
                    async def _flow(code):
                        async with sem:
                            return code, await fetcher.fetch_money_flow(code)
                    fres = await asyncio.gather(*[_flow(s['code']) for s in batch])
                    flow_data = {code: flow for code, flow in fres if flow}
                    logger.info('资金流向获取完成：%d 支', len(flow_data))

                    self.progress.emit("正在获取涨跌幅...")
                    async def _changes(code):
                        async with sem:
                            return code, await fetcher.fetch_price_changes(code)
                    cres = await asyncio.gather(*[_changes(s['code']) for s in batch])
                    changes = {code: chg for code, chg in cres if chg}
                    logger.info('涨跌幅数据获取完成：%d 支', len(changes))

                    # 量化分析
                    self.progress.emit("正在执行量化分析...")
                    results = analyze_morning_auction(batch, klines, flow_data, changes)
                    logger.info('量化分析完成，生成候选 %d 支', len(results))

                    # 获取板块概念
                    self.progress.emit("正在获取板块概念...")
                    for r in results:
                        con = await fetcher.fetch_stock_concepts(r['code'])
                        r['sector_concept'] = '、'.join(con) if con else '-'

                    # 获取买卖区间（基于20日K线支撑/阻力位）
                    self.progress.emit("正在计算买卖区间...")
                    for r in results:
                        pr = await fetcher.fetch_price_range(r['code'])
                        if pr and pr.get('buy_low') and pr.get('sell_high'):
                            r['price_range'] = (
                                f"买 {pr['buy_low']:.2f} ~ {pr['buy_high']:.2f}\n"
                                f"卖 {pr['sell_low']:.2f} ~ {pr['sell_high']:.2f}"
                            )
                        else:
                            r['price_range'] = '-'

                    # 按评分排序取前3
                    results.sort(key=lambda x: x.get('score', 0), reverse=True)
                    return results[:3]

            results = loop.run_until_complete(do_analysis())
            loop.close()
            logger.info('早盘竞价分析完成，推荐 %d 支：%s',
                        len(results),
                        [(r.get('code'), r.get('name'), round(r.get('score', 0), 2)) for r in results])
            self.result_ready.emit(results)
        except Exception as e:
            logger.exception('早盘竞价分析失败: %s', e)
            self.result_ready.emit([])
            self.progress.emit(f"分析出错: {str(e)}")


class MorningTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("早盘竞价推荐")
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#F39C12;")
        top.addWidget(title)
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

        self.status = QLabel('点击"执行分析"或等待9:25自动触发')
        self.status.setStyleSheet("color:#AAAAAA; font-size:13px; padding:5px;")
        layout.addWidget(self.status)

        self.table = StockTableWidget(headers=HEADERS)
        layout.addWidget(self.table, 1)

    def _check_time(self):
        """早盘竞价只能在 9:25 之后执行"""
        now = datetime.now()
        if now.time() < time(9, 25):
            self.status.setText(f"需等到 9:25 后才能执行早盘竞价分析（当前 {now.strftime('%H:%M')}）")
            return False
        return True

    def run_analysis(self):
        if not self._check_time():
            return
        self.run_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("正在分析早盘竞价...")
        retire_thread(self)
        self.thread = MorningAnalysisThread()
        self.thread.progress.connect(self._on_progress)
        self.thread.result_ready.connect(self._on_result)
        keep_alive(self.thread)  # 全局引用兜底：竞态误杀 self.thread 也不会销毁运行中的 QThread
        self.thread.start()

    def _on_progress(self, msg):
        self.status.setText(msg)

    def _on_result(self, results):
        self.run_btn.setEnabled(True)
        self.progress.setVisible(False)
        if results:
            self.status.setText(f"早盘竞价分析完成，推荐 {len(results)} 支股票")
            self.table.set_data(results, HEADER_MAP)
        else:
            self.status.setText("未找到符合条件的股票（非交易时段正常现象）")
            self.table.set_data([], HEADER_MAP)

    def stop_thread(self):
        """断开工作线程信号连接并终止线程，防止退出时进程残留"""
        # isinstance 检查：QObject 有内置 thread() 方法，hasattr 始终为 True
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
            # 直接终止（asyncio run_until_complete 不受 quit 控制）；
            # kill_thread 保证线程真正结束后才允许置 None，超时则保留引用，避免运行中 QThread 被销毁
            kill_thread(self.thread)
            self.thread = None
