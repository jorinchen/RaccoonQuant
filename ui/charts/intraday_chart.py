"""
分时图组件 - 包含集合竞价区域 + 9:30~11:30 + 13:00~15:00
"""
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter, QPen, QColor, QFont, QLinearGradient, QBrush

COLOR_RED = QColor("#E74C3C")
COLOR_GREEN = QColor("#2ECC71")
COLOR_WHITE = QColor("#FFFFFF")
COLOR_DARK_BG = QColor("#1A1A2E")
COLOR_GOLD = QColor("#F39C12")
COLOR_GRID = QColor("#2A2A4A")
COLOR_BLUE = QColor("#3498DB")


class IntradayChart(QWidget):
    """分时图"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = None
        self.setMinimumHeight(300)

    def update_data(self, data: dict):
        self._data = data
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), COLOR_DARK_BG)

        if not self._data:
            p.setPen(QColor("#AAAAAA"))
            p.setFont(QFont("Microsoft YaHei", 12))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无分时数据")
            p.end()
            return

        minute_data = self._data.get('minute_data', [])
        yesterday_close = self._data.get('yesterday_close', 0)
        current = self._data.get('current', yesterday_close)

        if not minute_data or yesterday_close == 0:
            p.setPen(QColor("#AAAAAA"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无分时数据")
            p.end()
            return

        w = self.width()
        h = self.height()
        margin = {'l': 65, 'r': 20, 't': 30, 'b': 30}
        plot_w = w - margin['l'] - margin['r']
        plot_h = h - margin['t'] - margin['b']

        # 计算价格范围
        prices = [d.get('price', yesterday_close) for d in minute_data]
        if not prices:
            p.end()
            return

        all_prices = prices + [yesterday_close]
        max_p = max(all_prices)
        min_p = min(all_prices)
        price_range = max_p - min_p
        if price_range == 0:
            price_range = max_p * 0.02 or 0.1

        # 扩展范围
        max_p = max_p + price_range * 0.1
        min_p = min_p - price_range * 0.1
        price_range = max_p - min_p

        # 均价线
        avg_prices = []
        total_vol = 0
        total_amt = 0
        for d in minute_data:
            vol = d.get('volume', 0)
            price = d.get('price', yesterday_close)
            total_vol += vol
            total_amt += vol * price
            avg_prices.append(total_amt / total_vol if total_vol > 0 else price)

        # 画网格
        p.setPen(QPen(COLOR_GRID, 1, Qt.PenStyle.DotLine))
        grid_count = 6
        for i in range(grid_count + 1):
            y = margin['t'] + plot_h * i / grid_count
            p.drawLine(int(margin['l']), int(y), int(w - margin['r']), int(y))

        # 画时间分隔线（11:30 / 13:00）
        p.setPen(QPen(QColor("#F39C12"), 1, Qt.PenStyle.DashLine))
        # 找到中午分隔点
        morning_end_idx = None
        afternoon_start_idx = None
        for i, d in enumerate(minute_data):
            time_str = d.get('time', '')
            if time_str.startswith('11:30') and morning_end_idx is None:
                morning_end_idx = i
            if time_str.startswith('13:0') and afternoon_start_idx is None:
                afternoon_start_idx = i

        if morning_end_idx is not None:
            x = margin['l'] + plot_w * morning_end_idx / len(minute_data)
            p.drawLine(int(x), int(margin['t']), int(x), int(margin['t'] + plot_h))

        # 画价格线
        if prices:
            # 价格路径
            path = None
            from PyQt6.QtGui import QPainterPath
            path = QPainterPath()
            first = True
            for i, price in enumerate(prices):
                x = margin['l'] + plot_w * i / len(prices)
                y = margin['t'] + plot_h * (1 - (price - min_p) / price_range)
                if first:
                    path.moveTo(x, y)
                    first = False
                else:
                    path.lineTo(x, y)

            # 填充区域
            fill_path = QPainterPath(path)
            fill_path.lineTo(margin['l'] + plot_w, margin['t'] + plot_h)
            fill_path.lineTo(margin['l'], margin['t'] + plot_h)
            fill_path.closeSubpath()

            change_pct = self._data.get('change_pct', 0)
            fill_color = QColor(231, 76, 60, 30) if change_pct >= 0 else QColor(46, 204, 113, 30)
            p.fillPath(fill_path, fill_color)

            # 画价格线
            line_color = COLOR_RED if change_pct >= 0 else COLOR_GREEN
            p.setPen(QPen(line_color, 1.5))
            p.drawPath(path)

        # 画均价线
        if avg_prices:
            from PyQt6.QtGui import QPainterPath
            avg_path = QPainterPath()
            first = True
            for i, avg in enumerate(avg_prices):
                x = margin['l'] + plot_w * i / len(avg_prices)
                y = margin['t'] + plot_h * (1 - (avg - min_p) / price_range)
                if first:
                    avg_path.moveTo(x, y)
                    first = False
                else:
                    avg_path.lineTo(x, y)
            p.setPen(QPen(COLOR_GOLD, 1, Qt.PenStyle.DashLine))
            p.drawPath(avg_path)

        # 昨日收盘价线
        if min_p <= yesterday_close <= max_p:
            y_close = margin['t'] + plot_h * (1 - (yesterday_close - min_p) / price_range)
            p.setPen(QPen(QColor("#AAAAAA"), 0.5, Qt.PenStyle.DashLine))
            p.drawLine(int(margin['l']), int(y_close), int(w - margin['r']), int(y_close))

        # 价格标签
        font = QFont("Microsoft YaHei", 9)
        p.setFont(font)
        p.setPen(QColor("#AAAAAA"))
        for i in range(grid_count + 1):
            price = max_p - price_range * i / grid_count
            y = margin['t'] + plot_h * i / grid_count
            p.drawText(2, int(y + 4), f"{price:.2f}")

        # 变化百分比
        change_pct = self._data.get('change_pct', 0)
        pct_text = f"{change_pct:+.2f}%"
        p.setPen(COLOR_RED if change_pct >= 0 else COLOR_GREEN)
        p.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        p.drawText(int(margin['l'] + 10), int(margin['t'] - 5), pct_text)

        # 时间标签
        p.setPen(QColor("#AAAAAA"))
        p.setFont(QFont("Microsoft YaHei", 8))
        time_labels = ['9:30', '10:30', '11:30/13:00', '14:00', '15:00']
        for i, label in enumerate(time_labels):
            x = margin['l'] + plot_w * i / 4
            p.drawText(int(x - 15), int(h - 5), label)

        # 标题
        p.setPen(COLOR_GOLD)
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        p.drawText(int(w / 2 - 40), int(margin['t'] - 8), "分时走势")

        p.end()
