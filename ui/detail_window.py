"""
股票详情窗口 - 分时图/日线图/资金/公告/财务/机构调研
数据源：腾讯 + 东方财富
"""
import asyncio, logging
from PyQt6 import sip
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
                              QStackedWidget, QTextEdit, QProgressBar, QGroupBox, QGridLayout,
                              QFrame, QApplication, QDialog, QScrollArea, QSizePolicy)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRectF, QPointF, QUrl
from PyQt6.QtGui import QFont, QColor, QBrush, QPainter, QPen, QPainterPath, QFontMetrics, QDesktopServices, QPolygonF
import numpy as np
from data_fetcher import MultiSourceFetcher, new_async_loop

_logger = logging.getLogger('detail_window')

RED = QColor("#E74C3C"); GREEN = QColor("#2ECC71"); WHITE = QColor("#FFFFFF")
DARK = QColor("#1A1A2E"); CARD = QColor("#16213E"); GOLD = QColor("#F39C12")
HEADER_C = QColor("#0F3460"); GRID = QColor("#2A2A4A"); GRAY = QColor("#AAAAAA")


class DetailDataThread(QThread):
    data_ready = pyqtSignal(dict)
    progress = pyqtSignal(str)

    def __init__(self, code, name, data_type="all", load_id=0):
        super().__init__()
        self.code = code; self.name = name; self.data_type = data_type; self.load_id = load_id

    def run(self):
        try:
            loop = new_async_loop(); asyncio.set_event_loop(loop)
            async def fetch():
                result = {'code': self.code, 'name': self.name, 'data_type': self.data_type, '_load_id': self.load_id}
                async with MultiSourceFetcher() as fetcher:
                    if self.data_type in ('all', 'intraday'):
                        self.progress.emit("正在获取分时数据...")
                        try:
                            intraday = await fetcher.fetch_intraday_data(self.code)
                            result['intraday'] = intraday
                        except Exception as e:
                            _logger.warning(f"intraday fetch error for {self.code}: {e}")
                            result['intraday'] = None
                        # 成交明细独立获取，不依赖分时数据是否成功
                        self.progress.emit("正在获取成交明细...")
                        try:
                            result['trade_details'] = await fetcher.fetch_trade_details(self.code)
                        except Exception as e:
                            _logger.warning(f"trade details error for {self.code}: {e}")
                            result['trade_details'] = []

                    if self.data_type in ('all', 'daily'):
                        self.progress.emit("正在获取日K线数据...")
                        try:
                            result['daily'] = await fetcher.fetch_kline_data(self.code, count=250)
                        except Exception as e:
                            _logger.warning(f"daily kline error for {self.code}: {e}")
                            result['daily'] = None

                    # 资金流向始终获取（主力净流入在详情页顶部显示）
                    self.progress.emit("正在获取资金流向...")
                    try: result['flow'] = await fetcher.fetch_money_flow(self.code)
                    except Exception as e:
                        _logger.warning(f"money flow error for {self.code}: {e}")
                        result['flow'] = None

                    if self.data_type in ('all', 'flow'):
                        self.progress.emit("正在获取大宗交易...")
                        try: result['block_trades'] = await fetcher.fetch_block_trades(self.code)
                        except Exception as e:
                            _logger.warning(f"block trades error for {self.code}: {e}")
                            result['block_trades'] = None

                    if self.data_type in ('all', 'notice'):
                        self.progress.emit("正在获取新闻资讯...")
                        try: result['news'] = await fetcher.fetch_stock_news(self.code)
                        except Exception as e:
                            _logger.warning(f"news error for {self.code}: {e}")
                            result['news'] = None

                        self.progress.emit("正在获取公司公告...")
                        try: result['notices'] = await fetcher.fetch_company_notices(self.code)
                        except Exception as e:
                            _logger.warning(f"notices error for {self.code}: {e}")
                            result['notices'] = None

                    if self.data_type in ('all', 'financial'):
                        self.progress.emit("正在获取财务数据...")
                        try: result['financial'] = await fetcher.fetch_financial_data(self.code)
                        except Exception as e:
                            _logger.warning(f"financial error for {self.code}: {e}")
                            result['financial'] = None

                    if self.data_type in ('all', 'research'):
                        self.progress.emit("正在获取机构调研...")
                        try: result['research'] = await fetcher.fetch_institution_research(self.code)
                        except Exception as e:
                            _logger.warning(f"research error for {self.code}: {e}")
                            result['research'] = None
                return result
            r = loop.run_until_complete(fetch()); loop.close()
            self.data_ready.emit(r)
        except Exception as e:
            _logger.error(f"DetailDataThread fatal error for {self.code}: {e}")
            self.data_ready.emit({'code': self.code, 'name': self.name, 'error': str(e)})


def _diff_volumes(data):
    """将累计成交量转为每分钟/每点成交量，只改 volume 字段，不修改原数据"""
    result = []
    prev = 0
    for d in data:
        cv = d.get('volume', 0)
        dv = max(0, cv - prev)
        result.append({'time': d.get('time', ''), 'price': d.get('price', 0), 'volume': dv})
        prev = cv
    return result


class IntradayChartWidget(QWidget):
    """分时图 — 连续曲线：集合竞价(9:15-9:25)+早盘(9:30-11:30)+午盘(13:00-15:00)"""
    def __init__(self, parent=None):
        super().__init__(parent); self._data = None; self.setMinimumHeight(240)
        self._hover_idx = None; self._hover_seg = None
        self.setMouseTracking(True)

    def update_data(self, data):
        if sip.isdeleted(self): return
        self._data = data; self.update()

    def _split_data(self):
        ad = self._data.get('auction_data', []) if self._data else []
        md = self._data.get('minute_data', []) if self._data else []
        # 兜底：若 auction_data 为空，从 minute_data 提取 0915-0925 竞价时段
        if not ad:
            ad = [d for d in md if '0915' <= d.get('time', '') <= '0925']
        morning = [d for d in md if '0930' <= d.get('time', '') <= '1130']
        afternoon = [d for d in md if d.get('time', '') >= '1300']
        return ad, morning, afternoon

    def paintEvent(self, event):
        try:
            if sip.isdeleted(self) or not self.isVisible():
                return
        except RuntimeError:
            return
        _data = self._data
        if not _data:
            return
        ad, morning, afternoon = self._split_data()
        all_data = ad + morning + afternoon
        if not all_data:
            return
        yc = _data.get('yesterday_close', 0)
        w, h = self.width(), self.height()
        m = {'l': 65, 'r': 65, 't': 20, 'b': 22}
        pw = w - m['l'] - m['r']; ph = h - m['t'] - m['b']

        # ── 价格区间：对称围绕昨收 ──
        all_prices = [d['price'] for d in all_data if d.get('price', 0) > 0]
        if yc and yc > 0: all_prices.append(yc)
        if not all_prices:
            return
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), DARK)
        max_p, min_p = max(all_prices), min(all_prices)
        max_dev = max(abs(max_p - yc), abs(min_p - yc)) if yc else (max_p - min_p) / 2
        if max_dev == 0: max_dev = yc * 0.02 or 0.01
        max_p = yc + max_dev * 1.1; min_p = yc - max_dev * 1.1
        pr = max_p - min_p
        max_pct = max_dev / yc * 100 if yc else 0

        def _to_y(price):
            return m['t'] + ph * (1 - (price - min_p) / pr)

        # ── X轴映射：竞价~20% | 早盘~40% | 午盘~40% ──
        ad_c, mo_c, af_c = len(ad), len(morning), len(afternoon)
        ar = 0.20 if ad_c > 0 else 0
        mr = 0.40 if mo_c > 0 else 0
        afr = 0.40 if af_c > 0 else 0
        tr = ar + mr + afr
        if tr > 0: ar, mr, afr = ar / tr, mr / tr, afr / tr
        seg_ends = [m['l'] + pw * ar, m['l'] + pw * (ar + mr), m['l'] + pw]

        def _to_x(seg_idx, idx, seg_len):
            if seg_len <= 0: return seg_ends[seg_idx - 1] if seg_idx > 0 else m['l']
            if seg_idx == 0:
                return m['l'] + pw * ar * (idx / seg_len)
            elif seg_idx == 1:
                return seg_ends[0] + pw * mr * (idx / seg_len)
            else:
                return seg_ends[1] + pw * afr * (idx / seg_len)

        cp = _data.get('change_pct', 0)
        curve_color = QColor("#4A90D9")  # 蓝色曲线
        fill_color = QColor(74, 144, 217, 12)  # 浅蓝填充

        # ── 网格 ──
        p.setPen(QPen(QColor("#E8E8E8"), 0.5, Qt.PenStyle.DotLine))
        for i in range(7):
            y = m['t'] + ph * i / 6
            p.drawLine(int(m['l']), int(y), int(w - m['r']), int(y))
        for x_pos in seg_ends[:2]:
            p.drawLine(int(x_pos), int(m['t']), int(x_pos), int(h - m['b']))

        # ── 昨收线（0%虚线）──
        if yc and yc > 0 and min_p <= yc <= max_p:
            yy = _to_y(yc)
            p.setPen(QPen(QColor("#999999"), 0.8, Qt.PenStyle.DashLine))
            p.drawLine(int(m['l']), int(yy), int(w - m['r']), int(yy))

        # ── 连续价格曲线 ──
        path = QPainterPath(); first = True
        for seg_idx, seg_data in enumerate([ad, morning, afternoon]):
            for i, d in enumerate(seg_data):
                x = _to_x(seg_idx, i, len(seg_data))
                y = _to_y(d.get('price', yc))
                if first: path.moveTo(x, y); first = False
                else: path.lineTo(x, y)
        # 填充
        if not path.isEmpty():
            fp = QPainterPath(path)
            last_pt = path.currentPosition()
            fp.lineTo(last_pt.x(), m['t'] + ph)
            # 找到第一个点的x
            first_x = m['l']
            if ad_c > 0: first_x = _to_x(0, 0, ad_c)
            fp.lineTo(first_x, m['t'] + ph)
            fp.closeSubpath()
            p.fillPath(fp, fill_color)
        p.setPen(QPen(curve_color, 1.2)); p.drawPath(path)

        # ── 均价线（黄色，从早盘开始）──
        trade_data = morning + afternoon
        if trade_data:
            diffed = _diff_volumes(trade_data)
            avg_path = QPainterPath(); first = True
            tv, ta = 0, 0
            for seg_idx, seg_data in enumerate([morning, afternoon]):
                for i, d in enumerate(seg_data):
                    # find corresponding diffed entry
                    idx_in_trade = sum(len([morning, afternoon][j]) for j in range(seg_idx)) + i
                    if idx_in_trade < len(diffed):
                        dd = diffed[idx_in_trade]
                        vol, price = dd['volume'], dd.get('price', yc)
                        tv += vol; ta += vol * price
                        av = ta / tv if tv > 0 else price
                        x = _to_x(seg_idx + 1, i, len(seg_data))
                        y = _to_y(av)
                        if first: avg_path.moveTo(x, y); first = False
                        else: avg_path.lineTo(x, y)
            if not avg_path.isEmpty():
                p.setPen(QPen(QColor("#E67E22"), 1.0)); p.drawPath(avg_path)

        # ── Y轴左：价格 ──
        p.setFont(QFont("Microsoft YaHei", 8))
        for i in range(7):
            prc = max_p - pr * i / 6; y = m['t'] + ph * i / 6
            clr = RED if yc and prc > yc else (GREEN if yc and prc < yc else QColor("#333333"))
            p.setPen(clr); p.drawText(2, int(y + 4), f"{prc:.2f}")

        # ── Y轴右：涨跌幅 ──
        p.setFont(QFont("Microsoft YaHei", 8))
        for i in range(7):
            pct = max_pct - max_pct * 2 * i / 6
            pct_str = f"{pct:+.2f}%"
            clr = RED if pct > 0 else (GREEN if pct < 0 else QColor("#333333"))
            p.setPen(clr)
            fm = QFontMetrics(p.font()); tw = fm.horizontalAdvance(pct_str)
            p.drawText(int(w - m['r'] + 4), int(m['t'] + ph * i / 6 + 4), pct_str)

        # ── 底部时间标签 ──
        p.setFont(QFont("Microsoft YaHei", 8)); p.setPen(QColor("#666666"))
        labels = [
            (m['l'], '09:15~25'),
            (seg_ends[0], '09:30'),
            (seg_ends[1], '11:30/13:00'),
            (m['l'] + pw, '15:30'),
        ]
        for x_pos, text in labels:
            fm = QFontMetrics(p.font()); tw = fm.horizontalAdvance(text)
            p.drawText(int(x_pos - tw / 2), int(h - 4), text)

        # ── 鼠标悬浮十字线 + 数据提示 ──
        if self._hover_idx is not None and self._hover_seg is not None:
            seg_data = [ad, morning, afternoon][self._hover_seg]
            if 0 <= self._hover_idx < len(seg_data):
                d = seg_data[self._hover_idx]
                hx = _to_x(self._hover_seg, self._hover_idx, len(seg_data))
                hp = d.get('price', yc)
                hy = _to_y(hp)
                # 十字虚线
                p.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.PenStyle.DashLine))
                p.drawLine(int(hx), int(m['t']), int(hx), int(h - m['b']))
                p.drawLine(int(m['l']), int(hy), int(w - m['r']), int(hy))
                # 悬浮圆点
                p.setPen(QPen(QColor(255, 255, 255), 1.5))
                p.setBrush(QColor(74, 144, 217))
                p.drawEllipse(QRectF(hx - 3, hy - 3, 6, 6))
                # 数据提示
                time_str = d.get('time', '')
                if len(time_str) == 4:
                    time_display = f"{time_str[:2]}:{time_str[2:]}"
                else:
                    time_display = time_str
                chg_pct = (hp - yc) / yc * 100 if yc and yc > 0 else 0
                lines = [
                    f"时间: {time_display}",
                    f"价格: {hp:.2f}",
                    f"涨跌幅: {chg_pct:+.2f}%",
                ]
                p.setFont(QFont("Microsoft YaHei", 9))
                fm = p.fontMetrics()
                tw = max(fm.boundingRect(line).width() for line in lines) + 16
                th = fm.height() * len(lines) + 10
                tx = int(hx - tw - 15) if hx > w / 2 else int(hx + 15)
                ty = int(m['t'] + 5)
                if ty + th > h - m['b']:
                    ty = max(0, int(h - m['b'] - th))
                bg_box = QColor(20, 30, 50, 215)
                p.setPen(QPen(QColor(100, 140, 200), 1))
                p.setBrush(bg_box)
                p.drawRoundedRect(QRectF(tx, ty, tw, th), 4, 4)
                p.setPen(QColor(220, 220, 220))
                for j, line in enumerate(lines):
                    ly = ty + fm.height() + j * fm.height() + 2
                    p.drawText(int(tx + 8), int(ly), line)
        p.end()

    def mouseMoveEvent(self, event):
        if not self._data:
            return
        ad, morning, afternoon = self._split_data()
        all_data = ad + morning + afternoon
        if not all_data:
            return
        x = event.position().x()
        w, hw = self.width(), self.height()
        m_m = {'l': 65, 'r': 65, 't': 20, 'b': 22}
        pw = w - m_m['l'] - m_m['r']

        ad_c, mo_c, af_c = len(ad), len(morning), len(afternoon)
        ar = 0.20 if ad_c > 0 else 0
        mr = 0.40 if mo_c > 0 else 0
        afr = 0.40 if af_c > 0 else 0
        tr = ar + mr + afr
        if tr > 0:
            ar, mr, afr = ar / tr, mr / tr, afr / tr
        seg_ends = [m_m['l'] + pw * ar, m_m['l'] + pw * (ar + mr), m_m['l'] + pw]

        if x < m_m['l'] or x > w - m_m['r']:
            self._hover_idx = None; self._hover_seg = None
        elif x < seg_ends[0] and ad_c > 0:
            self._hover_seg = 0
            self._hover_idx = max(0, min(ad_c - 1, int((x - m_m['l']) / (pw * ar) * ad_c)))
        elif x < seg_ends[1] and mo_c > 0:
            self._hover_seg = 1
            self._hover_idx = max(0, min(mo_c - 1, int((x - seg_ends[0]) / (pw * mr) * mo_c)))
        elif af_c > 0 and x < seg_ends[2]:
            self._hover_seg = 2
            self._hover_idx = max(0, min(af_c - 1, int((x - seg_ends[1]) / (pw * afr) * af_c)))
        else:
            self._hover_idx = None; self._hover_seg = None
        self.update()

    def leaveEvent(self, event):
        if self._hover_idx is not None:
            self._hover_idx = None; self._hover_seg = None
            self.update()


