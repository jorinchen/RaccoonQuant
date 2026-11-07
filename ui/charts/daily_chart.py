"""
日K线图组件 - K线 + MA5/10/30/60/120 + 成交量 + MACD
"""
import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter, QPen, QColor, QFont, QBrush

COLOR_RED = QColor("#E74C3C")
COLOR_GREEN = QColor("#2ECC71")
COLOR_WHITE = QColor("#FFFFFF")
COLOR_DARK_BG = QColor("#1A1A2E")
COLOR_GOLD = QColor("#F39C12")
COLOR_GRID = QColor("#2A2A4A")
COLOR_BLUE = QColor("#3498DB")
COLOR_PURPLE = QColor("#9B59B6")


def calc_ma(data, period):
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(float('nan'))
        else:
            result.append(np.mean(data[i - period + 1:i + 1]))
    return result


def calc_ema(data, period):
    result = []
    if not data:
        return result
    multiplier = 2 / (period + 1)
    for i in range(len(data)):
        if i == 0:
            result.append(data[0])
        else:
            result.append((data[i] - result[i - 1]) * multiplier + result[i - 1])
    return result


def calc_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = calc_ema(dif, signal)
    macd_hist = [2 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, macd_hist


class DailyChart(QWidget):
    """日线K线图"""

    MA_COLORS = {
        5: QColor("#FFD700"),
        10: QColor("#FF6347"),
        30: QColor("#00BFFF"),
        60: QColor("#32CD32"),
        120: QColor("#BA55D3"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = None
        self._start_index = 0
        self._visible_count = 120
        self._is_dragging = False
        self._drag_start_x = 0
        self.setMinimumHeight(500)
        self.setMouseTracking(True)

    def update_data(self, data: list):
        self._data = data
        if data:
            self._start_index = max(0, len(data) - self._visible_count)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            p.fillRect(self.rect(), COLOR_DARK_BG)

            if not self._data:
                p.setPen(QColor("#AAAAAA"))
                p.setFont(QFont("Microsoft YaHei", 12))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无日K线数据")
                return

            klines = self._data
            end_idx = min(max(0, self._start_index + self._visible_count), len(klines))
            si = max(0, min(self._start_index, len(klines) - 1))
            visible_klines = klines[si:end_idx]

            if not visible_klines:
                return

            closes = [k['close'] for k in visible_klines]
            highs = [k['high'] for k in visible_klines]
            lows = [k['low'] for k in visible_klines]
            opens = [k['open'] for k in visible_klines]
            volumes = [k['volume'] for k in visible_klines]

            # 计算MA
            full_closes = [k['close'] for k in klines]
            ma_data = {}
            for period in [5, 10, 30, 60, 120]:
                ma = calc_ma(full_closes, period)
                ma_data[period] = ma[si:end_idx]

            # 计算MACD
            dif, dea, macd_hist = calc_macd(full_closes)
            dif = dif[si:end_idx]
            dea = dea[si:end_idx]
            macd_hist = macd_hist[si:end_idx]

            w = self.width()
            h = self.height()

            # 布局：K线图占70%，成交量15%，MACD15%
            kline_bottom = h * 0.68
            vol_bottom = h * 0.83
            macd_bottom = h - 5

            margin = {'l': 65, 'r': 15, 't': 25, 'b': 5}

            # === K线图区域 ===
            kline_h = kline_bottom - margin['t']
            kline_w = w - margin['l'] - margin['r']

            all_range_prices = highs + lows
            if all_range_prices:
                # 加入MA数据到范围
                for period, ma in ma_data.items():
                    valid_ma = [x for x in ma if not np.isnan(x)]
                    if valid_ma:
                        all_range_prices.extend(valid_ma)

                max_p = max(all_range_prices)
                min_p = min(all_range_prices)
                price_range = max_p - min_p
                if price_range == 0:
                    price_range = max_p * 0.02 or 1
                max_p += price_range * 0.05
                min_p -= price_range * 0.05
                price_range = max_p - min_p

                # 网格
                p.setPen(QPen(COLOR_GRID, 1, Qt.PenStyle.DotLine))
                for i in range(5):
                    y = margin['t'] + kline_h * i / 4
                    p.drawLine(int(margin['l']), int(y), int(w - margin['r']), int(y))

                # 画K线
                candle_w = max(2, kline_w / len(visible_klines) * 0.7)
                for i in range(len(visible_klines)):
                    x = margin['l'] + kline_w * (i + 0.5) / len(visible_klines)
                    o = opens[i]
                    c = closes[i]
                    hi = highs[i]
                    lo = lows[i]

                    color = COLOR_RED if c >= o else COLOR_GREEN

                    # 影线
                    p.setPen(QPen(color, 1))
                    p.drawLine(int(x), int(margin['t'] + kline_h * (1 - (hi - min_p) / price_range)),
                               int(x), int(margin['t'] + kline_h * (1 - (lo - min_p) / price_range)))

                    # 实体
                    if abs(c - o) < 0.001:
                        p.drawLine(int(x - candle_w / 2), int(margin['t'] + kline_h * (1 - (c - min_p) / price_range)),
                                   int(x + candle_w / 2), int(margin['t'] + kline_h * (1 - (c - min_p) / price_range)))
                    else:
                        top = max(o, c)
                        bot = min(o, c)
                        p.fillRect(QRectF(x - candle_w / 2, margin['t'] + kline_h * (1 - (top - min_p) / price_range),
                                          candle_w, kline_h * (top - bot) / price_range), color)

                # 画MA线
                for period in [5, 10, 30, 60, 120]:
                    ma = ma_data[period]
                    color = self.MA_COLORS.get(period, COLOR_WHITE)
                    pen_style = Qt.PenStyle.SolidLine
                    pen_width = 1.5 if period <= 10 else 1
                    p.setPen(QPen(color, pen_width, pen_style))

                    last_x, last_y = None, None
                    for i in range(len(ma)):
                        if np.isnan(ma[i]):
                            last_x, last_y = None, None
                            continue
                        x = margin['l'] + kline_w * (i + 0.5) / len(ma)
                        y = margin['t'] + kline_h * (1 - (ma[i] - min_p) / price_range)
                        if last_x is not None:
                            p.drawLine(int(last_x), int(last_y), int(x), int(y))
                        last_x, last_y = x, y

                # 价格标签
                p.setFont(QFont("Microsoft YaHei", 8))
                for i in range(5):
                    price = max_p - price_range * i / 4
                    y = margin['t'] + kline_h * i / 4
                    p.setPen(QColor("#AAAAAA"))
                    p.drawText(2, int(y + 4), f"{price:.2f}")

                # MA图例
                p.setFont(QFont("Microsoft YaHei", 9))
                legend_x = int(margin['l'] + 5)
                legend_y = int(margin['t'] + 5)
                for period in [5, 10, 30, 60, 120]:
                    color = self.MA_COLORS.get(period, COLOR_WHITE)
                    p.setPen(color)
                    p.drawText(legend_x, legend_y, f"MA{period}")
                    legend_x += 55

            # === 成交量区域 ===
            vol_top = kline_bottom
            vol_h = vol_bottom - vol_top

            if volumes:
                max_vol = max(volumes) if volumes else 1
                p.setPen(QPen(COLOR_GRID, 1, Qt.PenStyle.DotLine))
                for i in range(3):
                    y = vol_top + vol_h * i / 2
                    p.drawLine(int(margin['l']), int(y), int(w - margin['r']), int(y))

                bar_w = max(1, kline_w / len(volumes) * 0.7)
                for i in range(len(volumes)):
                    x = margin['l'] + kline_w * (i + 0.5) / len(volumes)
                    vol_h_bar = (volumes[i] / max_vol) * vol_h if max_vol > 0 else 0
                    color = COLOR_RED if closes[i] >= opens[i] else COLOR_GREEN
                    p.fillRect(QRectF(x - bar_w / 2, vol_top + vol_h - vol_h_bar, bar_w, vol_h_bar), color)

                p.setPen(QColor("#AAAAAA"))
                p.setFont(QFont("Microsoft YaHei", 8))
                p.drawText(2, int(vol_top + 10), f"{max_vol/100:,.0f}手")

            # === MACD区域 ===
            macd_top = vol_bottom
            macd_h = macd_bottom - macd_top

            if dif and dea and macd_hist:
                all_macd = [x for x in dif + dea + macd_hist if not np.isnan(x)]
                if all_macd:
                    max_m = max(all_macd)
                    min_m = min(all_macd)
                    m_range = max(abs(max_m), abs(min_m))
                    if m_range == 0:
                        m_range = 0.1
                    max_m = m_range
                    min_m = -m_range

                    # 零轴
                    zero_y = macd_top + macd_h / 2
                    p.setPen(QPen(QColor("#555555"), 0.5))
                    p.drawLine(int(margin['l']), int(zero_y), int(w - margin['r']), int(zero_y))

                    # MACD柱
                    hist_bar_w = max(1, kline_w / len(macd_hist) * 0.7)
                    for i in range(len(macd_hist)):
                        if np.isnan(macd_hist[i]):
                            continue
                        x = margin['l'] + kline_w * (i + 0.5) / len(macd_hist)
                        val = macd_hist[i]
                        bar_h = abs(val) / (2 * m_range) * macd_h
                        if val >= 0:
                            p.fillRect(QRectF(x - hist_bar_w / 2, zero_y - bar_h, hist_bar_w, bar_h), COLOR_RED)
                        else:
                            p.fillRect(QRectF(x - hist_bar_w / 2, zero_y, hist_bar_w, bar_h), COLOR_GREEN)

                    # DIF线
                    p.setPen(QPen(QColor("#FFFFFF"), 1))
                    last_x, last_y = None, None
                    for i in range(len(dif)):
                        if np.isnan(dif[i]):
                            continue
                        x = margin['l'] + kline_w * (i + 0.5) / len(dif)
                        y = macd_top + macd_h * (1 - (dif[i] - min_m) / (2 * m_range))
                        if last_x is not None:
                            p.drawLine(int(last_x), int(last_y), int(x), int(y))
                        last_x, last_y = x, y

                    # DEA线
                    p.setPen(QPen(QColor("#F39C12"), 1))
                    last_x, last_y = None, None
                    for i in range(len(dea)):
                        if np.isnan(dea[i]):
                            continue
                        x = margin['l'] + kline_w * (i + 0.5) / len(dea)
                        y = macd_top + macd_h * (1 - (dea[i] - min_m) / (2 * m_range))
                        if last_x is not None:
                            p.drawLine(int(last_x), int(last_y), int(x), int(y))
                        last_x, last_y = x, y

                    # MACD图例
                    p.setFont(QFont("Microsoft YaHei", 8))
                    p.setPen(QColor("#FFFFFF"))
                    p.drawText(int(margin['l'] + 5), int(macd_top + 12), "DIF")
                    p.setPen(COLOR_GOLD)
                    p.drawText(int(margin['l'] + 35), int(macd_top + 12), "DEA")
                    p.setPen(QColor("#AAAAAA"))
                    p.drawText(int(margin['l'] + 65), int(macd_top + 12), "MACD")

            # 日期标签
            p.setPen(QColor("#AAAAAA"))
            p.setFont(QFont("Microsoft YaHei", 8))
            step = max(1, len(visible_klines) // 6)
            for i in range(0, len(visible_klines), step):
                x = margin['l'] + kline_w * (i + 0.5) / len(visible_klines)
                date_str = visible_klines[i].get('date', '')
                if len(date_str) >= 8:
                    date_str = date_str[4:8]
                    p.drawText(int(x - 15), int(h - 2), date_str)
        except Exception:
            import traceback; traceback.print_exc()
            p.setPen(COLOR_RED); p.setFont(QFont("Microsoft YaHei", 10))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "图表渲染异常，请刷新")
        finally:
            p.end()

    def wheelEvent(self, event):
        """滚轮缩放"""
        delta = event.angleDelta().y()
        if delta > 0:
            self._visible_count = max(30, self._visible_count - 20)
        else:
            self._visible_count = min(len(self._data) if self._data else 300, self._visible_count + 20)
        if self._data:
            self._start_index = max(0, min(self._start_index, max(0, len(self._data) - self._visible_count)))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = True
            self._drag_start_x = event.position().x()

    def mouseMoveEvent(self, event):
        if self._is_dragging and self._data:
            dx = event.position().x() - self._drag_start_x
            self._drag_start_x = event.position().x()
            new_idx = self._start_index + int(-dx / 5)
            self._start_index = max(0, min(new_idx, max(0, len(self._data) - self._visible_count)))
            self.update()

    def mouseReleaseEvent(self, event):
        self._is_dragging = False
