"""
全局配置
"""
import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 缓存目录（用户临时目录，避免安装/便携目录无写权限）
CACHE_DIR = os.path.join(tempfile.gettempdir(), 'RaccoonQuant', 'cache')

CACHE_TTL = 3600                # 缓存有效期（秒）
CACHE_EXPIRE_SECONDS = 3600

MAX_CONCURRENT = 30             # 并发请求数
REQUEST_TIMEOUT = 10            # 请求超时（秒）
REQUEST_DELAY = 0.5             # 请求间隔（秒）

# 股票过滤
EXCLUDE_PREFIXES = ['3', '68', '7', '8', '9']   # 排除创业板3/科创板68/北交所8/9等
EXCLUDE_KEYWORDS = ['ST', '*ST', '退市']
MARKET_CAP_THRESHOLD = 80       # 流通市值阈值（亿）
MIN_MARKET_CAP = 80

# 定时任务时间
MORNING_AUCTION_TIME = '09:25'
AFTERNOON_ANALYSIS_TIME = '14:30'
CLOSING_ANALYSIS_TIME = '16:00'

# 主题色板
COLORS = {
    'bg_dark': '#1A1A2E',
    'bg_card': '#16213E',
    'bg_header': '#0F3460',
    'accent': '#F39C12',
    'accent_hover': '#F5B041',
    'red': '#E74C3C',
    'green': '#2ECC71',
    'white': '#FFFFFF',
    'gray': '#AAAAAA',
    'grid': '#2A2A4A',
}

RECOMMEND_COUNT = 3             # 推荐股票数量