class IntradayVolumeWidget(QWidget):
    """分时成交量 — 竞价双层(未匹配/已匹配) + 早盘 + 午盘"""
    def __init__(self, parent=None):
        super().__init__(parent); self._data = None; self.setMinimumHeight(80)

    def update_data(self, data):
        if sip.isdeleted(self): return
        self._data = data; self.update()

    def _split_data(self):
        ad = self._data.get('auction_data', []) if self._data else []
        md = self._data.get('minute_data', []) if self._data else []
        if not ad:
            ad = [d for d in md if '0915' <= d.get('time', '') <= '0925']
        morning = [d for d in md if '0930' <= d.get('time', '') <= '1130']
        afternoon = [d for d in md if d.get('time', '') >= '1300']
        return ad, morning, afternoon

    @staticmethod
    def _auction_buy_sell(ad_data, open_price):
        """从累计量竞价数据推导每笔买卖方向拆分。
        返回 [{time, volume, buy_vol, sell_vol, is_buy}, ...]，volume=单笔差分成交量"""
        result = []
        if not ad_data or open_price <= 0:
            return result
        # 累计量→单笔量
        prev_cum = 0
        raw_ticks = []
        for d in ad_data:
            vol = d.get('volume', 0)
            tick_vol = max(0, vol - prev_cum)
            prev_cum = vol
            raw_ticks.append({'time': d.get('time', ''), 'price': float(d.get('price', 0)), 'volume': tick_vol})
        if not raw_ticks:
            return result
        # 按相对于开盘价的方向拆分买卖量
        for d in raw_ticks:
            price, vol = d['price'], d['volume']
            if vol <= 0:
                result.append({'time': d['time'], 'volume': 0, 'buy_vol': 0, 'sell_vol': 0, 'is_buy': True})
                continue
            # 偏差比率：价高于开盘则买盘主导，反则卖盘主导
            dev = (price - open_price) / max(open_price, 0.01)
            ratio = max(0.15, min(0.85, 0.5 + 0.45 * abs(dev) * 10))
            if price >= open_price:
                buy_vol = vol * ratio
                sell_vol = vol * (1 - ratio)
                is_buy = True
            else:
                sell_vol = vol * ratio
                buy_vol = vol * (1 - ratio)
                is_buy = False
            result.append({
                'time': d['time'], 'volume': vol,
                'buy_vol': buy_vol, 'sell_vol': sell_vol, 'is_buy': is_buy,
            })
        return result

    def paintEvent(self, event):
        try:
            if sip.isdeleted(self) or not self.isVisible():
                return
        except RuntimeError:
            return
        # 提前拷贝数据引用，防止绘制中途 _data 被置 None
        _data = self._data
        if not _data:
            return
        ad, morning, afternoon = self._split_data()
        all_raw = ad + morning + afternoon
        if not all_raw:
            return
        diffed = _diff_volumes(all_raw)
        all_vols = [d['volume'] for d in diffed]
        if not all_vols:
            return
        w, h = self.width(), self.height()
        m = {'l': 65, 'r': 65, 't': 26, 'b': 20}
        pw = w - m['l'] - m['r']; ph = h - m['t'] - m['b']
        mv = max(all_vols) if all_vols else 1
        if mv == 0:
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.fillRect(self.rect(), DARK)

            # ── X轴映射（与价格曲线对齐：竞价20% / 早盘40% / 午盘40%）──
            ad_c, mo_c, af_c = len(ad), len(morning), len(afternoon)
            ar = 0.20 if ad_c > 0 else 0; mr = 0.40 if mo_c > 0 else 0; afr = 0.40 if af_c > 0 else 0
            tr = ar + mr + afr
            if tr > 0: ar, mr, afr = ar / tr, mr / tr, afr / tr
            seg_ends = [m['l'] + pw * ar, m['l'] + pw * (ar + mr), m['l'] + pw]

            def _to_x(seg_idx, idx, seg_len):
                if seg_len <= 0: return seg_ends[seg_idx - 1] if seg_idx > 0 else m['l']
                if seg_idx == 0: return m['l'] + pw * ar * (idx / seg_len)
                elif seg_idx == 1: return seg_ends[0] + pw * mr * (idx / seg_len)
                else: return seg_ends[1] + pw * afr * (idx / seg_len)

            # ── 统计信息 ──
            total_vol = sum(all_vols)
            total_amt = sum(d.get('price', 0) * d['volume'] * 100 for d in diffed)
            latest_vol = diffed[-1]['volume'] if diffed else 0
            p.setFont(QFont("Microsoft YaHei", 8)); p.setPen(QColor("#666666"))
            info = f"成交量   量:{latest_vol}  额:{total_amt/10000:.2f}万"
            p.drawText(int(m['l']), int(m['t'] - 6), info)

            # ── 网格 ──
            p.setPen(QPen(QColor("#E8E8E8"), 0.5, Qt.PenStyle.DotLine))
            for i in range(3):
                y = m['t'] + ph * i / 2
                p.drawLine(int(m['l']), int(y), int(w - m['r']), int(y))
            for x_pos in seg_ends[:2]:
                p.drawLine(int(x_pos), int(m['t']), int(x_pos), int(h - m['b']))

            # ── 竞价段：双层买卖柱形图 ──
            open_price = _data.get('open', 0) or _data.get('yesterday_close', 0)
            if open_price <= 0 and ad:
                open_price = float(ad[0].get('price', 0))
            auction_ticks = self._auction_buy_sell(ad, open_price)
            if auction_ticks:
                # 竞价区域的Y范围：整体ph，分为上下两半
                auc_mid = m['t'] + ph * 0.5    # 零轴中线（未匹配与已匹配交界）
                auc_bot = m['t'] + ph          # 下半(已匹配)底部
                half_ph = ph * 0.5             # 半高

                # 上半最大高度：取最买卖量中最大值用于缩放
                top_max = max(max(t['buy_vol'], t['sell_vol']) for t in auction_ticks) or 1
                # 下半：已匹配量柱也以整体mv缩放
                bot_max = mv

                for i, t in enumerate(auction_ticks):
                    x = _to_x(0, i, len(ad))
                    bar_w = max(1.5, (pw * ar) / max(len(ad), 1) * 0.55)

                    if t['volume'] <= 0:
                        continue

                    # ── 上半：未匹配买卖挂单（零轴发散） ──
                    buy_h = max(1, (t['buy_vol'] / top_max) * half_ph * 0.85)
                    sell_h = max(1, (t['sell_vol'] / top_max) * half_ph * 0.85)
                    # 红柱(买单)向上，从零轴往上画
                    p.fillRect(QRectF(x - bar_w / 2, auc_mid - buy_h, bar_w, buy_h), RED)
                    # 绿柱(卖单)向下，从零轴往下画
                    p.fillRect(QRectF(x - bar_w / 2, auc_mid, bar_w, sell_h), GREEN)

                    # ── 下半：已匹配成交（标准量柱） ──
                    match_h = max(1, (t['volume'] / bot_max) * half_ph)
                    match_y = auc_bot - match_h
                    clr = RED if t['is_buy'] else GREEN
                    p.fillRect(QRectF(x - bar_w / 2, match_y, bar_w, match_h), clr)

                # ── 竞价分区标签 ──
                if ad_c > 0:
                    p.setFont(QFont("Microsoft YaHei", 7)); p.setPen(QColor("#999999"))
                    # 上半标签(挂单)放在竞价区域的左上方
                    p.save(); p.translate(5, auc_mid - half_ph * 0.5)
                    p.rotate(-90); p.drawText(-8, 0, "未匹配"); p.restore()
                    # 下半标签(成交)放在竞价区域的左下方
                    p.save(); p.translate(5, auc_mid + half_ph * 0.5)
                    p.rotate(-90); p.drawText(-8, 0, "已匹配"); p.restore()
                    # 零轴实线加固
                    p.setPen(QPen(QColor("#555555"), 0.8, Qt.PenStyle.SolidLine))
                    p.drawLine(int(m['l']), int(auc_mid), int(seg_ends[0]), int(auc_mid))

            # ── 早盘+午盘 量柱 ──
            for seg_idx, seg_data in enumerate([morning, afternoon]):
                seg_diff = _diff_volumes(seg_data)
                if not seg_diff: continue
                for i, d in enumerate(seg_diff):
                    vol = d['volume']
                    x = _to_x(seg_idx + 1, i, len(seg_data))
                    bh = max(1, (vol / mv) * ph)
                    y = m['t'] + ph - bh
                    bw = max(1, pw / max(len(diffed), 240) * 0.6)
                    raw_seg = [morning, afternoon][seg_idx]
                    if i > 0 and raw_seg[i].get('price', 0) >= raw_seg[i - 1].get('price', 0):
                        clr = RED
                    else:
                        clr = GREEN
                    p.fillRect(QRectF(x - bw / 2, y, bw, bh), clr)

            # ── Y轴标签 ──
            p.setPen(QColor("#999999")); p.setFont(QFont("Microsoft YaHei", 7))
            p.drawText(3, int(m['t'] + 5), "万")
            p.drawText(int(m['l'] + 2), int(m['t'] + 10), f"{mv / 10000:,.0f}")
            p.drawText(int(m['l'] + 2), int(m['t'] + ph + 10), "0")

            # ── 底部时间标签 ──
            p.setFont(QFont("Microsoft YaHei", 8)); p.setPen(QColor("#666666"))
            labels = [
                (m['l'], '09:15~25'), (seg_ends[0], '09:30'),
                (seg_ends[1], '11:30/13:00'), (m['l'] + pw, '15:30'),
            ]
            for x_pos, text in labels:
                fm = QFontMetrics(p.font()); tw = fm.horizontalAdvance(text)
                p.drawText(int(x_pos - tw / 2), int(h - 4), text)
        finally:
            p.end()


