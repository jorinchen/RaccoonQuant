"""
十档买卖盘及成交明细组件
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QGroupBox,
    QSplitter,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QBrush

from config import COLORS

COLOR_RED = QColor("#E74C3C")
COLOR_GREEN = QColor("#2ECC71")
COLOR_WHITE = QColor("#FFFFFF")
COLOR_DARK_BG = QColor("#1A1A2E")
COLOR_CARD_BG = QColor("#16213E")


class OrderBookWidget(QWidget):
    """十档买卖盘 + 成交明细"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buy_levels = []
        self._sell_levels = []
        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(2, 2, 2, 2)
        main_layout.setSpacing(4)

        # 标题
        title = QLabel("十档买卖盘")
        title.setStyleSheet("font-size:13px; font-weight:bold; color:#F39C12; padding:2px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title)

        # 十档盘口表格
        self.table = QTableWidget(21, 4)  # 10卖 + 分隔 + 10买
        self.table.setHorizontalHeaderLabels(['价格', '成交量', '成交量', '价格'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(400)
        self.table.setMaximumHeight(500)

        self.table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {COLOR_DARK_BG.name()};
                color: white;
                gridline-color: transparent;
                border: none;
                font-size: 11px;
            }}
            QTableWidget::item {{
                padding: 2px 4px;
                border: none;
            }}
            QHeaderView::section {{
                background-color: #16213E;
                color: #AAAAAA;
                padding: 3px;
                border: none;
                font-size: 10px;
            }}
        """)

        main_layout.addWidget(self.table)

        # 成交概况
        info_layout = QHBoxLayout()
        self.volume_label = QLabel("成交量: - 手")
        self.amount_label = QLabel("成交额: - 万")
        for lbl in [self.volume_label, self.amount_label]:
            lbl.setStyleSheet("font-size:11px; color:#AAAAAA;")
            info_layout.addWidget(lbl)
        main_layout.addLayout(info_layout)

    def update_data(self, order_book: dict):
        """更新盘口数据"""
        if not order_book:
            return

        buy_levels = order_book.get('buy_levels', [])
        sell_levels = order_book.get('sell_levels', [])

        self.table.clearContents()

        # 卖盘 (前10行) - 卖10到卖1
        for i in range(min(10, len(sell_levels))):
            row = 9 - i
            sl = sell_levels[i] if i < len(sell_levels) else {'price': 0, 'volume': 0}
            price_item = QTableWidgetItem(f"{sl['price']:.2f}")
            price_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            price_item.setForeground(QBrush(COLOR_GREEN))
            self.table.setItem(row, 0, price_item)

            vol_item1 = QTableWidgetItem(f"{sl['volume']:>6}")
            vol_item1.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            vol_item1.setForeground(QBrush(COLOR_GREEN))
            self.table.setItem(row, 1, vol_item1)

            vol_item2 = QTableWidgetItem("")
            self.table.setItem(row, 2, vol_item2)
            self.table.setItem(row, 3, QTableWidgetItem(""))

        # 分隔行 (row 10)
        sep_item = QTableWidgetItem("──────")
        sep_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        sep_item.setForeground(QBrush(QColor("#F39C12")))
        self.table.setItem(10, 0, sep_item)
        self.table.setItem(10, 1, QTableWidgetItem("──────"))
        self.table.setItem(10, 2, QTableWidgetItem("──────"))
        self.table.setItem(10, 3, QTableWidgetItem("──────"))

        # 买盘 (后10行) - 买1到买10
        for i in range(min(10, len(buy_levels))):
            row = 11 + i
            bl = buy_levels[i] if i < len(buy_levels) else {'price': 0, 'volume': 0}

            self.table.setItem(row, 0, QTableWidgetItem(""))

            vol_item1 = QTableWidgetItem("")
            self.table.setItem(row, 1, vol_item1)

            vol_item2 = QTableWidgetItem(f"{bl['volume']:>6}")
            vol_item2.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            vol_item2.setForeground(QBrush(COLOR_RED))
            self.table.setItem(row, 2, vol_item2)

            price_item = QTableWidgetItem(f"{bl['price']:.2f}")
            price_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            price_item.setForeground(QBrush(COLOR_RED))
            self.table.setItem(row, 3, price_item)

        # 更新成交概况
        trade_vol = order_book.get('trade_volume', 0)
        trade_amt = order_book.get('trade_amount', 0)
        self.volume_label.setText(f"成交量: {trade_vol:,.0f} 手")
        self.amount_label.setText(f"成交额: {trade_amt/10000:,.0f} 万")
