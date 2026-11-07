"""
通达信 TdxL2 数据源 (mootdx/tdxpy)
- 直连 TDX 行情服务器，无需安装 TDX 客户端
- 提供：股票列表、实时行情、K线
- 所有函数为同步函数，在异步上下文中通过 asyncio.to_thread() 调用
"""
import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger('tdx')

_mootdx_available = False
_client = None
_init_done = False
_last_fail_time = 0.0

# TDX API 超时（秒）：股票列表、行情等简单查询应在 3 秒内完成
TDX_TIMEOUT = 3
# 初始化失败后的重试冷却（秒）：bestip 扫描约 10-15 秒，避免每次调用都触发
_RETRY_COOLDOWN = 30


def _ensure_config():
    """确保 mootdx config.json 存在且包含最佳 IP 列表。
    PyInstaller 打包后，mootdx 路径为只读，config.json 无法创建。
    这会导致每次 API 调用都遍历服务器列表尝试连接，每个超时 8 秒。
    这里提前触发配置生成并存入用户可写目录。
    """
    try:
        from mootdx import config
        config_dir = os.path.join(os.path.expanduser('~'), '.mootdx')
        os.makedirs(config_dir, exist_ok=True)
        conf_file = os.path.join(config_dir, 'config.json')
        # 如果配置文件已存在且未过期（24小时内），直接使用
        if os.path.exists(conf_file):
            mtime = os.path.getmtime(conf_file)
            if time.time() - mtime < 86400:
                return
        # 同步执行 bestip 扫描，找到最快的 5 个服务器
        from mootdx.server import bestip
        logger.info('正在扫描最优 TDX 服务器（约 10-15 秒）...')
        try:
            bestip(console=False, limit=5, sync=True)
        except Exception as e:
            logger.info(f'bestip 扫描跳过: {e}')
    except Exception:
        pass


def _reset_client():
    """客户端失效后重置初始化状态，允许下次调用时自动重建。"""
    global _client, _mootdx_available, _init_done
    _client = None
    _mootdx_available = False
    _init_done = False


def _init():
    global _mootdx_available, _client, _init_done, _last_fail_time
    if _init_done:
        return
    # 失败冷却：bestip 扫描约 10-15 秒，冷却期内不重试，避免反复阻塞
    if _last_fail_time and time.time() - _last_fail_time < _RETRY_COOLDOWN:
        return
    _init_done = True
    try:
        from mootdx.quotes import Quotes
        _ensure_config()
        _client = Quotes.factory(market='std', timeout=TDX_TIMEOUT, encoding='gbk')
        _mootdx_available = True
        _last_fail_time = 0.0
        logger.info(f'通达信 mootdx 模块加载成功 (timeout={TDX_TIMEOUT}s)')
    except ImportError:
        # 模块缺失属永久失败，标记已完成，避免无意义重试
        logger.warning('mootdx 未安装，通达信 API 不可用')
    except Exception as e:
        _last_fail_time = time.time()
        _init_done = False  # 失败后允许下次重建
        _client = None
        logger.warning(f'mootdx 加载失败（{_RETRY_COOLDOWN}s 后重试）: {e}')


def _get_client():
    global _client
    if _client is None:
        if _init_done:
            # 客户端曾在运行中被丢弃（API 调用失败），重置后重建
            _reset_client()
        _init()
    return _client


def is_available() -> bool:
    """检查通达信 API 是否可用。
    若客户端已失效（被丢弃），自动尝试重建——否则 TDX 一旦失败，
    整个会话都拿不到数据却仍被标记可用，搜索会永远走东财慢速接口。"""
    if _client is None:
        _get_client()
    return _mootdx_available


# ==================== 股票列表 ====================
# A股代码特征：沪市主板60xxxx，科创板688xxx，深市主板000xxx/001xxx，创业板300xxx/301xxx
# 过滤掉指数(000001/399xxx/999xxx)、债券、基金等

def _fix_tdx_name(raw: str) -> str:
    """修复 TDX 返回名称可能出现的编码问题。
    当 mootdx 与 TDX 服务端编码不一致时，名称可能包含乱码字符（如 \\x00 或 Latin-1 字符）。
    尝试检测并修复：去除空字符、尝试 latin-1 → utf-8 重解码、过滤不可打印字符。
    """
    name = raw.strip()
    if not name:
        return name
    # 去除嵌入的 NUL 字符
    if '\x00' in name:
        name = name.replace('\x00', '')
    # 检测是否全是 ASCII/Latin-1 范围内的非中文字符（可能是 GBK/UTF-8 错位）
    # 正常的 A 股名称应至少包含一个中文或 ASCII 字母
    has_chinese = any('\u4e00' <= c <= '\u9fff' for c in name)
    has_digit = any(c.isdigit() for c in name)
    has_letter = any(c.isascii() and c.isalpha() for c in name)
    if not has_chinese and not has_digit and not has_letter:
        # 可能是 latin-1 解码了 UTF-8 → 重新编码再解码
        try:
            fixed = name.encode('latin-1').decode('utf-8')
            if any('\u4e00' <= c <= '\u9fff' for c in fixed):
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        # 可能是 GBK 被当成 UTF-8 解码 → 重新编码再解码
        try:
            fixed = name.encode('utf-8').decode('gbk')
            if any('\u4e00' <= c <= '\u9fff' for c in fixed):
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    # 过滤路径不安全字符
    name = ''.join(c for c in name if c.isprintable())
    return name


