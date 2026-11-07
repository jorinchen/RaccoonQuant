"""
首板推荐模块 - 收盘时段寻找"今日首次涨停"的科技股 + 推荐缘由
"""
import asyncio
from datetime import datetime, time
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar
from PyQt6.QtCore import QThread, pyqtSignal
from data_fetcher import MultiSourceFetcher, new_async_loop
from stock_filter import analyze_first_limit_up, _is_a_share_eligible
from stock_list_loader import load_stock_list
from log_manager import get_logger
from ui.widgets.stock_table import StockTableWidget
from ui.thread_utils import keep_alive, kill_thread, retire_thread

logger = get_logger('closing')


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


class ClosingAnalysisThread(QThread):
    progress = pyqtSignal(str)
    result_ready = pyqtSignal(list)

    def run(self):
        try:
            logger.info('开始执行首板推荐分析')
            loop = new_async_loop()
            asyncio.set_event_loop(loop)

            async def do_analysis():
                async with MultiSourceFetcher() as fetcher:
                    # 收盘后 3 分钟内，等待数据稳定再拉取
                    now = datetime.now()
                    if time(15, 0) <= now.time() <= time(15, 3):
                        wait = max(0, 3 - (now - now.replace(hour=15, minute=0, second=0, microsecond=0)).total_seconds())
                        if wait > 0:
                            self.progress.emit(f"收盘数据正在结算，{wait:.0f} 秒后获取首板候选...")
                            await asyncio.sleep(wait)

                    self.progress.emit("正在加载股票列表...")
                    stocks, _src = await load_stock_list(fetcher, progress_cb=self.progress.emit)
                    logger.info('股票列表加载完成：%d 支，来源=%s', len(stocks), _src)

                    # 不在此处按市值过滤：TDX 返回的列表中 market_cap=0；
                    # 市值数据会在 fetch_real_time_quotes 中通过腾讯 API 补充，之后再统一过滤
                    candidates = stocks

                    self.progress.emit(f"正在获取 {len(candidates)} 支股票行情，筛选首板候选...")
                    codes = [s['code'] for s in candidates]
                    quotes = await fetcher.fetch_real_time_quotes(codes)

                    # 重试：如果行情为空，等待后重试
                    retry = 0
                    while not quotes and retry < 3:
                        retry += 1
                        self.progress.emit(f"行情数据暂未就绪，第 {retry} 次重试...")
                        await asyncio.sleep(3)
                        quotes = await fetcher.fetch_real_time_quotes(codes)
                    logger.info('首板分析行情获取完成：%d 支（重试 %d 次）', len(quotes), retry)

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
                            s['real_time_change'] = s.get('change_pct', 0)

                    # 收盘后 qt API 可能缺量比 → TDX 兜底补充
                    missing_vr = [s for s in candidates if s.get('volume_ratio', 0) <= 0
                                  and s.get('current', 0) > 0]
                    if missing_vr and len(missing_vr) > len(candidates) * 0.3:
                        try:
                            from tdx_fetcher import get_quotes as tdx_get_quotes
                            tdx_codes = [s['code'] for s in missing_vr]
                            tdx_quotes = await asyncio.to_thread(tdx_get_quotes, tdx_codes)
                            for s in missing_vr:
                                if s['code'] in tdx_quotes:
                                    tq = tdx_quotes[s['code']]
                                    vr = _to_float(tq.get('volume_ratio', 0))
                                    if vr > 0:
                                        s['volume_ratio'] = vr
                                    tc = _to_float(tq.get('change_pct', 0))
                                    if s.get('change_pct', 0) == 0 and tc != 0:
                                        s['change_pct'] = tc
                            self.progress.emit(f"已通过 TDX 补充 {len(tdx_quotes)} 支股票数据...")
                        except Exception:
                            pass

                    # 首板涨停：先排除 ST 股、创业板、科创板（仅保留主板）
                    pre_count = len(candidates)
                    candidates = [s for s in candidates if _is_a_share_eligible(s)]
                    logger.info('主板/ST 过滤：%d → %d 支', pre_count, len(candidates))

                    # 若超 80% 股票市值仍为 0（TDX 列表 + 腾讯 API 也缺失），跳过市值过滤
                    total = len(candidates)
                    mc_zero = sum(1 for s in candidates if s.get('market_cap', 0) <= 0)
                    skip_mc = total > 0 and mc_zero > total * 0.8

                    # 首板涨停粗筛：主板 + 市值 > 100 亿 + 股价 > 5 元 + 涨幅 >= 9.5%
                    filtered = [s for s in candidates if
                                (s.get('market_cap', 0) >= 100 or skip_mc) and
                                s.get('current', 0) > 5 and
                                s.get('change_pct', 0) >= 9.5]
                    logger.info('首板涨停粗筛：%d 支', len(filtered))

                    batch = filtered[:150]
                    sem = asyncio.Semaphore(15)

                    self.progress.emit(f"正在获取 {len(batch)} 支候选股K线数据...")
                    async def _kline(code):
                        async with sem:
                            return code, await fetcher.fetch_kline_data(code, count=30)
                    kres = await asyncio.gather(*[_kline(s['code']) for s in batch])
                    klines = {code: kline for code, kline in kres if kline}
                    logger.info('K线数据获取完成：%d 支', len(klines))

                    # 获取候选股概念（用于科技股判定与最终展示）
                    self.progress.emit("正在获取候选股概念标签...")
                    csem = asyncio.Semaphore(10)
                    async def _concept(code):
                        async with csem:
                            return code, await fetcher.fetch_stock_concepts(code)
                    cres = await asyncio.gather(*[_concept(s['code']) for s in batch])
                    concepts = {code: con for code, con in cres if con}
                    logger.info('候选股概念获取完成：%d 支', len(concepts))

                    # 获取热点板块与成分股（用于热点命中）
                    self.progress.emit("正在获取市场热点板块...")
                    hot_boards = []
                    try:
                        hot_boards = await fetcher.fetch_hot_boards(12) or []
                    except Exception as e:
                        logger.warning('获取热点板块失败: %s', e)
                    hot_codes = set()
                    hot_keywords = [b['name'] for b in hot_boards] if hot_boards else []
                    if hot_boards:
                        bsem = asyncio.Semaphore(5)
                        async def _members(b):
                            async with bsem:
                                try:
                                    return await fetcher.fetch_board_members(b['code'], 60)
                                except Exception:
                                    return []
                        mres = await asyncio.gather(*[_members(b) for b in hot_boards])
                        for ms in mres:
                            for m in ms:
                                hot_codes.add(m['code'])
                    logger.info('热点板块：%d 个，成分股 %d 支', len(hot_boards), len(hot_codes))

                    # 获取市场消息（消息面活跃度）
                    self.progress.emit("正在获取市场消息与情绪...")
                    news_text = ''
                    try:
                        tel = await fetcher.fetch_telegraph(count=100)
                        if tel:
                            news_text = '\n'.join(
                                str(x.get('title') or x.get('content') or '') for x in tel)
                    except Exception as e:
                        logger.warning('获取电报快讯失败: %s', e)

                    # 批量拉取资金流向与涨跌幅（用于补全表格字段）
                    self.progress.emit("正在获取资金流向与涨跌幅...")
                    flow_data = {}
                    changes = {}
                    try:
                        async def _flow(code):
                            async with sem:
                                return code, await fetcher.fetch_money_flow(code)
                        fres = await asyncio.gather(*[_flow(s['code']) for s in batch])
                        flow_data = {code: flow for code, flow in fres if flow}
                    except Exception as e:
                        logger.warning('获取资金流向失败: %s', e)
                    try:
                        async def _changes(code):
                            async with sem:
                                return code, await fetcher.fetch_price_changes(code)
                        cres = await asyncio.gather(*[_changes(s['code']) for s in batch])
                        changes = {code: chg for code, chg in cres if chg}
                    except Exception as e:
                        logger.warning('获取涨跌幅失败: %s', e)
                    logger.info('首板资金流向 %d 条，涨跌幅 %d 条', len(flow_data), len(changes))

                    self.progress.emit("正在执行首板涨停量化分析...")
                    results = analyze_first_limit_up(
                        batch, klines, concepts,
                        hot_codes=hot_codes, hot_keywords=hot_keywords,
                        news_text=news_text)
                    logger.info('首板涨停分析完成，生成候选 %d 支', len(results))

                    # 把 candidates / flow / changes 上的全部字段补齐到 result
                    from stock_filter import _fmt_main_net
                    cand_map = {s['code']: s for s in batch}
                    for r in results:
                        code = r['code']
                        c = cand_map.get(code, {})
                        # 现价 / 昨收 / 涨幅
                        r['current'] = f"{c.get('current', 0):.2f}"
                        r['yesterday_close'] = f"{c.get('yesterday_close', 0):.2f}"
                        rtc = c.get('change_pct', 0)
                        r['change_pct'] = f"{rtc:+.2f}%"
                        # 竞价涨幅 / 实时涨幅
                        ac = c.get('auction_change', 0)
                        r['auction_change'] = f"{ac:+.2f}%" if ac else '-'
                        r['real_time_change'] = f"{rtc:+.2f}%"
                        # 换手率 / 成交额 / 量比
                        r['turnover_rate'] = f"{c.get('turnover_rate', 0):.2f}%"
                        amt = c.get('amount', 0)
                        r['amount'] = f"{amt / 100000000:.2f}亿" if amt else '-'
                        vr = c.get('volume_ratio', 0)
                        r['volume_ratio'] = f"{vr:.2f}" if vr else '-'
                        # 市值 / 市盈
                        mc = c.get('market_cap', 0)
                        r['market_cap'] = f"{mc:.1f}亿" if mc else '-'
                        pe = c.get('pe', 0)
                        r['pe'] = f"{pe:.1f}" if pe > 0 else '-'
                        # 主力净额
                        flow = flow_data.get(code) or {}
                        mn = flow.get('main', 0) if flow else 0
                        r['main_net'] = _fmt_main_net(mn) if flow and flow.get('data_available') else '-'
                        # 5/10/20 日涨幅
                        chg = changes.get(code) or {}
                        r['change_5d'] = f"{chg.get('change_5d', 0):+.2f}%" if chg else '-'
                        r['change_10d'] = f"{chg.get('change_10d', 0):+.2f}%" if chg else '-'
                        r['change_20d'] = f"{chg.get('change_20d', 0):+.2f}%" if chg else '-'

                    # 获取板块概念（表格展示用）
                    self.progress.emit("正在获取板块概念...")
                    for r in results:
                        con = concepts.get(r['code'])
                        if con is None:
                            try:
                                con = await fetcher.fetch_stock_concepts(r['code'])
                            except Exception:
                                con = []
                        r['sector_concept'] = '、'.join(con[:3]) if con else '-'

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

                    results.sort(key=lambda x: x.get('score', 0), reverse=True)
                    return results[:10]

            results = loop.run_until_complete(do_analysis())
            loop.close()
            logger.info('首板推荐分析完成，推荐 %d 支：%s',
                        len(results),
                        [(r.get('code'), r.get('name'), round(r.get('score', 0), 2)) for r in results])
            self.result_ready.emit(results)
        except Exception as e:
            logger.exception('首板推荐分析失败: %s', e)
            self.result_ready.emit([])
            self.progress.emit(f"分析出错: {str(e)}")


class ClosingTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("首板推荐")
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

        self.status = QLabel('点击"执行分析"（首板涨停 | 主板 | 市值>100亿 | 股价>5元 | 排除ST/创业板/科创板）')
        self.status.setStyleSheet("color:#AAAAAA; font-size:13px; padding:5px;")
        layout.addWidget(self.status)

        self.table = StockTableWidget(headers=HEADERS)
        layout.addWidget(self.table, 1)

    def _check_time(self):
        """全天可执行，不再限制 15:00 后。早盘开盘后即可手动执行。"""
        return True

    def run_analysis(self):
        if not self._check_time():
            return
        self.run_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("正在分析首板涨停候选...")
        retire_thread(self)
        self.thread = ClosingAnalysisThread()
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
            self.status.setText(f"首板分析完成，推荐 {len(results)} 支首板涨停股票")
            self.table.set_data(results, HEADER_MAP)
        else:
            self.status.setText(
                "当前无符合条件的首板涨停股（条件：主板 | 市值>100亿 | 股价>5元 | "
                "今日涨停且前20日无涨停）。开盘后可随时重试。")
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