class OrderBookRatioBar(QWidget):
    """买卖力量对比横条"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.buy_ratio = 0.5
        self.setFixedHeight(6)
        self.setStyleSheet("background-color: transparent;")

    def set_ratio(self, buy_ratio):
        self.buy_ratio = max(0.0, min(1.0, buy_ratio))
        self.update()

    def paintEvent(self, event):
        try:
            p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            bw = int(w * self.buy_ratio)
            p.fillRect(0, 0, bw, h, QColor("#E74C3C"))
            p.fillRect(bw, 0, w - bw, h, QColor("#3498DB"))
            p.end()
        except RuntimeError:
            pass


class OrderBookPanel(QWidget):
    """五档买卖盘 — 卖5~卖1 | 买卖比条 | 买1~买5"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._order_book = None
        self._init_ui()

    def _init_ui(self):
        l = QVBoxLayout(self); l.setContentsMargins(4, 0, 4, 0); l.setSpacing(2)
        t = QLabel("五档买卖盘"); t.setStyleSheet("font-size:12px;font-weight:bold;color:#F39C12;padding:2px;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter); l.addWidget(t)

        # 卖盘区域
        self.sell_container = QWidget()
        self.sell_layout = QVBoxLayout(self.sell_container)
        self.sell_layout.setContentsMargins(0, 0, 0, 0); self.sell_layout.setSpacing(1)
        self.sell_rows = []
        for i in range(5, 0, -1):
            row = self._create_row(f"卖{i}", is_sell=True)
            self.sell_layout.addWidget(row)
            self.sell_rows.append(row)
        l.addWidget(self.sell_container)

        # 买卖比横条
        self.ratio_bar = OrderBookRatioBar()
        l.addWidget(self.ratio_bar)

        # 买盘区域
        self.buy_container = QWidget()
        self.buy_layout = QVBoxLayout(self.buy_container)
        self.buy_layout.setContentsMargins(0, 0, 0, 0); self.buy_layout.setSpacing(1)
        self.buy_rows = []
        for i in range(1, 6):
            row = self._create_row(f"买{i}", is_sell=False)
            self.buy_layout.addWidget(row)
            self.buy_rows.append(row)
        l.addWidget(self.buy_container)
        l.addStretch()

    def _create_row(self, label_text, is_sell):
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(4)
        lbl = QLabel(label_text)
        price_color = '#27AE60' if is_sell else '#E74C3C'  # 卖绿买红
        lbl.setStyleSheet(f"color:{price_color}; font-size:11px; font-weight:bold;")
        lbl.setFixedWidth(32); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price = QLabel("--")
        price.setStyleSheet(f"color:{price_color}; font-size:11px; font-weight:bold;")
        price.setAlignment(Qt.AlignmentFlag.AlignCenter)
        amt = QLabel("--")
        amt.setStyleSheet(f"color:{price_color}; font-size:11px; font-weight:bold; padding:1px 4px; border-radius:2px;")
        amt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(lbl); h.addWidget(price, 1); h.addWidget(amt, 1)
        row.label = lbl; row.price = price; row.amt = amt; row.is_sell = is_sell
        return row

    def update_data(self, order_book):
        if sip.isdeleted(self): return
        self._order_book = order_book
        SELL_COLOR, BUY_COLOR = '#27AE60', '#E74C3C'
        BASE_STYLE = "font-size:11px; font-weight:bold; padding:1px 4px; border-radius:2px;"
        if not order_book:
            for row in self.sell_rows + self.buy_rows:
                row.price.setText("--"); row.amt.setText("--")
                c = SELL_COLOR if row.is_sell else BUY_COLOR
                row.amt.setStyleSheet(f"color:{c}; {BASE_STYLE}")
            self.ratio_bar.set_ratio(0.5)
            return
        bl = order_book.get('buy_levels', []); sl = order_book.get('sell_levels', [])
        # 更新卖盘（卖5到卖1）
        for i, row in enumerate(self.sell_rows):
            idx = 4 - i  # 卖5,卖4,卖3,卖2,卖1
            if idx < len(sl):
                row.price.setText(f"{sl[idx]['price']:.2f}")
                amt = sl[idx]['price'] * sl[idx]['volume'] * 100 / 10000
                row.amt.setText(f"{amt:.2f}万" if amt >= 1 else f"{int(sl[idx]['volume'])}")
                row.amt.setStyleSheet(f"color:{SELL_COLOR}; {BASE_STYLE}")
            else:
                row.price.setText("--"); row.amt.setText("--")
                row.amt.setStyleSheet(f"color:{SELL_COLOR}; {BASE_STYLE}")
        # 更新买盘（买1到买5）
        for i, row in enumerate(self.buy_rows):
            if i < len(bl):
                row.price.setText(f"{bl[i]['price']:.2f}")
                amt = bl[i]['price'] * bl[i]['volume'] * 100 / 10000
                row.amt.setText(f"{amt:.2f}万" if amt >= 1 else f"{int(bl[i]['volume'])}")
                row.amt.setStyleSheet(f"color:{BUY_COLOR}; {BASE_STYLE}")
            else:
                row.price.setText("--"); row.amt.setText("--")
                row.amt.setStyleSheet(f"color:{BUY_COLOR}; {BASE_STYLE}")
        # 买卖比
        total_buy = sum(l['volume'] for l in bl)
        total_sell = sum(l['volume'] for l in sl)
        total = total_buy + total_sell
        self.ratio_bar.set_ratio(total_buy / total if total > 0 else 0.5)


class TradeDetailDialog(QDialog):
    """成交明细子窗口：分页显示全部成交数据，每页 15 条
    注意：不设 widget parent，避免 TradeDetailPanel 被异步重建时连带销毁 Dialog 导致崩溃。
    """
    PAGE_SIZE = 15

    def __init__(self, trades, ref_window=None):
        super().__init__(None)
        self.setWindowTitle("成交明细")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._trades = trades
        self._page = 0                         # 当前页码（0-based）
        self._total_pages = max(1, (len(trades) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._init_ui()
        self._render_page()
        self._apply_size(ref_window)

    def _apply_size(self, ref_window):
        if ref_window is None or sip.isdeleted(ref_window):
            return
        s = ref_window.size()
        new_w = int(s.width() * 0.8)
        new_h = int(s.height() * 0.8)
        self.resize(new_w, new_h)
        c = ref_window.mapToGlobal(ref_window.rect().center())
        self.move(c.x() - new_w // 2, c.y() - new_h // 2)

    # ── 样式常量 ──
    _BTN_STYLE = f"""
        QPushButton {{
            background: #16213E; color: {GRAY.name()}; border: 1px solid #2A3A5C;
            border-radius: 3px; padding: 3px 10px; font-size: 10px;
        }}
        QPushButton:hover {{ background: #0F3460; color: white; }}
        QPushButton:disabled {{ background: #16213E; color: #555555; border-color: #1A1A2E; }}
    """
    _BTN_ACTIVE = f"""
        QPushButton {{
            background: {GOLD.name()}; color: {DARK.name()}; border: none;
            border-radius: 3px; padding: 5px 14px; font-size: 11px; font-weight: bold;
        }}
        QPushButton:hover {{ background: #F1C40F; }}
    """

    def _init_ui(self):
        pal = self.palette()
        pal.setColor(self.backgroundRole(), DARK)
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        root = QVBoxLayout()
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        self._title_label = QLabel()
        self._title_label.setStyleSheet("font-size:14px;font-weight:bold;color:#F39C12;padding:4px;background:transparent;")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._title_label)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(['时间', '价格', '成交量', '方向'])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setStyleSheet(f"""
            QTableWidget {{ background-color: {DARK.name()}; color: white; gridline-color: #2A3A5C;
                border: none; font-size: 11px; }}
            QTableWidget::item {{ padding: 2px 4px; border: none; }}
            QHeaderView::section {{ background-color: #16213E; color: {GRAY.name()}; padding: 3px;
                border: none; font-size: 10px; font-weight: bold; }}
        """)
        root.addWidget(self._table, 1)

        # ── 底栏：分页导航 + 关闭 ──
        bar = QHBoxLayout()
        bar.setSpacing(4)

        self._btn_first = QPushButton("首页")
        self._btn_first.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_first.setStyleSheet(self._BTN_STYLE)
        self._btn_first.clicked.connect(self._go_first)

        self._btn_prev = QPushButton("上一页")
        self._btn_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_prev.setStyleSheet(self._BTN_STYLE)
        self._btn_prev.clicked.connect(self._go_prev)

        self._page_label = QLabel()
        self._page_label.setStyleSheet("color:#AAAAAA;font-size:10px;padding:0 6px;background:transparent;")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._btn_next = QPushButton("下一页")
        self._btn_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_next.setStyleSheet(self._BTN_STYLE)
        self._btn_next.clicked.connect(self._go_next)

        self._btn_last = QPushButton("末页")
        self._btn_last.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_last.setStyleSheet(self._BTN_STYLE)
        self._btn_last.clicked.connect(self._go_last)

        bar.addStretch()
        bar.addWidget(self._btn_first)
        bar.addWidget(self._btn_prev)
        bar.addWidget(self._page_label)
        bar.addWidget(self._btn_next)
        bar.addWidget(self._btn_last)
        bar.addStretch()

        bar.addSpacing(20)

        btn_close = QPushButton("关闭")
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(self._BTN_ACTIVE)
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)

        root.addLayout(bar)
        self.setLayout(root)

    # ── 分页导航 ──

    def _go_first(self):
        self._page = 0
        self._render_page()

    def _go_prev(self):
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def _go_next(self):
        if self._page < self._total_pages - 1:
            self._page += 1
            self._render_page()

    def _go_last(self):
        self._page = self._total_pages - 1
        self._render_page()

    def _render_page(self):
        start = self._page * self.PAGE_SIZE
        end = min(start + self.PAGE_SIZE, len(self._trades))
        page_trades = self._trades[start:end]

        self._title_label.setText(f"成交明细（共 {len(self._trades)} 条）")
        self._page_label.setText(f"第 {self._page + 1}/{self._total_pages} 页")

        # 导航按钮状态
        self._btn_first.setEnabled(self._page > 0)
        self._btn_prev.setEnabled(self._page > 0)
        self._btn_next.setEnabled(self._page < self._total_pages - 1)
        self._btn_last.setEnabled(self._page < self._total_pages - 1)

        self._table.setRowCount(len(page_trades))
        for i, tr in enumerate(page_trades):
            ti = QTableWidgetItem(tr.get('time', ''))
            ti.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 0, ti)

            trade_type = tr.get('type', 'neutral')
            pi = QTableWidgetItem(f"{tr.get('price', 0):.2f}")
            pi.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if trade_type == 'buy':
                pi.setForeground(QBrush(RED))
            elif trade_type == 'sell':
                pi.setForeground(QBrush(GREEN))
            self._table.setItem(i, 1, pi)

            vi = QTableWidgetItem(TradeDetailPanel._format_volume(tr.get('volume', 0)))
            vi.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 2, vi)

            di = QTableWidgetItem(
                '买' if trade_type == 'buy' else ('卖' if trade_type == 'sell' else '-')
            )
            di.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if trade_type == 'buy':
                di.setForeground(QBrush(RED))
            elif trade_type == 'sell':
                di.setForeground(QBrush(GREEN))
            self._table.setItem(i, 3, di)