def _is_a_stock(code: str) -> bool:
    if len(code) != 6 or not code.isdigit():
        return False
    if code.startswith('60'):  # 沪市主板
        return True
    if code.startswith('68'):  # 科创板
        return True
    if code.startswith('00'):  # 深市 (000xxx-004xxx)
        # 排除上证指数、深证指数（TDX 中 000001-000020 等是指数而非个股）
        sz_index_codes = set()
        for i in range(1, 100):
            sz_index_codes.add(f'{i:06d}')  # 000001-000099 都是指数
        sz_index_codes |= {f'{i:06d}' for i in range(300, 400)}      # 000300+
        sz_index_codes |= {f'{i:06d}' for i in range(800, 1000)}      # 000800+
        if code in sz_index_codes:
            return False
        # 深圳个股代码通常从 000xxx 开始，但这里排除的指数范围已经去掉
        return True
    if code.startswith('30'):  # 创业板
        return True
    return False


def get_stock_list() -> List[Dict]:
    """
    通过 TDX stocks() API 获取完整股票列表（含名称、昨收）
    返回 [{'code': '000001', 'name': '平安银行', 'pre_close': 11.27}, ...]
    """
    global _client
    if not is_available():
        return []
    t0 = time.time()
    try:
        client = _get_client()
        # mootdx stocks() 默认仅返回沪市(market=1)，深市需单独请求后合并；
        # 否则深市股票（如 000725 京东方A、300750 宁德时代）缺失，
        # 搜索本地拼音匹配失败后退化为东财慢速在线兜底
        frames = []
        for market in (1, 0):
            try:
                df = client.stocks(market=market)
                if df is not None and len(df) > 0:
                    frames.append(df)
            except Exception as e:
                logger.warning(f'TDX stocks(market={market}) 失败: {e}')
        if not frames:
            logger.info(f'TDX stocks() 返回空 ({time.time()-t0:.0f}ms)')
            return []
        seen = set()
        stocks = []
        for df in frames:
            try:
                # 向量化预过滤：仅保留 6 位数字、60/68/00/30 开头且名称非空的行，
                # 避免对 5 万+ 行（含基金/债券/指数）逐行 iterrows
                codes_s = df['code'].astype(str).str.strip()
                names_s = df['name'].astype(str).str.strip()
                pre_mask = (codes_s.str.len() == 6) & codes_s.str.isdigit() \
                    & codes_s.str.startswith(('60', '68', '00', '30')) & names_s.ne('')
                rows = df.loc[pre_mask].to_dict('records')
            except Exception:
                rows = df.to_dict('records')
            for row in rows:
                code = str(row.get('code', '')).strip() if row.get('code') is not None else ''
                if code in seen or not _is_a_stock(code):
                    continue
                seen.add(code)
                name = _fix_tdx_name(str(row.get('name', '')) if row.get('name') is not None else '')
                if not name:
                    continue
                pc = row.get('pre_close')
                pre_close = float(pc) if pc is not None and pc != '' else 0.0
                stocks.append({
                    'code': code,
                    'name': name,
                    'market_cap': 0,
                    'pe': 0,
                    'sector': '',
                    'pre_close': pre_close,
                })
        dt2 = (time.time() - t0) * 1000
        logger.info(f'TDX 股票列表: {len(stocks)} 支 A 股 ({dt2:.0f}ms)')
        return stocks
    except Exception as e:
        dt = (time.time() - t0) * 1000
        logger.warning(f'TDX 获取股票列表失败 ({dt:.0f}ms): {e}')
        _reset_client()  # 丢弃旧客户端，下次调用自动重建
    return []


