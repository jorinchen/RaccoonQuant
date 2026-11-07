"""
可复用股票数据表格组件
"""
from PyQt6.QtWidgets import (
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton,
    QStyledItemDelegate, QStyle,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QColor, QBrush, QPalette

from config import COLORS
TABLE_ROW_HEIGHT = 52


COLOR_RED = QColor("#E74C3C")
COLOR_GREEN = QColor("#2ECC71")
COLOR_WHITE = QColor("#FFFFFF")
COLOR_GRAY = QColor("#AAAAAA")
COLOR_DARK_BG = QColor("#1A1A2E")
COLOR_CARD_BG = QColor("#16213E")
COLOR_SELECTION_BG = QColor("#1F3A5F")
COLOR_HEADER = QColor("#0F3460")


class KeepForegroundDelegate(QStyledItemDelegate):
    """选中行只改变背景色，不改变文字颜色；支持 \\n 多行文本"""

    def paint(self, painter, option, index):
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text and '\n' in str(text):
            painter.save()
            # 背景色：选中 > 交替行 > 默认
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(option.rect, COLOR_SELECTION_BG)
            elif index.row() % 2 == 1:
                painter.fillRect(option.rect, QColor("#11182B"))
            else:
                painter.fillRect(option.rect, COLOR_DARK_BG)

            # 文字颜色与字体
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            if fg is not None:
                painter.setPen(fg.color())
            else:
                painter.setPen(COLOR_WHITE)

            font = index.data(Qt.ItemDataRole.FontRole)
            if font is not None:
                painter.setFont(font)

            lines = str(text).split('\n')
            line_h = option.rect.height() / max(len(lines), 1)
            for i, line in enumerate(lines):
                r = option.rect
                r.setTop(int(r.top() + i * line_h))
                r.setHeight(int(line_h))
                painter.drawText(r, Qt.AlignmentFlag.AlignCenter, line.strip())
            painter.restore()
        else:
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(option.rect, COLOR_SELECTION_BG)
                option.state &= ~QStyle.StateFlag.State_Selected
            super().paint(painter, option, index)


COLOR_GOLD = QColor("#F39C12")


def format_amount(value, unit='元'):
    """格式化金额"""
    if value is None or value == 0:
        return '-'
    v = abs(value)
    sign = '-' if value < 0 else ''
    if v >= 100000000:  # 亿
        return f"{sign}{v/100000000:.2f}亿"
    elif v >= 10000:  # 万
        return f"{sign}{v/10000:.0f}万"
    else:
        return f"{sign}{v:.0f}"


def format_pct(value):
    """格式化百分比"""
    if value is None:
        return '-'
    return f"{value:+.2f}%"


def get_color_for_change(value):
    """根据涨跌返回颜色，自动处理字符串/None"""
    if value is None:
        return COLOR_WHITE
    if isinstance(value, str):
        return COLOR_WHITE
    try:
        v = float(value)
        if v > 0:
            return COLOR_RED
        elif v < 0:
            return COLOR_GREEN
    except (ValueError, TypeError):
        pass
    return COLOR_WHITE


class StockTableWidget(QTableWidget):
    """股票数据表格"""
    stock_clicked = pyqtSignal(str, str)  # code, name

    def __init__(self, parent=None, headers=None):
        super().__init__(parent)
        self._headers = headers or []
        self._data = []
        self._init_ui()

    def _init_ui(self):
        self.setColumnCount(len(self._headers))
        self.setHorizontalHeaderLabels(self._headers)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(True)
        self.setItemDelegate(KeepForegroundDelegate(self))

        # 样式
        self.setStyleSheet(f"""
            QTableWidget {{
                background-color: {COLOR_DARK_BG.name()};
                color: {COLOR_WHITE.name()};
                gridline-color: #2A2A4A;
                border: 1px solid #2A2A4A;
                border-radius: 4px;
                font-size: 13px;
                selection-background-color: transparent;  /* delegate 控制背景色 */
            }}
            QTableWidget::item {{
                padding: 6px 10px;
                border-bottom: 1px solid #2A2A4A;
            }}
            QTableWidget::item:hover {{
                background-color: #1A2F4A;
            }}
            QHeaderView::section {{
                background-color: {COLOR_HEADER.name()};
                color: {COLOR_WHITE.name()};
                padding: 8px 6px;
                border: 1px solid #2A2A4A;
                font-weight: bold;
                font-size: 12px;
            }}
            QTableWidget::item:alternate {{
                background-color: #11182B;
            }}
        """)

        # Header
        header = self.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStretchLastSection(True)
        for i in range(len(self._headers)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

        # 板块概念列限制最大宽度，防止长文本挤压后续列
        for i, h in enumerate(self._headers):
            if h == '板块概念':
                header.setMaximumSectionSize(130)
                header.resizeSection(i, 100)
            elif h == '买卖区间':
                header.setMaximumSectionSize(160)
                header.resizeSection(i, 120)
            elif h == '推荐缘由':
                header.setMaximumSectionSize(200)
                header.resizeSection(i, 140)

        self.setMinimumHeight(200)
        self.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)

        self.cellDoubleClicked.connect(self._on_cell_double_clicked)

    def _on_cell_double_clicked(self, row, col):
        """双击单元格 -> 发出 stock_clicked 信号"""
        try:
            if row >= 0 and row < len(self._data):
                stock = self._data[row]
                self.stock_clicked.emit(stock.get('code', ''), stock.get('name', ''))
        except Exception:
            import traceback; traceback.print_exc()

    def set_data(self, data: list, header_map: dict = None):
        """设置表格数据"""
        self._data = data
        self.setRowCount(len(data))

        for row_idx, stock in enumerate(data):
            for col_idx, header in enumerate(self._headers):
                if header_map and header in header_map:
                    key = header_map[header]
                    value = stock.get(key, '')
                else:
                    value = stock.get(header, '')

                display_text = self._format_cell(header, value)
                item = QTableWidgetItem(display_text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                # 颜色处理：先将字符串转为数值，再取颜色
                # 正值红色，负值绿色
                numeric_for_color = value
                if header in ['竞价涨幅', '实时涨幅', '涨跌幅', 'change_pct', 'auction_change',
                              'real_time_change']:
                    if isinstance(value, str) and value != '' and value != '-':
                        try:
                            numeric_for_color = float(value.replace('+', '').replace('%', ''))
                        except ValueError:
                            numeric_for_color = None
                    item.setForeground(QBrush(get_color_for_change(numeric_for_color)))
                elif header in ['主力净额', '5日主力净额', '10日主力净额', '20日主力净额',
                                'main_net', 'main_net_5d', 'main_net_10d', 'main_net_20d']:
                    if isinstance(value, str) and value != '' and value != '-':
                        try:
                            numeric_for_color = float(value)
                        except ValueError:
                            numeric_for_color = None
                    item.setForeground(QBrush(get_color_for_change(numeric_for_color)))
                elif header in ['5日涨幅', '10日涨幅', '20日涨幅',
                                'change_5d', 'change_10d', 'change_20d']:
                    if isinstance(value, str) and value != '' and value != '-':
                        try:
                            numeric_for_color = float(value.replace('+', '').replace('%', ''))
                        except ValueError:
                            numeric_for_color = None
                    item.setForeground(QBrush(get_color_for_change(numeric_for_color)))
                elif header in ['昨日收盘价', '现价', 'yesterday_close', 'current']:
                    if isinstance(value, str) and value != '' and value != '-':
                        try:
                            numeric_for_color = float(value)
                        except ValueError:
                            numeric_for_color = None
                    item.setForeground(QBrush(get_color_for_change(numeric_for_color)))
                elif header in ['买卖区间', 'price_range']:
                    item.setForeground(QBrush(QColor("#E74C3C")))

                # 推荐缘由、板块概念、买卖区间列设置 tooltip 显示完整文字
                if header in ['推荐缘由', '板块概念', '买卖区间'] and value:
                    item.setToolTip(str(value))

                # 代码和名称列加粗
                if header in ['代码', '名称', 'code', 'name']:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)

                self.setItem(row_idx, col_idx, item)

    def _format_cell(self, header: str, value) -> str:
        """格式化单元格显示"""
        if value is None or value == '':
            return '-'

        if isinstance(value, float):
            if header in ['竞价涨幅', '实时涨幅', 'auction_change', 'real_time_change', 'change_pct']:
                return f"{value:+.2f}%"
            elif header in ['5日涨幅', '10日涨幅', '20日涨幅',
                            'change_5d', 'change_10d', 'change_20d']:
                return f"{value:+.2f}%"
            elif header in ['换手率', 'turnover_rate']:
                return f"{value:.2f}%"
            elif header in ['动态市盈', 'pe']:
                return f"{value:.1f}"
            elif header in ['量比', 'volume_ratio']:
                return f"{value:.2f}"
            elif header in ['成交额', 'amount']:
                return format_amount(value)
            elif header in ['主力净额', '5日主力净额', '10日主力净额', '20日主力净额',
                            'main_net', 'main_net_5d', 'main_net_10d', 'main_net_20d']:
                return format_amount(value)
            elif header in ['市值', 'market_cap']:
                return f"{value:.0f}亿" if value >= 1 else '-'
            elif header in ['昨日收盘价', '现价', 'yesterday_close', 'current']:
                return f"{value:.2f}"
            else:
                return f"{value:.2f}"

        if isinstance(value, str):
            # 板块概念过长时截断，完整内容由 tooltip 展示
            if header == '板块概念' and len(value) > 12:
                return value[:12] + '...'
            return value

        return str(value)