class TradeDetailPanel(QWidget):
    """成交明细面板：默认显示前20条，点击"查看更多"打开子窗口"""
    PAGE_SIZE = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_trades = []          # 全部成交数据（已倒序）
        self._init_ui()

    def _init_ui(self):
        l = QVBoxLayout(self); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(2)

        title = QLabel("成交明细")
        title.setStyleSheet("font-size:13px;font-weight:bold;color:#F39C12;padding:4px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l.addWidget(title)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['时间', '价格', '成交量', '方向'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setStyleSheet(f"""
            QTableWidget {{ background-color: {DARK.name()}; color: white; gridline-color: transparent;
                border: none; font-size: 10px; }}
            QTableWidget::item {{ padding: 1px 3px; border: none; }}
            QHeaderView::section {{ background-color: #16213E; color: {GRAY.name()}; padding: 2px;
                border: none; font-size: 9px; }}
        """)
        l.addWidget(self.table)

        # 查看更多 按钮
        self.btn_more = QPushButton("查看更多 ▾")
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_more.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {GOLD.name()}; border: 1px solid {GOLD.name()};
                border-radius: 3px; padding: 3px 8px; font-size: 10px;
            }}
            QPushButton:hover {{
                background: {GOLD.name()}; color: {DARK.name()};
            }}
        """)
        self.btn_more.clicked.connect(self._open_detail_dialog)
        self.btn_more.setVisible(False)
        l.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def _format_volume(vol):
        """成交量格式化：>=10000手显示万手，否则显示手"""
        if not vol or vol == 0:
            return '-'
        if vol >= 10000:
            return f"{vol / 10000:.2f}万"
        return f"{vol}手"

    def _open_detail_dialog(self):
        """点击查看更多：打开子窗口（独立顶层窗口，不依附面板）"""
        if sip.isdeleted(self.table) or not self._all_trades:
            return
        ref = self.window()  # DetailWindow 顶层窗口，用于定位大小
        if sip.isdeleted(ref):
            ref = None
        dialog = TradeDetailDialog(self._all_trades, ref)
        dialog.exec()

    def _render(self):
        """渲染表格内联视图（最多 PAGE_SIZE 条）"""
        if sip.isdeleted(self.table):
            return
        trades = self._all_trades
        if not trades:
            self.table.setRowCount(0)
            self.btn_more.setVisible(False)
            return

        show_count = min(self.PAGE_SIZE, len(trades))
        self.table.setRowCount(show_count)
        for i, tr in enumerate(trades[:show_count]):
            ti = QTableWidgetItem(tr.get('time', ''))
            ti.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 0, ti)

            trade_type = tr.get('type', 'neutral')
            pi = QTableWidgetItem(f"{tr.get('price', 0):.2f}")
            pi.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if trade_type == 'buy':
                pi.setForeground(QBrush(RED))
            elif trade_type == 'sell':
                pi.setForeground(QBrush(GREEN))
            self.table.setItem(i, 1, pi)

            vi = QTableWidgetItem(self._format_volume(tr.get('volume', 0)))
            vi.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, vi)

            di = QTableWidgetItem(
                '买' if trade_type == 'buy' else ('卖' if trade_type == 'sell' else '-')
            )
            di.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if trade_type == 'buy':
                di.setForeground(QBrush(RED))
            elif trade_type == 'sell':
                di.setForeground(QBrush(GREEN))
            self.table.setItem(i, 3, di)

        # 按钮状态：数据 > PAGE_SIZE 时显示
        total = len(trades)
        self.btn_more.setVisible(total > self.PAGE_SIZE)
        if total > self.PAGE_SIZE:
            self.btn_more.setText(f"查看更多 ▾ (共{total}条)")

    def update_data(self, trades):
        if sip.isdeleted(self): return
        """接收最新成交数据（已倒序），默认展示前20条"""
        self._all_trades = trades if trades else []
        self._render()


class IntradayFullWidget(QWidget):
    """分时图：左(上:分时曲线 | 下:成交量) | 右(五档盘口+成交明细)"""
    def __init__(self, parent=None):
        super().__init__(parent); self._init_ui()

    def _init_ui(self):
        root = QHBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(4)

        # ── 左列：图上量下 ──
        left = QWidget()
        left_l = QVBoxLayout(left); left_l.setContentsMargins(0, 0, 0, 0); left_l.setSpacing(2)

        self.chart = IntradayChartWidget()
        left_l.addWidget(self.chart, 4)

        self.vol_chart = IntradayVolumeWidget()
        left_l.addWidget(self.vol_chart, 1)

        root.addWidget(left, 5)

        # ── 右列：五档盘口 + 成交明细 ──
        right = QWidget()
        right_l = QVBoxLayout(right); right_l.setContentsMargins(2, 0, 0, 0); right_l.setSpacing(4)

        self.order_book = OrderBookPanel()
        right_l.addWidget(self.order_book, 3)

        self.trade_detail = TradeDetailPanel()
        right_l.addWidget(self.trade_detail, 2)

        root.addWidget(right, 2)

    def update_data(self, intraday, order_book=None, trades=None):
        if sip.isdeleted(self): return
        if not sip.isdeleted(self.chart): self.chart.update_data(intraday)
        if not sip.isdeleted(self.vol_chart): self.vol_chart.update_data(intraday)
        if order_book and not sip.isdeleted(self.order_book): self.order_book.update_data(order_book)
        if not sip.isdeleted(self.trade_detail): self.trade_detail.update_data(trades if trades else [])


