"""
每日推荐模块 - 科技股超跌发现，基本面好+持续下跌的科技股
"""
import asyncio
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar
from PyQt6.QtCore import QThread, pyqtSignal
from data_fetcher import MultiSourceFetcher, new_async_loop
from stock_filter import analyze_daily_tech_picks
from stock_list_loader import load_stock_list
from log_manager import get_logger
from ui.widgets.stock_table import StockTableWidget
from ui.thread_utils import keep_alive, kill_thread, retire_thread

logger = get_logger('daily')


def _to_float(val, default=0.0):
    """行情/列表接口偶发返回 '-'、'--'、'' 等脏值，统一安全转 float，避免比较时崩溃"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

HEADERS = ['代码', '名称', '现价', '昨日收盘价', '涨跌幅', '市值', '买卖区间', '板块概念', '推荐缘由']
HEADER_MAP = {
    '代码': 'code', '名称': 'name', '现价': 'current',
    '昨日收盘价': 'yesterday_close', '涨跌幅': 'change_pct',
    '市值': 'market_cap', '买卖区间': 'price_range', '板块概念': 'concept', '推荐缘由': 'reason',
}


class DailyPickThread(QThread):
    progress = pyqtSignal(str)
    result_ready = pyqtSignal(list)

    def run(self):
        try:
            logger.info('开始执行每日推荐分析')
            loop = new_async_loop()
            asyncio.set_event_loop(loop)

            async def do_analysis():
                async with MultiSourceFetcher() as fetcher:
                    self.progress.emit("正在加载股票列表...")
                    stocks, _src = await load_stock_list(fetcher, progress_cb=self.progress.emit)
                    logger.info('股票列表加载完成：%d 支，来源=%s', len(stocks), _src)

                    if not stocks:
                        self.progress.emit("股票列表为空，请检查网络或先更新股票列表")
                        return []

                    self.progress.emit(f"已加载 {len(stocks)} 支股票，正在获取实时行情...")
                    codes = [s['code'] for s in stocks]
                    quotes = await fetcher.fetch_real_time_quotes(codes)
                    logger.info('实时行情获取完成：%d 支', len(quotes))

                    # 合并行情到股票数据
                    candidates = []
                    for s in stocks:
                        code = s['code']
                        if code in quotes:
                            q = quotes[code]
                            price = _to_float(q.get('current', 0))
                            # 跳过无价格的
                            if price <= 0:
                                continue
                            s['current'] = price
                            s['change_pct'] = _to_float(q.get('change_pct', 0))
                            s['yesterday_close'] = _to_float(q.get('yesterday_close', 0))
                            s['turnover_rate'] = _to_float(q.get('turnover_rate', 0))
                            s['amount'] = _to_float(q.get('amount', 0))
                            s['volume_ratio'] = _to_float(q.get('volume_ratio', 0))
                            s['pe'] = _to_float(q.get('pe', 0)) or _to_float(s.get('pe', 0))
                            # 用腾讯行情市值(亿元)覆盖股票列表值
                            s['market_cap'] = _to_float(q.get('market_cap', 0)) or _to_float(s.get('market_cap', 0))
                            candidates.append(s)

                    self.progress.emit(f"有效候选 {len(candidates)} 支，正在筛选科技股...")
                    logger.info('价格/市盈率初筛后有效候选：%d 支', len(candidates))

                    # 两轮筛选科技股
                    # 第一轮：名称关键词快速匹配 → 直接进候选
                    from stock_filter import _is_tech_stock
                    tech_fast = []
                    need_concept_check = []
                    for s in candidates:
                        price = s.get('current', 0)
                        pe = s.get('pe', 0)
                        if price < 5 or price > 30:
                            continue
                        if pe <= 0 or pe > 40:
                            continue
                        if _is_tech_stock(s.get('name', '')):
                            tech_fast.append(s)
                        else:
                            need_concept_check.append(s)

                    self.progress.emit(f"名称匹配 {len(tech_fast)} 支，待概念验证 {len(need_concept_check)} 支")

                    # 第二轮的候选先获取概念数据再过滤（限制数量）
                    need_check = need_concept_check[:150]
                    csem = asyncio.Semaphore(10)
                    async def _concept(code):
                        async with csem:
                            return code, await fetcher.fetch_stock_concepts(code)
                    cres = await asyncio.gather(*[_concept(s['code']) for s in need_check])
                    concepts = {code: con for code, con in cres if con}

                    # 通过概念判断科技股
                    tech_by_concept = []
                    for s in need_check:
                        code = s['code']
                        con_list = concepts.get(code, [])
                        if _is_tech_stock(s.get('name', ''), con_list):
                            tech_by_concept.append(s)

                    self.progress.emit(f"概念匹配 {len(tech_by_concept)} 支，总计科技股候选 {len(tech_fast) + len(tech_by_concept)} 支")
                    logger.info('科技股筛选：名称匹配 %d 支 + 概念匹配 %d 支，合计 %d 支',
                                len(tech_fast), len(tech_by_concept),
                                len(tech_fast) + len(tech_by_concept))

                    tech_candidates = tech_fast + tech_by_concept

                    self.progress.emit(f"科技股候选 {len(tech_candidates)} 支，正在获取K线数据...")

                    # 获取K线数据（并发）
                    ksem = asyncio.Semaphore(15)
                    async def _kline_daily(code):
                        async with ksem:
                            return code, await fetcher.fetch_kline_data(code, count=60)
                    kres = await asyncio.gather(*[_kline_daily(s['code']) for s in tech_candidates[:200]])
                    klines = {code: kline for code, kline in kres if kline}
                    logger.info('K线数据获取完成：%d 支', len(klines))

                    self.progress.emit(f"已获取 {len(klines)} 支K线，正在补充概念数据...")

                    # 为名称匹配的快速通道股票也获取概念（并发）
                    need_more = [s for s in tech_fast[:100] if s['code'] not in concepts]
                    async def _concept_fast(code):
                        async with csem:
                            return code, await fetcher.fetch_stock_concepts(code)
                    c2 = await asyncio.gather(*[_concept_fast(s['code']) for s in need_more])
                    for code, con in c2:
                        if con:
                            concepts[code] = con

                    self.progress.emit("正在获取市场热点板块...")
                    hot_boards = await fetcher.fetch_hot_boards(12)
                    logger.info('热点板块获取完成：%d 个', len(hot_boards) if hot_boards else 0)
                    hot_codes = set()
                    hot_keywords = [b['name'] for b in hot_boards] if hot_boards else []
                    if hot_boards:
                        bsem = asyncio.Semaphore(5)

                        async def _members(b):
                            async with bsem:
                                return await fetcher.fetch_board_members(b['code'], 60)

                        mres = await asyncio.gather(*[_members(b) for b in hot_boards])
                        for ms in mres:
                            for m in ms:
                                hot_codes.add(m['code'])
                        self.progress.emit(f"热点板块 {len(hot_boards)} 个，成分股 {len(hot_codes)} 支")
                    logger.info('热点板块成分股：%d 支，关键词 %d 个', len(hot_codes), len(hot_keywords))

                    self.progress.emit("正在获取市场消息与情绪...")
                    news_text = ''
                    try:
                        tel = await fetcher.fetch_telegraph(count=100)
                        if tel:
                            news_text = '\n'.join(
                                str(x.get('title') or x.get('content') or '') for x in tel)
                    except Exception:
                        pass

                    self.progress.emit("正在获取财务与资金数据...")
                    fcodes = [s['code'] for s in tech_candidates if s['code'] in klines]
                    fin_sem = asyncio.Semaphore(8)

                    async def _finance(code):
                        async with fin_sem:
                            return code, await fetcher.fetch_financial_data(code)

                    fres = await asyncio.gather(*[_finance(c) for c in fcodes])
                    finances = {code: fin for code, fin in fres if fin}
                    logger.info('财务数据获取完成：%d 支', len(finances))

                    flow_sem = asyncio.Semaphore(8)

                    async def _flow(code):
                        async with flow_sem:
                            return code, await fetcher.fetch_money_flow(code)

                    flow_res = await asyncio.gather(*[_flow(c) for c in fcodes])
                    flows = {code: f for code, f in flow_res if f}
                    logger.info('资金流向获取完成：%d 支', len(flows))

                    self.progress.emit("正在执行量化分析...")
                    results = analyze_daily_tech_picks(
                        tech_candidates, klines, concepts,
                        finances=finances, flows=flows,
                        hot_codes=hot_codes, hot_keywords=hot_keywords,
                        news_text=news_text)
                    logger.info('每日推荐量化分析完成，生成候选 %d 支', len(results))

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

                    return results

            results = loop.run_until_complete(do_analysis())
            loop.close()
            logger.info('每日推荐分析完成，推荐 %d 支：%s',
                        len(results),
                        [(r.get('code'), r.get('name'), round(r.get('score', 0), 2)) for r in results])
            self.result_ready.emit(results)
        except Exception as e:
            logger.exception('每日推荐分析失败: %s', e)
            self.progress.emit(f"分析出错: {str(e)}")
            self.result_ready.emit([])


class DailyPickTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # 标题栏
        top = QHBoxLayout()
        title = QLabel("每日推荐")
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#F39C12;")
        top.addWidget(title)

        desc = QLabel("超跌科技股 | 主板 | 市值>100亿 | 股价5~30元 | 排除ST/创业板/科创板 | 财报正增长 | 现金流充足 | 负债率<70%")
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

        # 进度条
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar { border: 1px solid #2A2A4A; border-radius: 4px;
                background-color: #16213E; height: 20px; }
            QProgressBar::chunk { background-color: #F39C12; border-radius: 3px; }
        """)
        layout.addWidget(self.progress)

        # 状态
        self.status = QLabel('点击"执行分析"获取每日科技股推荐')
        self.status.setStyleSheet("color:#AAAAAA; font-size:13px; padding:5px;")
        layout.addWidget(self.status)

        # 表格
        self.table = StockTableWidget(headers=HEADERS)
        layout.addWidget(self.table, 1)

    def run_analysis(self):
        self.run_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("正在分析数据，请稍候...")
        retire_thread(self)
        self.thread = DailyPickThread()
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
            self.status.setText(f"分析完成，推荐 {len(results)} 支超跌热点科技股")
            self.table.set_data(results, HEADER_MAP)
        else:
            self.status.setText("未找到符合条件的科技股（市场可能处于强势期或数据获取异常）")
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