# ==================== 实时行情 ====================
def get_quotes(codes: List[str]) -> Dict[str, Dict]:
    """
    获取实时行情（五档盘口、量比、换手率、市盈率）
    参数: codes = ['000001', '600519', ...] 纯数字代码
    返回: {code: {name, open, yesterday_close, current, high, low, vol, amount, change_pct, ...}}
    """
    global _client
    if not is_available() or not codes:
        return {}
    try:
        client = _get_client()
        df = client.quotes(symbol=codes)
        if df is None or len(df) == 0:
            return {}
        result = {}
        for _, row in df.iterrows():
            code = str(row.get('code', '')).strip()
            if not code or len(code) != 6:
                continue
            yc = float(row.get('last_close', 0) or 0)
            cur = float(row.get('price', 0) or 0)
            change_pct = ((cur - yc) / yc * 100) if yc > 0 else 0
            # 五档买卖
            buy_prices, buy_vols, sell_prices, sell_vols = [], [], [], []
            for i in range(1, 6):
                b = float(row.get(f'bid{i}', 0) or 0)
                a = float(row.get(f'ask{i}', 0) or 0)
                bv = float(row.get(f'bid_vol{i}', 0) or 0)
                av = float(row.get(f'ask_vol{i}', 0) or 0)
                if b > 0:
                    buy_prices.append(b)
                    buy_vols.append(bv)
                if a > 0:
                    sell_prices.append(a)
                    sell_vols.append(av)
            result[code] = {
                'name': _fix_tdx_name(str(row.get('name', ''))) if 'name' in row.index else '',
                'open': float(row.get('open', 0) or 0),
                'yesterday_close': yc,
                'current': cur,
                'high': float(row.get('high', 0) or 0),
                'low': float(row.get('low', 0) or 0),
                'volume': float(row.get('vol', 0) or 0) * 100,
                'amount': float(row.get('amount', 0) or 0),
                'change_pct': change_pct,
                'turnover_rate': float(row.get('turnover_rate', 0) or 0),
                'pe': 0,  # TDX quotes 无 PE
                'market_cap': 0,
                'volume_ratio': float(row.get('vol_ratio', 0) or 0),
                'buy_prices': buy_prices,
                'buy_volumes': buy_vols,
                'sell_prices': sell_prices,
                'sell_volumes': sell_vols,
            }
        return result
    except Exception as e:
        logger.warning(f"TDX 获取实时行情失败: {e}")
        _reset_client()  # 丢弃旧客户端，下次调用自动重建
    return {}


# ==================== K线 ====================
def get_kline(code: str, period: str = 'daily', count: int = 250) -> List[Dict]:
    """
    K线数据
    period: 'daily' | 'weekly' | 'monthly'
    返回: [{'date': '20260807', 'open': 1.0, 'close': 2.0, 'high': 3.0, 'low': 0.5, 'volume': 10000, 'amount': 20000}, ...]
    """
    global _client
    if not is_available():
        return []
    freq_map = {'daily': 9, 'weekly': 5, 'monthly': 6}
    frequency = freq_map.get(period, 9)
    try:
        client = _get_client()
        df = client.bars(symbol=code, frequency=frequency, offset=count)
        if df is None or len(df) == 0:
            return []
        import pandas as pd
        results = []
        for idx, row in df.iterrows():
            if isinstance(idx, pd.Timestamp):
                date_str = idx.strftime('%Y%m%d')
            else:
                dt = row.get('datetime', '')
                if hasattr(dt, 'strftime'):
                    date_str = dt.strftime('%Y%m%d')
                else:
                    date_str = str(dt).replace('-', '')[:8]
            results.append({
                'date': date_str,
                'open': float(row.get('open', 0) or 0),
                'close': float(row.get('close', 0) or 0),
                'high': float(row.get('high', 0) or 0),
                'low': float(row.get('low', 0) or 0),
                'volume': float(row.get('vol', 0) or 0),
                'amount': float(row.get('amount', 0) or 0),
            })
        return results
    except Exception as e:
        logger.warning(f"TDX 获取 K线失败 ({code}/{period}): {e}")
        _reset_client()  # 丢弃旧客户端，下次调用自动重建
    return []


# ==================== 财务数据 ====================
def get_finance(code: str) -> Optional[Dict]:
    """获取财务数据（市盈率、市值等）"""
    if not is_available():
        return None
    try:
        client = _get_client()
        df = client.finance(symbol=code)
        if df is None or len(df) == 0:
            return None
        row = df.iloc[-1]
        return {
            'pe': float(row.get('pe', 0) or 0),
            'market_cap': float(row.get('total_mv', 0) or 0),
            'pb': float(row.get('pb', 0) or 0),
        }
    except Exception as e:
        logger.debug(f"TDX 财务数据失败 ({code}): {e}")
        return None


# ==================== 测试 ====================
def test_connection() -> bool:
    """测试 TDX 服务器连接"""
    if not is_available():
        return False
    try:
        client = _get_client()
        df = client.quotes(symbol=['000001'])
        return df is not None and len(df) > 0
    except Exception:
        return False


# 模块加载时不初始化，延迟到首次调用（避免阻塞启动 & 避免重复 bestip 扫描）