class DailyChartPanel(QWidget):
    """日线图 - K线+MA+成交量+MACD"""
    MA_COLORS = {5: QColor("#FFD700"), 10: QColor("#FF6347"), 30: QColor("#00BFFF"),
                 60: QColor("#32CD32"), 120: QColor("#BA55D3")}

    def __init__(self, parent=None):
        super().__init__(parent); self._data = None
        self._start_idx = 0; self._visible = 120; self._dragging = False; self._dx = 0
        self._hover_idx = None; self._hover_x = None; self._hover_y = None
        self.setMinimumHeight(320); self.setMouseTracking(True)

    def update_data(self, data):
        if sip.isdeleted(self): return
        self._data = data
        if data: self._start_idx = max(0, len(data) - self._visible)
        self.update()

    @staticmethod
    def _ma(data, period):
        r = []; n = len(data)
        for i in range(n):
            if i < period - 1: r.append(float('nan'))
            else: r.append(np.mean(data[i - period + 1:i + 1]))
        return r

    @staticmethod
    def _ema(data, period):
        if not data: return []
        r = [data[0]]; m = 2 / (period + 1)
        for i in range(1, len(data)): r.append((data[i] - r[i - 1]) * m + r[i - 1])
        return r

    @staticmethod
    def _find_extrema(values, order=2, mode='high'):
        """找局部极值索引（左右各 order 根范围内为最大/最小值）"""
        res = []; n = len(values)
        if n < 2 * order + 1: return res
        for i in range(order, n - order):
            seg = values[i - order:i + order + 1]
            v = values[i]
            if mode == 'high':
                if v == max(seg) and v > values[i - 1] and v > values[i + 1]: res.append(i)
            else:
                if v == min(seg) and v < values[i - 1] and v < values[i + 1]: res.append(i)
        return res

    def _detect_divergences(self, closes, dif):
        """检测 MACD(DIF) 顶背离/底背离：价格创新高(低)但 DIF 走势相反
        closes: 可见窗口收盘价序列; dif: 与 closes 等长的 DIF 序列
        返回: [(i1, i2, kind), ...]，i1<i2 为背离两端索引，kind 为 'top'/'bottom'
        """
        res = []; n = len(closes)
        if n < 12: return res
        peaks = self._find_extrema(closes, 2, 'high')
        troughs = self._find_extrema(closes, 2, 'low')
        last_idx = {'top': -99, 'bottom': -99}
        # 顶背离：后一个高点价格更高，但 DIF 未创新高
        for a, b in zip(peaks, peaks[1:]):
            if np.isnan(dif[a]) or np.isnan(dif[b]): continue
            if closes[b] > closes[a] and dif[b] < dif[a]:
                if b - last_idx['top'] < 4: continue  # 避免连续震荡时标记过密
                last_idx['top'] = b
                res.append((a, b, 'top'))
        # 底背离：后一个低点价格更低，但 DIF 未创新低
        for a, b in zip(troughs, troughs[1:]):
            if np.isnan(dif[a]) or np.isnan(dif[b]): continue
            if closes[b] < closes[a] and dif[b] > dif[a]:
                if b - last_idx['bottom'] < 4: continue
                last_idx['bottom'] = b
                res.append((a, b, 'bottom'))
        return res

    def _draw_divergences(self, p, m, kw, mmx, mh, mt, dif, divs):
        """在 MACD 区域绘制背离连线、箭头与文字标注"""
        if not divs: return
        p.setFont(QFont("Microsoft YaHei", 7))
        fh = p.fontMetrics().height()  # 文字高度
        fw = p.fontMetrics().horizontalAdvance("底背离")
        for a, b, kind in divs:
            x1 = m['l'] + kw * (a + 0.5) / len(dif)
            x2 = m['l'] + kw * (b + 0.5) / len(dif)
            y1 = mt + mh * (1 - (dif[a] + mmx) / (2 * mmx))
            y2 = mt + mh * (1 - (dif[b] + mmx) / (2 * mmx))
            if kind == 'top':
                col = QColor("#FF5252"); down = True
                label = "顶背离"
            else:
                col = QColor("#4CAF50"); down = False
                label = "底背离"
            p.setPen(QPen(col, 1, Qt.PenStyle.DashLine))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))
            # 两端点小圆点
            p.setBrush(col); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x1, y1), 2.5, 2.5)
            p.drawEllipse(QPointF(x2, y2), 2.5, 2.5)
            # 第二个端点处画方向箭头（顶背离向下、底背离向上）
            s = 5
            if down:
                tri = QPolygonF([QPointF(x2 - s, y2 - s), QPointF(x2 + s, y2 - s), QPointF(x2, y2 + s)])
            else:
                tri = QPolygonF([QPointF(x2 - s, y2 + s), QPointF(x2 + s, y2 + s), QPointF(x2, y2 - s)])
            p.drawPolygon(tri)
            # 文字标注：顶背离在端点上方、底背离在端点下方（与箭头反向），
            # 用字体高度保证不遮挡箭头，并始终限制在 MACD 区域内
            p.setPen(col)
            if down:
                ty = max(mt + fh, y2 - fh - s - 4)   # 基线，文字位于端点上侧
            else:
                ty = min(mt + mh, y2 + s + fh + 4)   # 基线，文字位于端点下侧
            tx = int(x2) - fw // 2
            p.drawText(tx, int(ty), label)

    def _draw_price_divergences(self, p, m, kw, kh, mnp, pr, vk, divs):
        """在K线主图标注背离价格：端点圆点 + 价格标签
        顶背离取端点K线最高价、底背离取最低价，标签方向与MACD标记一致
        """
        if not divs or not vk: return
        n = len(vk)
        p.setFont(QFont("Microsoft YaHei", 8))
        fm = p.fontMetrics()
        th = fm.height() + 4
        for a, b, kind in divs:
            if not (0 <= b < n): continue
            bar = vk[b]
            if kind == 'top':
                col = QColor("#FF5252"); above = True
                price = bar.get('high') or bar.get('close')
            else:
                col = QColor("#4CAF50"); above = False
                price = bar.get('low') or bar.get('close')
            if not price: continue
            y = m['t'] + kh * (1 - (price - mnp) / pr)
            x = m['l'] + kw * (b + 0.5) / n
            # 端点标记
            p.setBrush(col); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), 3, 3)
            # 价格标签：顶背离在端点上方、底背离在下方，背景半透明
            txt = f"{price:.2f}"
            tw = fm.horizontalAdvance(txt) + 8
            if above: ly = y - th - 4
            else: ly = y + 4
            ly = max(m['t'], min(m['t'] + kh - th, ly))
            lx = int(x - tw / 2)
            lx = max(int(m['l']), min(int(m['l'] + kw - tw), lx))
            # 半透明深色背景（无边框），白色文字
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(15, 25, 45, 210))
            p.drawRoundedRect(QRectF(lx, ly, tw, th), 3, 3)
            p.setPen(WHITE)
            p.drawText(QRectF(lx, ly, tw, th), Qt.AlignmentFlag.AlignCenter, txt)

    def paintEvent(self, event):
        try:
            if sip.isdeleted(self) or not self.isVisible():
                return
        except RuntimeError:
            return
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            p.fillRect(self.rect(), DARK)
            if not self._data: p.setPen(GRAY); p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无数据"); return
            # 安全提取可见K线
            kls = self._data
            ei = min(max(0, self._start_idx + self._visible), len(kls))
            si = max(0, min(self._start_idx, len(kls) - 1))
            vk = kls[si:ei]
            if not vk: return
            closes = [k['close'] for k in vk]; highs = [k['high'] for k in vk]
            lows = [k['low'] for k in vk]; opens = [k['open'] for k in vk]
            vols = [k['volume'] for k in vk]
            fc = [k['close'] for k in kls]
            ma_data = {p: self._ma(fc, p)[si:ei] for p in [5, 10, 30, 60, 120]}
            ema_f = self._ema(fc, 12); ema_s = self._ema(fc, 26)
            dif = [f - s for f, s in zip(ema_f, ema_s)] if ema_f and ema_s else []
            dea = self._ema(dif, 9)
            macd_h = [2 * (dif[i] - dea[i]) for i in range(len(dif))] if dif and dea else []
            dif = dif[si:ei]; dea = dea[si:ei]; macd_h = macd_h[si:ei]
            divs = self._detect_divergences(closes, dif)
            w, h = self.width(), self.height()
            # 保证成交量和MACD区域有足够的最小高度，K线占剩余空间
            mb = h - 5
            # 小窗口时给MACD+成交量分配固定比例，确保MACD不被裁剪
            macd_h_min = max(75, int(h * 0.19))
            vol_h_min = max(50, int(h * 0.10))
            # 总量不能超过可用空间
            total_bottom = macd_h_min + vol_h_min
            max_bottom = h - 40  # K线至少留40px
            if total_bottom > max_bottom:
                scale = max_bottom / total_bottom
                macd_h_min = int(macd_h_min * scale)
                vol_h_min = int(vol_h_min * scale)
            vb = mb - macd_h_min
            kb = vb - vol_h_min
            m = {'l': 65, 'r': 15, 't': 25, 'b': 5}
            kw = w - m['l'] - m['r']; kh = kb - m['t']
            # K线
            all_rp = highs + lows
            for period, ma in ma_data.items():
                valid = [x for x in ma if not np.isnan(x)]
                if valid: all_rp.extend(valid)
            if not all_rp: return
            mxp, mnp = max(all_rp), min(all_rp)
            pr = mxp - mnp
            if pr == 0: pr = mxp * 0.02 or 1
            mxp += pr * 0.05; mnp -= pr * 0.05; pr = mxp - mnp
            p.setPen(QPen(GRID, 1, Qt.PenStyle.DotLine))
            for i in range(5):
                y = m['t'] + kh * i / 4
                p.drawLine(int(m['l']), int(y), int(w - m['r']), int(y))
            cw = max(2, kw / len(vk) * 0.7)
            for i in range(len(vk)):
                x = m['l'] + kw * (i + 0.5) / len(vk)
                o, c, hi, lo = opens[i], closes[i], highs[i], lows[i]
                clr = RED if c >= o else GREEN
                p.setPen(QPen(clr, 1))
                p.drawLine(int(x), int(m['t'] + kh * (1 - (hi - mnp) / pr)),
                           int(x), int(m['t'] + kh * (1 - (lo - mnp) / pr)))
                if abs(c - o) < 0.001:
                    yc = m['t'] + kh * (1 - (c - mnp) / pr)
                    p.drawLine(int(x - cw / 2), int(yc), int(x + cw / 2), int(yc))
                else:
                    top, bot = max(o, c), min(o, c)
                    p.fillRect(QRectF(x - cw / 2, m['t'] + kh * (1 - (top - mnp) / pr), cw,
                                       kh * (top - bot) / pr), clr)
            for period in [5, 10, 30, 60, 120]:
                ma = ma_data[period]; clr = self.MA_COLORS.get(period, WHITE)
                p.setPen(QPen(clr, 1.5 if period <= 10 else 1))
                lx, ly = None, None
                for i in range(len(ma)):
                    if np.isnan(ma[i]): lx, ly = None, None; continue
                    x = m['l'] + kw * (i + 0.5) / len(ma)
                    y = m['t'] + kh * (1 - (ma[i] - mnp) / pr)
                    if lx is not None: p.drawLine(int(lx), int(ly), int(x), int(y))
                    lx, ly = x, y
            # K线主图标注背离价格（价格标签）
            self._draw_price_divergences(p, m, kw, kh, mnp, pr, vk, divs)
            p.setFont(QFont("Microsoft YaHei", 8))
            for i in range(5):
                prc = mxp - pr * i / 4
                p.setPen(GRAY); p.drawText(2, int(m['t'] + kh * i / 4 + 4), f"{prc:.2f}")
            lx = int(m['l'] + 5)
            for period in [5, 10, 30, 60, 120]:
                p.setPen(self.MA_COLORS.get(period, WHITE)); p.drawText(lx, int(m['t'] + 5), f"MA{period}")
                lx += 55
            # 成交量
            vt, vh = kb, vb - kb
            if vols:
                mv = max(vols) if vols else 1
                p.setPen(QPen(GRID, 1, Qt.PenStyle.DotLine))
                for i in range(3):
                    y = vt + vh * i / 2
                    p.drawLine(int(m['l']), int(y), int(w - m['r']), int(y))
                for i, vol in enumerate(vols):
                    x = m['l'] + kw * (i + 0.5) / len(vols)
                    bh = (vol / mv) * vh if mv > 0 else 0
                    clr = RED if closes[i] >= opens[i] else GREEN
                    p.fillRect(QRectF(x - cw / 2, vt + vh - bh, cw, bh), clr)
                p.setPen(GRAY); p.setFont(QFont("Microsoft YaHei", 8))
                p.drawText(2, int(vt + 10), f"{mv / 100:,.0f}手")
            # MACD
            mt, mh = vb, mb - vb
            if dif and dea and macd_h:
                all_m = [x for x in dif + dea + macd_h if not np.isnan(x)]
                if all_m:
                    mmx = max(abs(max(all_m)), abs(min(all_m)))
                    if mmx == 0: mmx = 0.1
                    zero_y = mt + mh / 2
                    p.setPen(QPen(QColor("#555555"), 0.5))
                    p.drawLine(int(m['l']), int(zero_y), int(w - m['r']), int(zero_y))
                    for i in range(len(macd_h)):
                        if np.isnan(macd_h[i]): continue
                        x = m['l'] + kw * (i + 0.5) / len(macd_h)
                        bh = abs(macd_h[i]) / (2 * mmx) * mh
                        if macd_h[i] >= 0: p.fillRect(QRectF(x - cw / 2, zero_y - bh, cw, bh), RED)
                        else: p.fillRect(QRectF(x - cw / 2, zero_y, cw, bh), GREEN)
                    p.setPen(QPen(WHITE, 1))
                    lx, ly = None, None
                    for i in range(len(dif)):
                        if np.isnan(dif[i]): continue
                        x = m['l'] + kw * (i + 0.5) / len(dif)
                        y = mt + mh * (1 - (dif[i] + mmx) / (2 * mmx))
                        if lx is not None: p.drawLine(int(lx), int(ly), int(x), int(y))
                        lx, ly = x, y
                    p.setPen(QPen(GOLD, 1))
                    lx, ly = None, None
                    for i in range(len(dea)):
                        if np.isnan(dea[i]): continue
                        x = m['l'] + kw * (i + 0.5) / len(dea)
                        y = mt + mh * (1 - (dea[i] + mmx) / (2 * mmx))
                        if lx is not None: p.drawLine(int(lx), int(ly), int(x), int(y))
                        lx, ly = x, y
                    # MACD 顶背离/底背离标记
                    self._draw_divergences(p, m, kw, mmx, mh, mt, dif, divs)
                    p.setFont(QFont("Microsoft YaHei", 8))
                    p.setPen(WHITE); p.drawText(int(m['l'] + 5), int(mt + 12), "DIF")
                    p.setPen(GOLD); p.drawText(int(m['l'] + 35), int(mt + 12), "DEA")
                    p.setPen(GRAY); p.drawText(int(m['l'] + 65), int(mt + 12), "MACD")
            # 日期
            p.setPen(GRAY); p.setFont(QFont("Microsoft YaHei", 8))
            step = max(1, len(vk) // 6)
            for i in range(0, len(vk), step):
                x = m['l'] + kw * (i + 0.5) / len(vk)
                ds = vk[i].get('date', '')
                if len(ds) >= 8: p.drawText(int(x - 15), int(h - 2), ds[4:8])

            # 悬浮提示
            if self._hover_idx is not None and 0 <= self._hover_idx < len(vk):
                hi = self._hover_idx
                hx = m['l'] + kw * (hi + 0.5) / len(vk)
                # 十字竖线
                p.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.PenStyle.DashLine))
                p.drawLine(int(hx), int(m['t']), int(hx), int(h - 2))
                # 十字横线 + 右侧价格标签
                if self._hover_y is not None:
                    hy = max(m['t'], min(kb, self._hover_y))
                    p.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.PenStyle.DashLine))
                    p.drawLine(int(m['l']), int(hy), int(w - m['r']), int(hy))
                    # 根据鼠标Y坐标反算价格
                    hover_price = mxp - pr * (hy - m['t']) / kh
                    price_text = f"{hover_price:.2f}"
                    p.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(255, 255, 255, 210))
                    label_w, label_h = 60, 18
                    lx = int(w - m['r'] - label_w)
                    ly = int(hy - label_h / 2)
                    p.drawRoundedRect(QRectF(lx, ly, label_w, label_h), 3, 3)
                    p.setPen(QColor(0, 0, 0))
                    p.drawText(QRectF(lx, ly, label_w, label_h), Qt.AlignmentFlag.AlignCenter, price_text)
                # 数据
                bar = vk[hi]
                o, c_val, hi_p, lo_p = bar['open'], bar['close'], bar['high'], bar['low']
                # 涨跌幅：前一日收盘为基准
                abs_idx = si + hi
                if abs_idx > 0:
                    prev_c = self._data[abs_idx - 1]['close']
                    chg = (c_val - prev_c) / prev_c * 100 if prev_c else 0
                else:
                    chg = 0
                amt = bar.get('amount', 0)
                tov = bar.get('turnover', None)
                lines = [
                    f"日期: {bar['date']}",
                    f"开盘: {o:.2f}  收盘: {c_val:.2f}",
                    f"最高: {hi_p:.2f}  最低: {lo_p:.2f}",
                    f"涨跌幅: {chg:+.2f}%",
                ]
                if amt > 0:
                    amt_str = f"{amt/1e8:.2f}亿" if amt >= 1e8 else f"{amt/1e4:.0f}万"
                    lines.append(f"成交额: {amt_str}")
                if tov is not None:
                    lines.append(f"换手率: {tov:.2f}%")
                # 测量文本大小
                p.setFont(QFont("Microsoft YaHei", 9))
                fm = p.fontMetrics()
                tw = max(fm.boundingRect(line).width() for line in lines) + 16
                th = fm.height() * len(lines) + 10
                # 决定提示框位置（偏右时放左边）
                tx = int(hx - tw - 15) if hx > w / 2 else int(hx + 15)
                ty = int(m['t'] + 5)
                if ty + th > h - 5:
                    ty = max(0, int(h - th - 5))
                # 半透明背景
                bg = QColor(20, 30, 50, 215)
                p.setPen(QPen(QColor(100, 140, 200), 1))
                p.setBrush(bg)
                p.drawRoundedRect(QRectF(tx, ty, tw, th), 4, 4)
                # 文本
                p.setPen(QColor(220, 220, 220))
                for j, line in enumerate(lines):
                    ly = ty + fm.height() + j * fm.height() + 2
                    p.drawText(int(tx + 8), int(ly), line)
        except Exception:
            import traceback; traceback.print_exc()
            p.setPen(RED); p.setFont(QFont("Microsoft YaHei", 10))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "图表渲染异常，请刷新")
        finally:
            p.end()

    def wheelEvent(self, event):
        d = event.angleDelta().y()
        self._visible = max(30, self._visible - 20) if d > 0 else min(len(self._data) if self._data else 300, self._visible + 20)
        if self._data: self._start_idx = max(0, min(self._start_idx, max(0, len(self._data) - self._visible)))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True; self._dx = event.position().x()

    def mouseMoveEvent(self, event):
        x, y = event.position().x(), event.position().y()
        if self._dragging and self._data:
            dx = x - self._dx; self._dx = x
            new_idx = self._start_idx + int(-dx / 5)
            self._start_idx = max(0, min(new_idx, max(0, len(self._data) - self._visible)))
            self._hover_idx = None; self._hover_x = None; self._hover_y = None
            self.update()
            return
        # 悬浮计算
        if self._data:
            w, m = self.width(), {'l': 65, 'r': 15}
            kw = w - m['l'] - m['r']
            ei = min(max(0, self._start_idx + self._visible), len(self._data))
            si = max(0, min(self._start_idx, len(self._data) - 1))
            vk = self._data[si:ei]
            if vk:
                idx = max(0, min(len(vk) - 1, int((x - m['l']) / kw * len(vk))))
                self._hover_idx = idx
                self._hover_x = x; self._hover_y = y
                self.update()

    def leaveEvent(self, event):
        if self._hover_idx is not None:
            self._hover_idx = None; self._hover_x = None; self._hover_y = None
            self.update()

    def mouseReleaseEvent(self, event): self._dragging = False


class FlowPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self._init_ui()

    def _init_ui(self):
        l = QVBoxLayout(self); l.setContentsMargins(10, 10, 10, 10)

        # --- 主力趋势 ---
        g2 = QGroupBox("主力趋势")
        g2.setStyleSheet("QGroupBox{color:#F39C12;font-weight:bold;border:1px solid #2A2A4A;border-radius:6px;margin-top:10px;padding-top:16px;}")
        g2l = QGridLayout(g2); g2l.setSpacing(8)
        for i, h in enumerate(['5日净流入', '10日净流入', '20日净流入']):
            lb = QLabel(h); lb.setStyleSheet("color:#AAAAAA;font-size:12px;font-weight:bold;")
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter); g2l.addWidget(lb, 0, i)
        self.t5 = QLabel('-'); self.t10 = QLabel('-'); self.t20 = QLabel('-')
        for i, lb in enumerate([self.t5, self.t10, self.t20]):
            lb.setStyleSheet("font-size:14px;font-weight:bold;"); lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            g2l.addWidget(lb, 1, i)
        l.addWidget(g2)

        # --- 大宗交易 ---
        g3 = QGroupBox("近半年大宗交易")
        g3.setStyleSheet("QGroupBox{color:#F39C12;font-weight:bold;border:1px solid #2A2A4A;border-radius:6px;margin-top:10px;padding-top:16px;}")
        g3l = QVBoxLayout(g3)
        self.bt = QTableWidget(0, 5)
        self.bt.setHorizontalHeaderLabels(['日期', '成交价', '成交量(万股)', '成交额(万元)', '折溢价率'])
        self.bt.verticalHeader().setVisible(False); self.bt.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.bt.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.bt.horizontalHeader(); hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.bt.setStyleSheet(f"QTableWidget{{background-color:{DARK.name()};color:white;gridline-color:#2A2A4A;border:none;font-size:11px;}}QHeaderView::section{{background-color:#16213E;color:#AAAAAA;padding:4px;border:none;}}")
        g3l.addWidget(self.bt); l.addWidget(g3); l.addStretch()

    def update_data(self, flow, block_trades):
        if sip.isdeleted(self): return
        if flow:
            # --- 主力趋势 ---
            for p, lb in [('5d', self.t5), ('10d', self.t10), ('20d', self.t20)]:
                v = flow.get(f'main_net_{p}', 0)
                if v:
                    av = abs(v)
                    sign = '-' if v < 0 else ''
                    if av >= 1e8:
                        t = f"{sign}{av / 1e8:.2f}亿"
                    else:
                        t = f"{sign}{av / 10000:,.0f}万"
                else:
                    t = '-'
                c = RED if v > 0 else GREEN if v < 0 else WHITE
                lb.setText(t); lb.setStyleSheet(f"color:{c.name()};font-size:14px;font-weight:bold;")
        else:
            for lb in [self.t5, self.t10, self.t20]:
                lb.setText('-'); lb.setStyleSheet("font-size:14px;font-weight:bold;")

        if block_trades:
            self.bt.setRowCount(len(block_trades))
            for i, bt in enumerate(block_trades):
                self.bt.setItem(i, 0, QTableWidgetItem(bt.get('date', '')))
                self.bt.setItem(i, 1, QTableWidgetItem(f"{bt.get('price', 0):.2f}"))
                self.bt.setItem(i, 2, QTableWidgetItem(f"{bt.get('volume', 0) / 10000:,.2f}"))
                self.bt.setItem(i, 3, QTableWidgetItem(f"{bt.get('amount', 0) / 10000:,.0f}"))
                dr = bt.get('discount_rate', 0); di = QTableWidgetItem(f"{dr:+.2f}%")
                di.setForeground(QBrush(GREEN if dr < 0 else RED)); self.bt.setItem(i, 4, di)


class NewsPanel(QWidget):
    """公告资讯面板"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._news = []
        l = QVBoxLayout(self); l.setContentsMargins(10, 10, 10, 10)
        t = QLabel("新闻资讯"); t.setStyleSheet("font-size:16px;font-weight:bold;color:#F39C12;padding:4px;"); l.addWidget(t)
        hint = QLabel("双击公告行在浏览器中查看原文  |  数据来源：东方财富公告中心"); hint.setStyleSheet("color:#888899;font-size:11px;padding:0 0 4px 4px;")
        l.addWidget(hint)
        self.tb = QTableWidget(0, 2)
        self.tb.setHorizontalHeaderLabels(['日期', '公告标题'])
        self.tb.verticalHeader().setVisible(False); self.tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tb.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tb.setStyleSheet(f"QTableWidget{{background-color:{DARK.name()};color:white;gridline-color:#2A2A4A;border:none;font-size:12px;}}QTableWidget::item{{padding:6px 8px;}}QHeaderView::section{{background-color:#16213E;color:#AAAAAA;padding:6px;border:none;}}QTableWidget::item:hover{{background-color:#1F3A5F;}}")
        self.tb.cellDoubleClicked.connect(self._open_url)
        l.addWidget(self.tb)

    def update_data(self, news):
        if sip.isdeleted(self): return
        self._news = news or []
        if not self._news:
            self.tb.setRowCount(0); return
        self.tb.setRowCount(len(self._news))
        for i, n in enumerate(self._news):
            date_str = f"{n.get('date', '')} {n.get('time', '')}".strip()
            di = QTableWidgetItem(date_str)
            di.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tb.setItem(i, 0, di)
            self.tb.setItem(i, 1, QTableWidgetItem(n.get('title', '')))

    def _open_url(self, row, _col):
        if row < 0 or row >= len(self._news):
            return
        url = self._news[row].get('url', '')
        if url:
            QDesktopServices.openUrl(QUrl(url))


class NoticePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._notices = []
        l = QVBoxLayout(self); l.setContentsMargins(10, 10, 10, 10)
        t = QLabel("公司公告"); t.setStyleSheet("font-size:16px;font-weight:bold;color:#F39C12;padding:4px;"); l.addWidget(t)
        hint = QLabel("双击公告行查看详情"); hint.setStyleSheet("color:#888899;font-size:11px;padding:0 0 4px 4px;")
        l.addWidget(hint)
        self.tb = QTableWidget(0, 3)
        self.tb.setHorizontalHeaderLabels(['日期', '公告标题', '类型'])
        self.tb.verticalHeader().setVisible(False); self.tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tb.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tb.setStyleSheet(f"QTableWidget{{background-color:{DARK.name()};color:white;gridline-color:#2A2A4A;border:none;font-size:12px;}}QTableWidget::item{{padding:6px 8px;}}QHeaderView::section{{background-color:#16213E;color:#AAAAAA;padding:6px;border:none;}}QTableWidget::item:hover{{background-color:#1F3A5F;}}")
        self.tb.cellDoubleClicked.connect(self._show_detail)
        l.addWidget(self.tb)

    def update_data(self, notices):
        if sip.isdeleted(self): return
        self._notices = notices or []
        if not self._notices:
            self.tb.setRowCount(0); return
        self.tb.setRowCount(len(self._notices))
        for i, n in enumerate(self._notices):
            date_item = QTableWidgetItem(n.get('date', ''))
            date_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tb.setItem(i, 0, date_item)
            title_item = QTableWidgetItem(n.get('title', ''))
            self.tb.setItem(i, 1, title_item)
            ti = QTableWidgetItem(n.get('type', '')); ti.setForeground(QBrush(GOLD))
            ti.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tb.setItem(i, 2, ti)

    def _show_detail(self, row, _col):
        if row < 0 or row >= len(self._notices):
            return
        n = self._notices[row]
        dlg = QDialog(self)
        dlg.setWindowTitle("公告详情")
        dlg.setMinimumSize(550, 400)
        dlg.setStyleSheet(f"QDialog{{background-color:#1A1A2E;}}")
        layout = QVBoxLayout(dlg); layout.setContentsMargins(20, 16, 20, 16); layout.setSpacing(12)

        # 公告类型标签
        type_lbl = QLabel(n.get('type', '公告'))
        type_lbl.setStyleSheet("font-size:11px;color:#F39C12;background-color:#2A2A0A;border-radius:4px;padding:2px 10px;")
        type_lbl.setFixedHeight(24)
        layout.addWidget(type_lbl)

        # 标题
        title_lbl = QLabel(n.get('title', ''))
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("font-size:15px;font-weight:bold;color:#FFFFFF;line-height:1.4;")
        layout.addWidget(title_lbl)

        # 日期
        date_lbl = QLabel(n.get('date', ''))
        date_lbl.setStyleSheet("font-size:12px;color:#888899;")
        layout.addWidget(date_lbl)

        layout.addStretch()

        # 按钮区域
        btn_layout = QHBoxLayout(); btn_layout.setSpacing(10)

        open_btn = QPushButton("在浏览器中查看原文")
        open_btn.setStyleSheet("""
            QPushButton{background-color:#E67E22;color:white;border:none;border-radius:6px;
            padding:10px 24px;font-size:13px;font-weight:bold;}
            QPushButton:hover{background-color:#D35400;}
        """)
        url = n.get('url', '')
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)) if url else None)
        open_btn.setEnabled(bool(url))
        btn_layout.addWidget(open_btn)

        close_btn = QPushButton("关闭")
        close_btn.setStyleSheet("""
            QPushButton{background-color:#2A2A4A;color:#AAAAAA;border:none;border-radius:6px;
            padding:10px 24px;font-size:13px;}
            QPushButton:hover{background-color:#3A3A5A;color:white;}
        """)
        close_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)
        dlg.exec()


class FinancialPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _create_card(self, name):
        """创建指标卡片：名称 + 值 + 可选变化率"""
        card = QWidget()
        card.setStyleSheet(f"background-color:{CARD.name()};border-radius:6px;padding:8px 10px;")
        cl = QVBoxLayout(card); cl.setContentsMargins(8, 6, 8, 6); cl.setSpacing(2)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lb_name = QLabel(name); lb_name.setStyleSheet("color:#AAAAAA;font-size:11px;")
        lb_name.setAlignment(Qt.AlignmentFlag.AlignCenter); cl.addWidget(lb_name)
        lb_val = QLabel("--"); lb_val.setStyleSheet("color:white;font-size:14px;font-weight:bold;")
        lb_val.setAlignment(Qt.AlignmentFlag.AlignCenter); cl.addWidget(lb_val)
        lb_growth = QLabel(""); lb_growth.setStyleSheet("font-size:10px;")
        lb_growth.setAlignment(Qt.AlignmentFlag.AlignCenter); cl.addWidget(lb_growth)
        card._val_label = lb_val; card._growth_label = lb_growth
        return card

    def _set_card(self, card, value, growth=""):
        card._val_label.setText(str(value) if value else "--")
        if growth:
            gs = str(growth)
            c = RED.name() if (gs.startswith('+') or (gs[0].isdigit() and float(gs.rstrip('%')) > 0)) else GREEN.name() if (gs.startswith('-') or (gs[0].isdigit() and float(gs.rstrip('%')) < 0)) else GRAY.name()
            if not gs.startswith('+') and not gs.startswith('-') and gs != '':
                gs = '+' + gs
            card._growth_label.setText(gs)
            card._growth_label.setStyleSheet(f"color:{c};font-size:10px;")
        else:
            card._growth_label.setText("")
        card._val_label.setStyleSheet("color:white;font-size:14px;font-weight:bold;")

    def _make_group(self, title, cards, cols=3):
        """创建带标题的分组框"""
        g = QGroupBox(title)
        g.setStyleSheet(
            f"QGroupBox{{color:{GOLD.name()};font-weight:bold;"
            f"border:1px solid #2A2A4A;border-radius:6px;"
            f"margin-top:10px;padding-top:16px;font-size:13px;}}")
        grid = QGridLayout(g); grid.setSpacing(8)
        for i, card in enumerate(cards):
            grid.addWidget(card, i // cols, i % cols)
        return g

    def _init_ui(self):
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"QScrollArea{{background-color:transparent;border:none;}}QScrollBar:vertical{{width:6px;background:{DARK.name()};}}QScrollBar::handle:vertical{{background:#3A3A5A;border-radius:3px;min-height:30px;}}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0px;}}")

        content = QWidget()
        content.setStyleSheet("background-color:transparent;")
        l = QVBoxLayout(content); l.setContentsMargins(10, 10, 10, 10)
        l.setSpacing(6)

        # 报告期标签
        self.date_label = QLabel("")
        self.date_label.setStyleSheet(f"color:{GRAY.name()};font-size:12px;padding:4px 0px;")
        self.date_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        l.addWidget(self.date_label)

        # 核心财务指标（4列）
        self.card_revenue = self._create_card("营业收入")
        self.card_net_profit = self._create_card("净利润")
        self.card_deducted = self._create_card("扣非净利润")
        self.card_gross = self._create_card("毛利润")
        g1 = self._make_group("核心财务指标",
            [self.card_revenue, self.card_net_profit, self.card_deducted, self.card_gross], cols=4)
        l.addWidget(g1)

        # 每股指标与盈利能力（5列）
        self.card_eps = self._create_card("每股收益")
        self.card_bps = self._create_card("每股净资产")
        self.card_roe = self._create_card("净资产收益率")
        self.card_gross_margin = self._create_card("毛利率")
        self.card_net_margin = self._create_card("净利率")
        g2 = self._make_group("每股指标与盈利能力",
            [self.card_eps, self.card_bps, self.card_roe, self.card_gross_margin, self.card_net_margin], cols=5)
        l.addWidget(g2)

        # 偿债能力与股本（4列）
        self.card_debt = self._create_card("资产负债率")
        self.card_current = self._create_card("流动比率")
        self.card_quick = self._create_card("速动比率")
        self.card_shares = self._create_card("总股本")
        g3 = self._make_group("偿债能力与股本",
            [self.card_debt, self.card_current, self.card_quick, self.card_shares], cols=4)
        l.addWidget(g3)

        l.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def update_data(self, financial):
        if sip.isdeleted(self): return
        if not financial:
            for c in [self.card_revenue, self.card_net_profit, self.card_deducted, self.card_gross,
                       self.card_eps, self.card_bps, self.card_roe, self.card_gross_margin,
                       self.card_net_margin, self.card_debt, self.card_current, self.card_quick, self.card_shares]:
                c._val_label.setText("--")
                c._growth_label.setText("")
            self.date_label.setText("")
            return

        rd = financial.get('report_date', '')
        self.date_label.setText(f"报告期：{rd}" if rd else "")

        # 解析增长率正负
        def _growth_str(key):
            g = financial.get(key, '')
            if not g or g == '-':
                return ''
            s = str(g).replace('%', '')
            try:
                v = float(s)
                return f"{v:+.2f}%"
            except ValueError:
                return str(g)

        # 核心财务指标
        self._set_card(self.card_revenue, financial.get('revenue', '--'), _growth_str('revenue_growth'))
        self._set_card(self.card_net_profit, financial.get('net_profit', '--'), _growth_str('net_profit_growth'))
        self._set_card(self.card_deducted, financial.get('deducted_profit', '--'), _growth_str('deducted_profit_growth'))
        self._set_card(self.card_gross, financial.get('gross_profit', '--'))

        # 每股指标与盈利能力
        self._set_card(self.card_eps, financial.get('eps', '--'))
        self._set_card(self.card_bps, financial.get('bps', '--'))
        self._set_card(self.card_roe, financial.get('roe', '--'))
        self._set_card(self.card_gross_margin, financial.get('gross_margin', '--'))
        self._set_card(self.card_net_margin, financial.get('net_margin', '--'))

        # 偿债能力与股本
        self._set_card(self.card_debt, financial.get('debt_ratio', '--'))
        self._set_card(self.card_current, financial.get('current_ratio', '--'))
        self._set_card(self.card_quick, financial.get('quick_ratio', '--'))
        self._set_card(self.card_shares, financial.get('total_shares', '--'))


class ResearchPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = []
        l = QVBoxLayout(self); l.setContentsMargins(10, 10, 10, 10)
        t = QLabel("近半年机构调研"); t.setStyleSheet("font-size:16px;font-weight:bold;color:#F39C12;padding:4px;"); l.addWidget(t)
        self.tb = QTableWidget(0, 4)
        self.tb.setHorizontalHeaderLabels(['调研日期', '调研机构', '机构类型', '调研内容摘要'])
        self.tb.verticalHeader().setVisible(False); self.tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tb.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tb.cellDoubleClicked.connect(self._show_detail)
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tb.setStyleSheet(f"QTableWidget{{background-color:{DARK.name()};color:white;gridline-color:#2A2A4A;border:none;font-size:12px;}}QTableWidget::item{{padding:6px 8px;}}QHeaderView::section{{background-color:#16213E;color:#AAAAAA;padding:6px;border:none;}}QTableWidget::item:hover{{background-color:#1F3A5F;}}")
        l.addWidget(self.tb)

    def update_data(self, research):
        if sip.isdeleted(self): return
        self._data = research or []
        if not self._data: self.tb.setRowCount(0); return
        self.tb.setRowCount(len(self._data))
        for i, r in enumerate(self._data):
            self.tb.setItem(i, 0, QTableWidgetItem(r.get('date', '')))
            self.tb.setItem(i, 1, QTableWidgetItem(r.get('institution', '')))
            self.tb.setItem(i, 2, QTableWidgetItem(r.get('institution_type', '')))
            sm = r.get('summary', '')
            if len(sm) > 80: sm = sm[:80] + '...'
            self.tb.setItem(i, 3, QTableWidgetItem(sm))

    def _show_detail(self, row, col):
        if row < 0 or row >= len(self._data):
            return
        r = self._data[row]
        dlg = QDialog(self)
        dlg.setWindowTitle("机构调研详情")
        dlg.setMinimumSize(520, 360)
        dlg.setStyleSheet(f"QDialog{{background-color:{DARK.name()};}}")
        dl = QVBoxLayout(dlg); dl.setContentsMargins(16, 16, 16, 16); dl.setSpacing(10)

        # 标题：调研日期 + 机构名称
        title = QLabel(f"{r.get('date', '')}  ·  {r.get('institution', '')}")
        title.setStyleSheet(f"font-size:16px;font-weight:bold;color:{GOLD.name()};")
        title.setWordWrap(True); dl.addWidget(title)

        # 信息条
        info_grid = QGridLayout(); info_grid.setSpacing(8)
        def _info_row(grid, row_idx, label, value):
            lb = QLabel(label); lb.setStyleSheet(f"color:{GRAY.name()};font-size:12px;")
            vl = QLabel(value if value else '--')
            vl.setStyleSheet("color:white;font-size:12px;"); vl.setWordWrap(True)
            grid.addWidget(lb, row_idx, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(vl, row_idx, 1)
        _info_row(info_grid, 0, "调研日期：", r.get('date', ''))
        _info_row(info_grid, 1, "调研机构：", r.get('institution', ''))
        _info_row(info_grid, 2, "机构类型：", r.get('institution_type', ''))
        _info_row(info_grid, 3, "调研方式：", r.get('way', ''))
        _info_row(info_grid, 4, "调研地点：", r.get('place', ''))
        dl.addLayout(info_grid)

        # 分隔线
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{GRID.name()};"); dl.addWidget(sep)

        # 完整内容
        ct_label = QLabel("调研内容"); ct_label.setStyleSheet(f"font-size:13px;font-weight:bold;color:{GOLD.name()};")
        dl.addWidget(ct_label)
        ct = QTextEdit(); ct.setReadOnly(True)
        ct.setStyleSheet(f"QTextEdit{{background-color:{CARD.name()};color:white;border:1px solid #2A2A4A;border-radius:4px;font-size:12px;padding:10px;line-height:1.6;}}")
        ct.setPlainText(r.get('summary', '暂无内容'))
        dl.addWidget(ct)

        # 关闭按钮
        btn = QPushButton("关闭"); btn.clicked.connect(dlg.accept)
        btn.setStyleSheet("QPushButton{background-color:#F39C12;color:#1A1A2E;font-weight:bold;padding:8px 32px;border-radius:4px;}QPushButton:hover{background-color:#E5A00D;}")
        btn_row = QHBoxLayout(); btn_row.addStretch(); btn_row.addWidget(btn); dl.addLayout(btn_row)

        dlg.exec()


class StockInfoBar(QWidget):
    """股票详情信息栏 - 15项关键行情指标"""
    FIELDS = [
        # (标签, 键名, 列, 行, 格式函数)
        ('当前价', 'current', 0, 0, lambda v: f'{v:.2f}'),
        ('涨幅', 'change_pct', 1, 0, lambda v: f'{v:+.2f}%'),
        ('换手率', 'turnover_rate', 2, 0, lambda v: f'{v:.2f}%'),
        ('涨停价', 'limit_up', 3, 0, lambda v: f'{v:.2f}'),
        ('跌停价', 'limit_down', 4, 0, lambda v: f'{v:.2f}'),
        ('最高价', 'high', 0, 1, lambda v: f'{v:.2f}'),
        ('最低价', 'low', 1, 1, lambda v: f'{v:.2f}'),
        ('振幅', 'amplitude', 2, 1, lambda v: f'{v:.2f}%'),
        ('昨收', 'yesterday_close', 3, 1, lambda v: f'{v:.2f}'),
        ('开盘价', 'open', 4, 1, lambda v: f'{v:.2f}'),
        ('成交量', 'volume', 0, 2, lambda v: f'{v / 10000:.0f}万'),
        ('成交额', 'amount', 1, 2, lambda v: f'{v / 10000:.2f}亿' if v > 0 else '-'),
        ('市盈率', 'pe', 2, 2, lambda v: f'{v:.1f}' if v > 0 else '-'),
        ('市值', 'market_cap', 3, 2, lambda v: f'{v:.2f}亿' if v > 0 else '-'),
        ('量比', 'volume_ratio', 4, 2, lambda v: f'{v:.2f}'),
    ]
    RED_KEYS = {'change_pct', 'limit_up'}   # 正值红色
    GREEN_KEYS = {'limit_down'}              # 负值或单值绿色

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value_labels = {}
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet(f"background-color:{CARD.name()};border-radius:6px;padding:4px;")
        g = QGridLayout(self); g.setContentsMargins(6, 4, 6, 4); g.setSpacing(4)
        for cols in range(5):
            g.setColumnStretch(cols, 1)

        for label, key, col, row, fmt in self.FIELDS:
            cell = QWidget()
            cell.setStyleSheet("background-color:transparent;")
            cl = QVBoxLayout(cell); cl.setContentsMargins(0, 0, 0, 0); cl.setSpacing(1)
            cl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            lb = QLabel(label)
            lb.setStyleSheet("color:#888888;font-size:10px;")
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(lb)

            val = QLabel('--')
            val.setStyleSheet("color:#FFFFFF;font-size:13px;font-weight:bold;")
            val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(val)

            g.addWidget(cell, row, col)
            self.value_labels[key] = val

    def update_data(self, info):
        if sip.isdeleted(self): return
        """info: dict with keys matching field keys above"""
        if not info:
            for key, lbl in self.value_labels.items():
                lbl.setText('--')
                lbl.setStyleSheet("color:#FFFFFF;font-size:13px;font-weight:bold;")
            return
        for label, key, col, row, fmt in self.FIELDS:
            lbl = self.value_labels.get(key)
            if lbl is None:
                continue
            v = info.get(key, 0)
            txt = fmt(v) if callable(fmt) else str(v)
            lbl.setText(txt)
            # 染色
            color = "#FFFFFF"
            if key in ('turnover_rate', 'limit_up'):
                color = RED.name()
            elif key == 'limit_down':
                color = GREEN.name()
            elif key == 'change_pct':
                if v > 0:
                    color = RED.name()
                elif v < 0:
                    color = GREEN.name()
            elif key in ('open', 'current'):
                if info.get('yesterday_close', 0) > 0:
                    if v > info['yesterday_close']:
                        color = RED.name()
                    elif v < info['yesterday_close']:
                        color = GREEN.name()
            lbl.setStyleSheet(f"color:{color};font-size:13px;font-weight:bold;")


class DetailWindow(QWidget):
    def __init__(self, code, name, parent=None):
        super().__init__(parent); self.code = code; self.name = name; self._cache = {}
        self._old_threads = []  # 保持引用，防止正在运行的 C++ QThread 被 GC 导致 segfault
        self._load_id = 0  # 加载版本号，递增用于丢弃陈旧回调
        self._init_ui(); self._load_data('intraday')

    def _init_ui(self):
        self.setWindowTitle(f"{self.code} {self.name} - 股票详情")
        screen = QApplication.primaryScreen().availableGeometry()
        w, h = int(screen.width() * 0.96), int(screen.height() * 0.9)
        self.resize(w, h)
        self.move((screen.width() - w) // 2, (screen.height() - h) // 2)
        self.setMinimumSize(1000, 700)
        self.setStyleSheet(f"background-color:{DARK.name()};")
        ml = QVBoxLayout(self); ml.setContentsMargins(10, 10, 10, 10); ml.setSpacing(8)
        top = QHBoxLayout()
        t = QLabel(f"{self.code}  {self.name}")
        t.setStyleSheet("font-size:22px;font-weight:bold;color:#F39C12;"); top.addWidget(t); top.addStretch()
        self.tabs = {}
        for name in ['分时图', '日线图', '资金', '新闻资讯', '公司公告', '财务状况', '机构调研']:
            btn = QPushButton(name); btn.setCheckable(True)
            btn.setStyleSheet("""
                QPushButton{background-color:#16213E;color:#AAAAAA;border:1px solid #2A2A4A;border-radius:4px;padding:8px 16px;font-size:13px;}
                QPushButton:checked{background-color:#0F3460;color:#F39C12;font-weight:bold;border-color:#F39C12;}
                QPushButton:hover{background-color:#1F3A5F;}
            """)
            btn.clicked.connect(lambda _, n=name: self._switch_tab(n))
            self.tabs[name] = btn; top.addWidget(btn)
        self.tabs['分时图'].setChecked(True); ml.addLayout(top)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color:#2A2A4A;max-height:1px;"); ml.addWidget(sep)
        self.pb = QProgressBar(); self.pb.setVisible(False)
        self.pb.setStyleSheet("QProgressBar{border:1px solid #2A2A4A;border-radius:4px;background-color:#16213E;height:16px;}QProgressBar::chunk{background-color:#F39C12;border-radius:3px;}")
        ml.addWidget(self.pb)
        self.st = QLabel("正在加载数据...")
        self.st.setStyleSheet("color:#AAAAAA;font-size:12px;padding:4px;"); ml.addWidget(self.st)
        self.info_bar = StockInfoBar(); ml.addWidget(self.info_bar)
        # 主力净流入
        mf = QHBoxLayout(); mf.setContentsMargins(10, 2, 10, 2)
        self.main_flow_label = QLabel("主力净流入  --")
        self.main_flow_label.setStyleSheet(f"color:{RED.name()};font-size:14px;font-weight:bold;padding:4px 0px;")
        mf.addWidget(self.main_flow_label); mf.addStretch()
        ml.addLayout(mf)
        self.stack = QStackedWidget()
        self.intraday_w = IntradayFullWidget(); self.stack.addWidget(self.intraday_w)
        self.daily_p = DailyChartPanel(); self.stack.addWidget(self.daily_p)
        self.flow_p = FlowPanel(); self.stack.addWidget(self.flow_p)
        self.news_p = NewsPanel(); self.stack.addWidget(self.news_p)
        self.notice_p = NoticePanel(); self.stack.addWidget(self.notice_p)
        self.financial_p = FinancialPanel(); self.stack.addWidget(self.financial_p)
        self.research_p = ResearchPanel(); self.stack.addWidget(self.research_p)
        ml.addWidget(self.stack, 1)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_intraday)
        self._refresh_timer.start(5000)
        # 切页去抖：200ms 内连续切页只触发最后一次加载，避免并发线程导致崩溃
        self._switch_timer = QTimer(self)
        self._switch_timer.setSingleShot(True)
        self._switch_timer.timeout.connect(self._do_switch_load)
        self._pending_tab = None

    def _switch_tab(self, name):
        idx = {'分时图': 0, '日线图': 1, '资金': 2, '新闻资讯': 3, '公司公告': 4, '财务状况': 5, '机构调研': 6}.get(name, 0)
        self.stack.setCurrentIndex(idx)
        for n, btn in self.tabs.items(): btn.setChecked(n == name)
        # 去抖：快速切页时只记录最后的 tab，200ms 无操作后再真正加载
        self._pending_tab = name
        self._switch_timer.start(200)

    def _do_switch_load(self):
        """去抖后真正执行数据加载"""
        from PyQt6 import sip as _sip_g
        if getattr(self, '_closing', False) or _sip_g.isdeleted(self):
            return
        name = self._pending_tab
        if name is None:
            return
        # _load_data 内部通过 _load_id 版本号机制丢弃陈旧回调，无需阻塞等待
        if name == '分时图':
            self._load_data('intraday')  # 分时数据时效性强，切回即刷新
        elif name == '日线图' and 'daily' not in self._cache:
            self._load_data('daily')
        elif name == '资金' and ('flow' not in self._cache or not self._cache.get('block_trades')):
            self._load_data('flow')
        elif name == '新闻资讯' and 'news' not in self._cache:
            self._load_data('notice')
        elif name == '公司公告' and 'notices' not in self._cache:
            self._load_data('notice')
        elif name == '财务状况' and 'financial' not in self._cache:
            self._load_data('financial')
        elif name == '机构调研' and 'research' not in self._cache:
            self._load_data('research')

    def _refresh_intraday(self):
        """定时刷新分时数据（仅当当前在分时图标签页时刷新）"""
        if getattr(self, '_closing', False):
            return
        if self.stack.currentIndex() != 0:
            return  # 不在分时图标签页，跳过刷新
        if hasattr(self, '_load_thread') and self._load_thread is not None:
            try:
                if self._load_thread.isRunning():
                    return  # 上个线程还在跑，跳过本次刷新
            except RuntimeError:
                self._load_thread = None
        self._load_data('intraday')

    def _load_data(self, data_type="all"):
        """启动数据加载线程（asyncio 线程，不可用 quit() 终止）"""
        # 递增版本号，后续 _on_ready 只处理匹配此 ID 的结果
        self._load_id += 1
        current_id = self._load_id
        # 把旧线程移入 _old_threads 保持引用，防止 C++ QThread 被 GC 时仍在运行导致 segfault
        if hasattr(self, '_load_thread') and self._load_thread is not None:
            old_thread = self._load_thread
            from PyQt6 import sip as _sip
            try:
                if _sip.isdeleted(old_thread):
                    # 旧线程的 C++ 对象已被销毁（keep_alive 的 finished→deleteLater 已执行，
                    # 事件循环已删除其 C++ 对象），丢弃僵尸引用，避免对已删除对象调用
                    # finished.connect 抛 RuntimeError。直接创建新线程。
                    self._load_thread = None
                else:
                    try: old_thread.data_ready.disconnect()
                    except (TypeError, RuntimeError): pass
                    try: old_thread.progress.disconnect()
                    except (TypeError, RuntimeError): pass
                    try: old_thread.finished.disconnect()
                    except (TypeError, RuntimeError): pass
                    # 不再尝试 quit() + wait() —— DetailDataThread 使用 asyncio 而非 Qt event loop，quit() 无效
                    self._old_threads.append(old_thread)
                    try:
                        old_thread.finished.connect(lambda t=old_thread: self._cleanup_thread(t))
                    except RuntimeError:
                        pass
            except RuntimeError:
                pass
        self.pb.setVisible(True); self.pb.setRange(0, 0); self.st.setText("正在加载数据...")
        self._load_thread = DetailDataThread(self.code, self.name, data_type, current_id)
        self._load_thread.progress.connect(lambda m: self.st.setText(m))
        self._load_thread.data_ready.connect(self._on_ready)
        self._load_thread.finished.connect(lambda: self._on_thread_done())
        from ui import thread_utils
        thread_utils.keep_alive(self._load_thread)  # 全局引用池兜底：_old_threads 竞态也不销毁运行中 QThread
        self._load_thread.start()

    def _cleanup_thread(self, thread):
        """清理已完成的后台线程"""
        try:
            if thread in self._old_threads:
                self._old_threads.remove(thread)
            if not thread.isFinished():
                thread.wait(2000)
        except RuntimeError:
            pass

    def _on_thread_done(self):
        """当前加载线程完成时的回调"""
        pass  # 可用于后续优化，如减少刷新频率等

    def _on_ready(self, result):
        from PyQt6 import sip as _sip_g
        if getattr(self, '_closing', False) or _sip_g.isdeleted(self):
            return
        # 丢弃陈旧线程的回调结果（由更新版本的 _load_data 取代）
        if not isinstance(result, dict) or result.get('_load_id', -1) != getattr(self, '_load_id', 0):
            return
        self.pb.setVisible(False); self.st.setText("数据加载完成")
        if 'error' in result: self.st.setText(f"加载出错: {result['error']}"); return

        # ── 逐面板独立更新，单个面板失败不影响其他 ──
        def _safe_update(panel_name, update_fn):
            if _sip_g.isdeleted(self): return False
            try:
                update_fn()
                return True
            except Exception:
                import traceback; traceback.print_exc()
                logger.warning(f"{panel_name} 渲染失败")
                return False

        # ── 1. 分时图 + 信息栏 ──
        intraday = result.get('intraday')
        if intraday:
            self._cache['intraday'] = intraday
        cached_intraday = self._cache.get('intraday')
        effective = intraday if isinstance(intraday, dict) else (
            cached_intraday if isinstance(cached_intraday, dict) else {})
        ob = None
        if effective:
            src = intraday if isinstance(intraday, dict) else cached_intraday
            bl = src.get('buy_levels', []) if isinstance(src, dict) else []
            sl = src.get('sell_levels', []) if isinstance(src, dict) else []
            if bl or sl:
                ob = {'buy_levels': bl, 'sell_levels': sl}

        if isinstance(intraday, dict):
            _safe_update("分时图", lambda: self.intraday_w.update_data(intraday, ob, result.get('trade_details', [])))
        stock_info = effective.get('stock_info', {}) if effective else {}
        if stock_info:
            _safe_update("信息栏", lambda: self.info_bar.update_data(stock_info))

        # ── 2. 日K线 ──
        if result.get('daily'):
            self._cache['daily'] = result['daily']
            _safe_update("日K线", lambda: self.daily_p.update_data(result['daily']))

        # ── 3. 资金流向 ──
        if result.get('flow') is not None or result.get('block_trades') is not None:
            self._cache['flow'] = result.get('flow')
            self._cache['block_trades'] = result.get('block_trades')
            _safe_update("资金流向", lambda: self.flow_p.update_data(result.get('flow'), result.get('block_trades')))
            flow_data = result.get('flow')
            if flow_data and not _sip_g.isdeleted(self):
                try:
                    m = flow_data.get('main', 0)
                    if m > 0:
                        c = RED
                        if m >= 1e8:
                            txt = f"主力净流入  +{m / 1e8:.2f}亿"
                        elif m >= 10000:
                            txt = f"主力净流入  +{m / 10000:,.0f}万"
                        else:
                            txt = f"主力净流入  +{m:,.0f}"
                    elif m < 0:
                        c = GREEN
                        am = abs(m)
                        if am >= 1e8:
                            txt = f"主力净流入  -{am / 1e8:.2f}亿"
                        elif am >= 10000:
                            txt = f"主力净流入  -{am / 10000:,.0f}万"
                        else:
                            txt = f"主力净流入  -{am:,.0f}"
                    else:
                        c = WHITE
                        txt = "主力净流入  0"
                    self.main_flow_label.setText(txt)
                    self.main_flow_label.setStyleSheet(f"color:{c.name()};font-size:14px;font-weight:bold;padding:4px 0px;")
                except Exception:
                    import traceback; traceback.print_exc()

        # ── 4. 新闻 / 公告 / 财务 / 调研 ──
        if result.get('news') is not None:
            self._cache['news'] = result['news']
            _safe_update("新闻", lambda: self.news_p.update_data(result['news']))
        if result.get('notices') is not None:
            self._cache['notices'] = result['notices']
            _safe_update("公告", lambda: self.notice_p.update_data(result['notices']))
        if result.get('financial') is not None:
            self._cache['financial'] = result['financial']
            _safe_update("财务", lambda: self.financial_p.update_data(result['financial']))
        if result.get('research') is not None:
            self._cache['research'] = result['research']
            _safe_update("调研", lambda: self.research_p.update_data(result['research']))

    def stop_thread(self):
        """终止所有后台线程，防止退出时进程残留"""
        # 收集所有运行中的线程
        running = []
        if hasattr(self, '_load_thread') and self._load_thread is not None:
            try:
                try: self._load_thread.data_ready.disconnect()
                except Exception: pass
                try: self._load_thread.progress.disconnect()
                except Exception: pass
                try: self._load_thread.finished.disconnect()
                except Exception: pass
            except RuntimeError: pass
            try:
                if self._load_thread.isRunning():
                    running.append(self._load_thread)
            except RuntimeError:
                pass  # C++ 对象已被删除（僵尸引用），丢弃即可
            self._load_thread = None
        if hasattr(self, '_old_threads'):
            for t in list(self._old_threads):
                try:
                    try: t.data_ready.disconnect()
                    except Exception: pass
                    try: t.progress.disconnect()
                    except Exception: pass
                    try: t.finished.disconnect()
                    except Exception: pass
                except RuntimeError: pass
                try:
                    if t.isRunning():
                        running.append(t)
                except RuntimeError:
                    pass  # C++ 对象已被删除（僵尸引用），丢弃即可
            self._old_threads.clear()
        # 先全部 terminate，再统一 wait（并行等待）
        for t in running:
            t.terminate()
        alive_threads = []
        for t in running:
            if not t.wait(500):
                alive_threads.append(t)
        if alive_threads:
            # wait 超时（线程未退出）时保留引用，
            # 避免 running QThread 被 GC 销毁触发 "Destroyed while thread is still running" 崩溃
            self._old_threads = alive_threads
            # 同时登记到全局引用池，进程退出时由 shutdown_all 统一等待
            from ui import thread_utils
            for t in alive_threads:
                thread_utils.keep_alive(t)

    def closeEvent(self, event):
        """安全关闭：标记关闭 + 停止定时器 + 断开所有后台线程信号"""
        self._closing = True
        self._refresh_timer.stop()
        if hasattr(self, '_switch_timer'):
            self._switch_timer.stop()
        self.stop_thread()
        # 清理本窗口及其子树所有已排队的绘制/输入事件，
        # 防止窗口销毁后 Qt 仍向已删除的 widget 派发事件导致崩溃
        try:
            for w in self.findChildren(QWidget):
                QApplication.removePostedEvents(w)
            QApplication.removePostedEvents(self)
        except RuntimeError:
            pass
        super().closeEvent(event)
        # 通过 Qt 事件循环安全销毁整个窗口树，避免依赖 Python GC 在任意时刻回收
        self.deleteLater()
