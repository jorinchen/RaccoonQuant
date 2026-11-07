"""
量化筛选模块 - 评分模型 + 推荐缘由生成
数据源：腾讯 + 东方财富
"""
from typing import List, Dict
from datetime import datetime

import numpy as np


def _fmt_main_net(value):
    """格式化主力净额：超过亿以亿作单位，否则以万作单位"""
    if value is None:
        return '-'
    av = abs(value)
    sign = '-' if value < 0 else ''
    if av >= 100000000:  # >= 1亿
        return f"{sign}{av / 100000000:.2f}亿"
    else:
        return f"{sign}{av / 10000:.0f}万"


# 科技股板块关键词（名称/概念中包含以下关键词视为科技股）
TECH_KEYWORDS = [
    '科技', '电子', '半导体', '芯片', '软件', '计算机', '通信',
    '人工智能', '互联网', '云计算', '大数据', '物联网',
    '机器人', '自动化', '智能', '光电', '信息', '数据', '网络',
    '航天', '精密', '数控', '激光', '触控', '传感器', '集成电路',
    '微电子', '射频', '显示', '面板', '光学', '安防', '导航',
    '卫星', '雷达', '元器件', '仪器', '设备', '控制',
    '移动', '互联', '区块链', '量子', '虚拟现实', '增强现实',
    '无人机', '新能源', '电池', '锂电', '光伏', '储能',
    '创新',
    '5G', 'AI',
    # 短词精准匹配（避免过于宽泛）
    '芯', '讯飞', '歌尔', '韦尔',
]


def _calc_ma(closes, period):
    if len(closes) < period: return float('nan')
    return np.mean(closes[-period:])


def _calc_macd(closes):
    if len(closes) < 26: return float('nan'), float('nan'), float('nan')
    ema_fast = closes[0]
    ema_slow = closes[0]
    ema_list = [closes[0]]
    mf = 2 / 13; ms = 2 / 27
    for c in closes[1:]:
        ema_fast = (c - ema_fast) * mf + ema_fast
        ema_slow = (c - ema_slow) * ms + ema_slow
        ema_list.append((c - ema_list[-1]) * 2 / (9 + 1) + ema_list[-1] if len(ema_list) > 0 else c)
    dif = ema_fast - ema_slow
    # 简化DEA
    return dif, dif * 0.8, 2 * (dif - dif * 0.8)


def _tech_score(klines: list) -> float:
    """技术面评分 0-100"""
    if not klines or len(klines) < 30: return 50
    closes = [k['close'] for k in klines]
    score = 50

    # 均线趋势
    ma5 = _calc_ma(closes, 5); ma10 = _calc_ma(closes, 10)
    ma30 = _calc_ma(closes, 30); ma60 = _calc_ma(closes, 60)
    current = closes[-1]

    if not np.isnan(ma5) and not np.isnan(ma10):
        if ma5 > ma10: score += 8
        else: score -= 5
    if not np.isnan(ma10) and not np.isnan(ma30):
        if ma10 > ma30: score += 7
        else: score -= 5
    if not np.isnan(ma30) and not np.isnan(ma60):
        if ma30 > ma60: score += 5

    if not np.isnan(ma5) and current > ma5: score += 5
    if not np.isnan(ma10) and current > ma10: score += 5
    if not np.isnan(ma30) and current > ma30: score += 5

    # 成交量趋势
    volumes = [k['volume'] for k in klines]
    if len(volumes) >= 10:
        vol_5 = np.mean(volumes[-5:]); vol_10 = np.mean(volumes[-10:-5])
        if vol_5 > vol_10 * 1.2: score += 8
        elif vol_5 > vol_10: score += 3

    # MACD
    if len(closes) >= 26:
        dif, dea, hist = _calc_macd(closes)
        if hist > 0: score += 5
        else: score -= 5

    # 近期涨幅
    if len(closes) >= 6:
        chg_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
        if 0 < chg_5d < 15: score += 5
        elif chg_5d > 15: score += 2

    return max(0, min(100, score))


def _fund_score(flow: dict) -> float:
    """资金面评分 0-100"""
    if not flow: return 50
    score = 50
    main = flow.get('main', 0)
    if main > 0: score += min(30, main / 10000000 * 10)
    else: score -= min(20, abs(main) / 10000000 * 10)

    for period, weight in [('main_net_5d', 8), ('main_net_10d', 5), ('main_net_20d', 3)]:
        v = flow.get(period, 0)
        if v > 0: score += weight
        else: score -= weight // 2
    return max(0, min(100, score))


def _generate_reason(stock: dict, klines: list, flow: dict, changes: dict, is_morning: bool = False) -> str:
    """生成推荐缘由"""
    reasons = []
    code = stock.get('code', ''); name = stock.get('name', '')

    # 竞价涨幅
    ac = stock.get('auction_change', stock.get('change_pct', 0))
    if is_morning and ac > 0:
        if ac >= 9: reasons.append("竞价涨停")
        elif ac >= 5: reasons.append(f"竞价高开{ac:.1f}%")
        elif ac >= 2: reasons.append(f"竞价强势{ac:.1f}%")

    # 实时涨幅
    rtc = stock.get('real_time_change', stock.get('change_pct', 0))
    if rtc > 0: reasons.append(f"当日涨{rtc:.1f}%")

    # 量比
    vr = stock.get('volume_ratio', 0)
    if vr > 2: reasons.append(f"放量{vr:.1f}倍")
    elif vr > 1: reasons.append("温和放量")

    # 资金面
    if flow:
        mn = flow.get('main', 0)
        if mn > 50000000: reasons.append("主力大幅流入")
        elif mn > 10000000: reasons.append("主力净流入")
        elif mn > 0: reasons.append("主力小幅流入")

        for p, label in [('main_net_5d', '5日'), ('main_net_10d', '10日')]:
            v = flow.get(p, 0)
            if v > 0: reasons.append(f"{label}主力净流入")

    # 技术面
    if klines and len(klines) >= 30:
        closes = [k['close'] for k in klines]
        ma5 = _calc_ma(closes, 5); ma10 = _calc_ma(closes, 10)
        ma30 = _calc_ma(closes, 30)
        current = closes[-1]
        if not np.isnan(ma5) and current > ma5: reasons.append("站上5日线")
        if not np.isnan(ma10) and current > ma10: reasons.append("站上10日线")
        if not np.isnan(ma30) and current > ma30: reasons.append("站上30日线")
        if not np.isnan(ma5) and not np.isnan(ma10) and ma5 > ma10: reasons.append("均线多头")

    # 涨跌幅
    if changes:
        for p, label in [('change_5d', '5日'), ('change_10d', '10日')]:
            v = changes.get(p, 0)
            if v > 0: reasons.append(f"{label}涨{v:.1f}%")

    # 市值
    mc = stock.get('market_cap', 0)
    if mc >= 500: reasons.append("大盘股")
    elif mc >= 200: reasons.append("中盘股")
    elif mc >= 80: reasons.append("中市值")

    if not reasons: reasons.append("量化筛选")

    return "，".join(reasons[:5])


def analyze_morning_auction(candidates: List[Dict], klines: Dict, flow_data: Dict, changes: Dict) -> List[Dict]:
    """早盘竞价分析 - 推荐3支涨停潜力股"""
    results = []
    for stock in candidates:
        code = stock['code']
        kline = klines.get(code, [])
        flow = flow_data.get(code, {})
        chg = changes.get(code, {})

        tech = _tech_score(kline)
        fund = _fund_score(flow)
        ac = stock.get('auction_change', stock.get('change_pct', 0))
        ac_score = min(30, ac * 3) if ac > 0 else 0
        vr = stock.get('volume_ratio', 0)
        vr_score = min(10, vr * 3) if vr > 0 else 0
        score = tech * 0.25 + fund * 0.25 + ac_score + vr_score

        reason = _generate_reason(stock, kline, flow, chg, is_morning=True)
        results.append({
            'code': code, 'name': stock.get('name', ''),
            'auction_change': f"{ac:+.2f}%" if ac else '-',
            'real_time_change': f"{stock.get('change_pct', 0):+.2f}%",
            'yesterday_close': f"{stock.get('yesterday_close', 0):.2f}",
            'current': f"{stock.get('current', 0):.2f}",
            'turnover_rate': f"{stock.get('turnover_rate', 0):.2f}%",
            'amount': f"{stock.get('amount', 0) / 100000000:.2f}亿",
            'volume_ratio': f"{vr:.2f}" if vr else '-',
            'market_cap': stock.get('market_cap', 0),
            'main_net': _fmt_main_net(flow.get('main', 0)) if flow.get('data_available') else '-',
            'change_5d': f"{chg.get('change_5d', 0):+.2f}%" if chg else '-',
            'change_10d': f"{chg.get('change_10d', 0):+.2f}%" if chg else '-',
            'change_20d': f"{chg.get('change_20d', 0):+.2f}%" if chg else '-',
            'pe': f"{stock.get('pe', 0):.1f}" if stock.get('pe', 0) > 0 else '-',
            'reason': reason, 'score': score,
            '_code': code, '_name': stock.get('name', ''),
        })
    return results


def analyze_first_limit_up(candidates: List[Dict], klines: Dict,
                           concepts: Dict = None,
                           hot_codes=None, hot_keywords=None,
                           news_text: str = None) -> List[Dict]:
    """
    收盘首板涨停：寻找热门行情下"今日首次涨停"的科技股。
    硬性条件：
    1. 主板股票（排除 ST 股、创业板 300/301、科创板 688/689）
    2. 市值 > 100 亿
    3. 股价 > 5 元
    4. 科技股（名称/概念匹配）
    5. 今日首次涨停（前 20 个交易日内未出现过涨停）
    返回最多10支推荐
    """
    results = []
    hot_codes = hot_codes or set()
    hot_keywords = hot_keywords or []
    # 今日日期：用于判定 K 线最后一根是否已是当天（决定涨幅取值口径）
    today_str = datetime.now().strftime('%Y-%m-%d')

    for stock in candidates:
        code = stock['code']
        name = stock.get('name', '')

        # 主板 / 非 ST 过滤
        if not _is_a_share_eligible(stock):
            continue

        price = stock.get('current', 0)
        mc = stock.get('market_cap', 0)
        if price <= 5 or mc <= 100:
            continue

        # 科技股判定
        con_list = concepts.get(code, []) if concepts else []
        if not _is_tech_stock(name, con_list):
            continue

        # K线：首板涨停
        # 盘中 K 线可能不含当天 bar，此时用实时行情涨幅(change_pct)判定今日涨停，
        # 保证"开盘之后"与"收盘后"都能出结果
        kline = klines.get(code, [])
        rt_pct = stock.get('change_pct', 0)
        if not _is_first_limit_up(kline, today_str, rt_pct):
            continue

        # 今日涨幅（口径与上面首板判定保持一致）
        if kline and str(kline[-1].get('date') or '')[:10] == today_str:
            prev_close = kline[-2]['close']
            today_pct = ((kline[-1]['close'] - prev_close) / prev_close * 100
                         if prev_close > 0 else 0)
        else:
            prev_close = (stock.get('yesterday_close', 0)
                          or (kline[-1]['close'] if kline else 0))
            today_pct = rt_pct

        # 热点命中（加分，不命中不淘汰）
        hot_hit, hot_desc = _hot_hit(code, name, con_list,
                                     hot_codes, hot_keywords, news_text)

        # 评分：基础 50 + 封板强度 + 热点 + 量能
        score = 50 + min(20, (today_pct - 9.8) * 12)  # 封板越死分越高
        if hot_hit:
            score += 10
        # 消息情绪加分
        senti_bonus, senti_desc = _sentiment_bonus(name, news_text)
        score += senti_bonus

        # 近 5 日放量（涨停首板往往伴随放量）
        volumes = [k.get('volume', 0) for k in kline[-5:]]
        if len(volumes) >= 5 and sum(volumes[:-1]) > 0:
            prev_avg = sum(volumes[:-1]) / 4
            if volumes[-1] > prev_avg * 1.5:
                score += min(8, (volumes[-1] / prev_avg - 1.5) * 6)

        reason = _gen_first_limit_reason(stock, kline, con_list,
                                         today_pct, hot_desc, senti_desc)

        results.append({
            'code': code, 'name': name,
            'current': f"{price:.2f}",
            'yesterday_close': f"{prev_close:.2f}",
            'change_pct': f"+{today_pct:.2f}%",
            'market_cap': f"{mc:.1f}亿",
            'concept': '、'.join(con_list[:3]) if con_list else _get_name_concept(name),
            'reason': reason,
            'score': score,
            '_code': code, '_name': name,
        })

    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return results[:10]


def _gen_first_limit_reason(stock: dict, kline: list, con_list: list,
                            today_pct: float, hot_desc: str = '',
                            senti_desc: str = '') -> str:
    """生成首板涨停推荐缘由"""
    reasons = []
    reasons.append(f"今日首板涨停 +{today_pct:.2f}%")

    if hot_desc:
        reasons.append(hot_desc)
    if senti_desc:
        reasons.append(senti_desc)

    if con_list:
        tech_concepts = [c for c in con_list if any(kw in c for kw in
                         ['科技', '半导体', '芯片', '软件', 'AI', '人工智能',
                          '机器人', '云计算', '大数据', '物联网', '新能源', '锂电',
                          '光伏', '电子', '通信', '智能', '电池', '储能',
                          '创新', '芯', '光电', '5G'])]
        if tech_concepts:
            reasons.append(f"概念: {'/'.join(tech_concepts[:3])}")

    mc = stock.get('market_cap', 0)
    if mc >= 500:
        reasons.append("大盘科技龙头")
    elif mc >= 200:
        reasons.append("中盘科技股")

    # 量能
    volumes = [k.get('volume', 0) for k in kline[-5:]]
    if len(volumes) >= 5 and sum(volumes[:-1]) > 0:
        prev_avg = sum(volumes[:-1]) / 4
        if volumes[-1] > prev_avg * 2:
            reasons.append(f"近期放量 {volumes[-1] / prev_avg:.1f} 倍")

    if not reasons:
        reasons.append("首板涨停")
    return "；".join(reasons[:6])


def analyze_afternoon_stocks(candidates: List[Dict], klines: Dict, flow_data: Dict, changes: Dict) -> List[Dict]:
    """尾盘/收盘分析 - 推荐3支次日有望涨5%的股票"""
    results = []
    for stock in candidates:
        code = stock['code']
        kline = klines.get(code, [])
        flow = flow_data.get(code, {})
        chg = changes.get(code, {})

        tech = _tech_score(kline)
        fund = _fund_score(flow)
        rtc = stock.get('change_pct', 0)
        rt_score = min(15, (5 - abs(rtc - 1.5)) * 3) if 0 <= rtc <= 5 else 0
        vr = stock.get('volume_ratio', 0)
        vr_score = min(10, vr * 3) if vr > 0 else 0
        mn = flow.get('main', 0)
        mn_score = min(15, mn / 5000000) if mn > 0 else 0
        score = tech * 0.2 + fund * 0.3 + rt_score + vr_score + mn_score

        reason = _generate_reason(stock, kline, flow, chg)
        results.append({
            'code': code, 'name': stock.get('name', ''),
            'auction_change': f"{stock.get('auction_change', 0):+.2f}%",
            'real_time_change': f"{rtc:+.2f}%",
            'yesterday_close': f"{stock.get('yesterday_close', 0):.2f}",
            'current': f"{stock.get('current', 0):.2f}",
            'turnover_rate': f"{stock.get('turnover_rate', 0):.2f}%",
            'amount': f"{stock.get('amount', 0) / 100000000:.2f}亿",
            'volume_ratio': f"{vr:.2f}" if vr else '-',
            'market_cap': stock.get('market_cap', 0),
            'main_net': _fmt_main_net(mn) if flow.get('data_available') else '-',
            'change_5d': f"{chg.get('change_5d', 0):+.2f}%" if chg else '-',
            'change_10d': f"{chg.get('change_10d', 0):+.2f}%" if chg else '-',
            'change_20d': f"{chg.get('change_20d', 0):+.2f}%" if chg else '-',
            'pe': f"{stock.get('pe', 0):.1f}" if stock.get('pe', 0) > 0 else '-',
            'reason': reason, 'score': score,
            '_code': code, '_name': stock.get('name', ''),
        })
    return results


def search_stocks(stocks: List[Dict], keyword: str) -> List[Dict]:
    from data_fetcher import match_stock_by_pinyin
    return match_stock_by_pinyin(stocks, keyword)


def _is_tech_stock(name: str, concepts: List[str] = None) -> bool:
    """判断是否为科技股（通过名称或概念标签）"""
    search_text = name
    if concepts:
        search_text = name + '|' + '|'.join(concepts)
    for kw in TECH_KEYWORDS:
        if kw in search_text:
            return True
    return False


# =============== 主板/ST/创业板/科创板过滤 ===============
# 主板涨停 10%，创业板/科创板 20%，北交所 30%
# 首板涨停策略只允许主板参与（创业板/科创板另行分析），故阈值取 9.8%
LIMIT_UP_PCT = 9.8


def _is_st_stock(name: str) -> bool:
    """是否 ST/*ST 股（名称含 ST 风险提示标记）"""
    if not name:
        return False
    # ST / *ST / SST / S*ST 等命名都以 ST 标识风险
    n = name.upper()
    if n.startswith(('ST', '*ST', 'SST', 'S*ST')):
        return True
    # 包含 "ST" 字样（前面带字母/汉字前缀也认作风险股）
    if 'ST' in n and len(n) <= 8:
        return True
    return False


def _is_kechuang(code: str) -> bool:
    """是否科创板（688 / 689 开头）"""
    if not code:
        return False
    return code.startswith(('688', '689'))


def _is_chinext(code: str) -> bool:
    """是否创业板（300 / 301 开头）"""
    if not code:
        return False
    return code.startswith(('300', '301'))


def _is_a_share_eligible(stock: dict) -> bool:
    """是否主板可分析股票：排除 ST、创业板、科创板"""
    code = stock.get('code', '')
    name = stock.get('name', '')
    if _is_st_stock(name):
        return False
    if _is_kechuang(code) or _is_chinext(code):
        return False
    return True


def _is_first_limit_up(kline: list, today_str: str = None,
                       today_change_pct: float = None) -> bool:
    """是否"首板涨停"：今日涨停 + 前 N 个交易日内未涨停（主板阈值）。

    创业板/科创板已被上游 _is_a_share_eligible 过滤，本函数不再区分板块。
    K 线按日期升序，但最后一根**未必**是今天：盘中（开盘后）日 K 接口可能
    尚未生成当天 bar，此时最后一根是昨日。因此按两种口径判断：

    1. `kline[-1]['date'] == today_str`（收盘后 / 当天 K 线已生成）
       今日涨幅与封板特征(high≈close)全部取自 K 线，首板检查覆盖到倒数第二根。
    2. 否则（盘中 K 线滞后、当天 bar 未生成）
       今日涨幅改用实时行情 `today_change_pct`；因无当天 high，跳过封板特征；
       首板检查覆盖到 K 线最后一根（那一根是昨日及更早，同样不能有涨停）。

    这样"开盘之后"与"收盘后"两个时段都能得到正确结果。
    """
    if not kline or len(kline) < 6:
        return False
    last = kline[-1]
    last_date = str(last.get('date') or '')[:10]
    has_today = bool(today_str) and last_date == today_str

    closes = [k['close'] for k in kline]
    if has_today:
        prev_close = closes[-2]
        if prev_close <= 0:
            return False
        today_pct = (last['close'] - prev_close) / prev_close * 100
        check_end = len(kline) - 1      # 前 20 日 = [start, len-2]
    else:
        if today_change_pct is None:
            return False
        today_pct = today_change_pct
        check_end = len(kline)          # 最后一根非今天，也要纳入首板检查

    # 主板封板（>=9.8% 视为涨停；考虑涨停下浮空间放宽至 9.5%）
    if today_pct < LIMIT_UP_PCT - 0.3:
        return False
    # 封板特征：high 几乎等于 close（容差 0.2%）——仅当天 K 线存在时可判断
    if has_today and last.get('high', 0) > 0 and last['close'] < last['high'] * 0.998:
        return False

    # 前 20 个交易日内不允许再次涨停（"首板"特征）
    start = max(0, check_end - 21)
    for i in range(start, check_end):
        if i <= 0:
            continue
        pc = closes[i - 1]
        if pc > 0 and (closes[i] - pc) / pc * 100 >= LIMIT_UP_PCT - 0.3:
            return False
    return True


def _to_float(v):
    """解析 '41.98%' / '-5.3%' / '1.56' → float；空/异常 → None"""
    if v is None:
        return None
    s = str(v).strip().replace('%', '').replace(',', '')
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _fin_issue(fin: dict):
    """财务硬性检查：最新财报正增长、现金流充足、负债率不高。
    返回 None 表示通过，否则返回问题描述。"""
    if not fin:
        return '无财务数据'
    npg = _to_float(fin.get('net_profit_growth'))
    if npg is None or npg <= 0:
        return '净利润未正增长'
    debt = _to_float(fin.get('debt_ratio'))
    if debt is None or debt >= 70:
        return '负债率偏高'
    cr = _to_float(fin.get('current_ratio'))
    qr = _to_float(fin.get('quick_ratio'))
    if not ((cr is not None and cr >= 1.2) or (qr is not None and qr >= 1.0)):
        return '现金流不充足'
    return None


def _hot_hit(code, name, con_list, hot_codes, hot_keywords, news_text):
    """市场热点判定：热门板块成分股 / 概念命中 / 消息面活跃。
    返回 (是否命中, 热点描述)"""
    if hot_codes and code in hot_codes:
        return True, '市场热点'
    if news_text and name and name in news_text:
        return True, '消息面活跃'
    if hot_keywords and con_list:
        for kw in hot_keywords:
            if not kw:
                continue
            for c in con_list:
                if kw in c or c in kw:
                    return True, '热门板块'
    return False, ''


def _sentiment_bonus(name, news_text):
    """消息情绪加分：股票名称出现在电报快讯中，命中越多情绪越强"""
    if not news_text or not name:
        return 0, ''
    cnt = news_text.count(name)
    if cnt <= 0:
        return 0, ''
    return min(15, cnt * 3), f'快讯提及{cnt}次'


def _gen_tech_reason(stock: dict, klines: list, concepts: list,
                     fin: dict = None, hot_desc: str = '',
                     senti_desc: str = '', main_net_text: str = None) -> str:
    """生成科技股推荐缘由（超跌 + 热点 + 情绪 + 财务）"""
    reasons = []

    # 跌幅情况（20日超跌）
    chg_20d = stock.get('_change_15d', 0)
    if chg_20d < -25:
        reasons.append(f"近20日跌{abs(chg_20d):.1f}%，深度超跌")
    elif chg_20d < -15:
        reasons.append(f"近20日跌{abs(chg_20d):.1f}%，超跌明显")
    elif chg_20d < -10:
        reasons.append(f"近20日跌{abs(chg_20d):.1f}%")
    else:
        reasons.append(f"近20日回调{abs(chg_20d):.1f}%")

    # 市场热点 / 消息情绪
    if hot_desc:
        reasons.append(hot_desc)
    if senti_desc:
        reasons.append(senti_desc)
    if main_net_text:
        reasons.append(f"主力{main_net_text}")

    # 科技概念
    if concepts:
        tech_concepts = [c for c in concepts if any(kw in c for kw in
                         ['科技', '半导体', '芯片', '软件', 'AI', '人工智能',
                          '机器人', '云计算', '大数据', '物联网', '新能源', '锂电',
                          '光伏', '电子', '通信', '智能', '电池', '储能',
                          '创新', '芯', '光电', '5G'])]
        if tech_concepts:
            reasons.append(f"概念: {'/'.join(tech_concepts[:3])}")

    # 财务面
    if fin:
        npg = _to_float(fin.get('net_profit_growth'))
        if npg is not None and npg > 0:
            reasons.append(f"净利增长{npg:.1f}%")
        debt = _to_float(fin.get('debt_ratio'))
        if debt is not None and debt < 70:
            reasons.append(f"负债率{debt:.0f}%")
        cr = _to_float(fin.get('current_ratio'))
        qr = _to_float(fin.get('quick_ratio'))
        if (cr is not None and cr >= 1.2) or (qr is not None and qr >= 1.0):
            reasons.append("现金流充足")

    # 估值与市值
    pe = stock.get('pe', 0)
    mc = stock.get('market_cap', 0)
    if 10 <= pe <= 30:
        reasons.append(f"PE{pe:.0f}估值合理")
    if mc >= 500:
        reasons.append("大盘科技龙头")
    elif mc >= 200:
        reasons.append("中盘科技股")

    # 连续收阴天数
    if klines and len(klines) >= 20:
        down_days = sum(1 for k in klines[-20:] if k['close'] < k['open'])
        if down_days >= 14:
            reasons.append(f"近20日{down_days}天收阴，空方释放充分")

    if not reasons:
        reasons.append("超跌科技股")

    return "；".join(reasons[:6])


def analyze_daily_tech_picks(candidates: List[Dict], klines: Dict,
                              concepts: Dict = None, finances: Dict = None,
                              flows: Dict = None, hot_codes=None,
                              hot_keywords=None, news_text: str = None) -> List[Dict]:
    """
    每日推荐：超跌热点科技股发现（资金面+基本面+情绪面）
    硬性条件：
    1. 主板股票（排除 ST 股、创业板 300/301、科创板 688/689）
    2. 市值 > 100 亿
    3. 股价 5~30 元
    4. PE 5~40（市盈率不高）
    5. 科技股（名称/概念匹配）
    6. 连续 20 日超跌（20日跌幅 < -3%）
    7. 最新财报正增长 + 现金流充足 + 负债率 < 70%
    市场热点/消息情绪为加分项（命中不淘汰）
    返回最多10支推荐
    """
    results = []
    fin_data = finances or {}
    flow_data = flows or {}
    for stock in candidates:
        code = stock['code']
        name = stock.get('name', '')
        price = stock.get('current', 0)
        pe = stock.get('pe', 0)
        mc = stock.get('market_cap', 0)

        # 主板 / 非 ST 过滤：排除创业板、科创板、ST/*ST 股
        if not _is_a_share_eligible(stock):
            continue

        # 股价 5~30 元
        if price < 5 or price > 30:
            continue
        # PE 5~40（市盈率不高）
        if pe <= 0 or pe > 40:
            continue
        # 市值 > 100 亿
        if mc <= 100:
            continue

        # 科技股判定
        con_list = concepts.get(code, []) if concepts else []
        if not _is_tech_stock(name, con_list):
            continue

        # 财务硬性条件
        fin = fin_data.get(code) or {}
        if _fin_issue(fin):
            continue

        # K线分析：检测连续 20 日超跌
        kline = klines.get(code, [])
        if not kline or len(kline) < 20:
            continue

        closes = [k['close'] for k in kline]
        volumes = [k['volume'] for k in kline]
        current_close = closes[-1]

        # 20日涨跌幅
        if len(closes) >= 21:
            chg_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
        else:
            chg_20d = (closes[-1] - closes[0]) / closes[0] * 100

        # 连续 20 日超跌（20日跌幅 < -3%）
        if chg_20d > -3:
            continue

        # 市场热点判定（命中加分，不命中不淘汰）
        hot_hit, hot_desc = _hot_hit(code, name, con_list,
                                     hot_codes, hot_keywords, news_text)

        stock['_change_15d'] = chg_20d
        stock['_change_30d'] = chg_20d

        # 评分
        score = 50

        # 超跌幅度（20日）
        if chg_20d < -25:
            score += 20
        elif chg_20d < -20:
            score += 16
        elif chg_20d < -15:
            score += 12
        elif chg_20d < -10:
            score += 8
        else:
            score += 5

        # 连续收阴天数（20日窗口）
        down_days = sum(1 for k in kline[-20:] if k['close'] < k['open'])
        if down_days >= 14:
            score += 12
        elif down_days >= 10:
            score += 8
        elif down_days >= 7:
            score += 4

        # 成交量萎缩（底部特征）
        if len(volumes) >= 20:
            vol_first_half = np.mean(volumes[-20:-10])
            vol_second_half = np.mean(volumes[-10:])
            if vol_first_half > 0 and vol_second_half < vol_first_half * 0.7:
                score += 8  # 缩量下跌，可能是底部

        # PE评分（低PE更佳）
        if pe <= 20:
            score += 12  # 低估值
        elif pe <= 30:
            score += 8
        elif pe <= 40:
            score += 4

        # 市值评分
        if mc >= 1000:
            score += 8  # 巨头
        elif mc >= 500:
            score += 6  # 大盘龙头
        elif mc >= 200:
            score += 4
        else:
            score += 2

        # 均线偏离度（远离均线=超跌反弹潜力）
        if len(closes) >= 30:
            ma30 = _calc_ma(closes, 30)
            if not np.isnan(ma30) and ma30 > 0:
                deviation = (current_close - ma30) / ma30 * 100
                if deviation < -15:
                    score += 12
                elif deviation < -10:
                    score += 8
                elif deviation < -5:
                    score += 4

        # 概念丰富度加分
        if con_list:
            tech_count = sum(1 for kw in TECH_KEYWORDS
                           for c in con_list if kw in c)
            score += min(8, tech_count * 2)

        # 热点强度：属于热门板块成分股 +10
        if hot_codes and code in hot_codes:
            score += 10

        # 消息情绪加分
        senti_bonus, senti_desc = _sentiment_bonus(name, news_text)
        score += senti_bonus

        # 资金面加分（主力净流入）
        flow = flow_data.get(code) or {}
        main_net = flow.get('main', 0) or 0
        if main_net > 0:
            score += min(10, main_net / 50000000 * 5)
        main_net_text = _fmt_main_net(main_net) if flow.get('data_available') else None

        reason = _gen_tech_reason(stock, kline, con_list, fin,
                                  hot_desc, senti_desc, main_net_text)

        results.append({
            'code': code, 'name': name,
            'current': f"{price:.2f}",
            'yesterday_close': f"{stock.get('yesterday_close', 0):.2f}",
            'change_pct': f"{stock.get('change_pct', 0):+.2f}%",
            'market_cap': f"{mc:.1f}亿",
            'concept': '、'.join(con_list[:3]) if con_list else _get_name_concept(name),
            'reason': reason,
            'score': score,
            '_code': code, '_name': name,
        })

    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return results[:10]


def analyze_dividend_picks(candidates: List[Dict],
                           dividend_map: Dict[str, Dict],
                           concepts: Dict = None,
                           finances: Dict = None) -> List[Dict]:
    """
    分红推荐：明日及之后要分红的科技股（主板）。
    硬性条件：
    1. 主板股票（排除 ST、创业板、科创板）
    2. 每股分红 > 0.2 元（含税前）
    3. 股价 > 10 元
    4. 市值 > 100 亿
    5. 科技股（名称/概念匹配）
    6. 财报正增长 + 现金流充足 + 负债率 < 70%
    返回最多10支推荐
    """
    results = []
    fin_data = finances or {}

    for stock in candidates:
        code = stock['code']
        if not _is_a_share_eligible(stock):
            continue

        div = dividend_map.get(code)
        if not div:
            continue
        per_share = div.get('per_share', 0)
        # 每股分红硬性条件：每股（含税）必须 > 0.2 元，不论金额来自主源预案
        # 还是"分配方案实施"公告正文提取，一律统一过滤（金额未提取到的按 0 处理，
        # 同样不满足 > 0.2 被剔除）
        if per_share <= 0.2:
            continue

        price = stock.get('current', 0)
        mc = stock.get('market_cap', 0)
        if price <= 10 or mc <= 100:
            continue

        name = stock.get('name', '')
        con_list = concepts.get(code, []) if concepts else []
        if not _is_tech_stock(name, con_list):
            continue

        fin = fin_data.get(code) or {}
        if _fin_issue(fin):
            continue

        # 评分：基础 50 + 分红金额 + 送股 + 财务加分
        score = 50
        score += min(20, (per_share - 0.2) * 20)  # 0.2 元 +0；0.5 元 +6；1.0 元 +16
        if div.get('bonus_share', 0) > 0:
            score += 5  # 额外送股
        if 5 <= mc < 200:
            score += 4
        elif 200 <= mc < 500:
            score += 6
        elif mc >= 500:
            score += 8
        if fin:
            npg = _to_float(fin.get('net_profit_growth'))
            if npg is not None and npg > 0:
                score += min(8, npg / 25)
            debt = _to_float(fin.get('debt_ratio'))
            if debt is not None and debt < 70:
                score += 4

        results.append({
            'code': code, 'name': name,
            'current': f"{price:.2f}",
            'dividend': (f"{per_share:.2f}元/股" if per_share > 0
                         else '详见公告'),
            # 含税/扣税后：主源命中时拼成 "X.XX含税 / Y.YY扣税"；
            # 金额来自实施公告（无可靠扣税口径）时显示 '-'，避免与"含税前"列重复
            'after_tax': _format_after_tax(per_share, div.get('per_share_after_tax', 0),
                                           bool(div.get('amount_from_notice'))),
            'bonus_share': (f"{div.get('bonus_share', 0):.0f}股/10股"
                            if div.get('bonus_share', 0) > 0
                            else '-'),
            'div_date': div.get('div_date', '-'),
            'concept': '、'.join(con_list[:3]) if con_list else _get_name_concept(name),
            'score': score,
            '_code': code, '_name': name,
        })

    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return results[:10]


def _format_after_tax(per_share: float, per_share_after_tax: float,
                      is_notice: bool = False) -> str:
    """格式化为含税/扣税后字符串（QTableWidget 单行紧凑格式）。

    - per_share == 0（副源独有且公告未给出金额）返回 '-'
    - per_share_after_tax == 0（无扣税字段）：
        * is_notice=True（金额来自实施公告）返回 '-'，避免与"含税"列显示重复
        * 否则仅显示含税值
    - 都有则拼成 "X.XX/Y.YY"（前者含税，后者扣税）
    """
    if per_share <= 0:
        return '-'
    if per_share_after_tax <= 0:
        return '-' if is_notice else f"{per_share:.2f}元/股"
    return f"{per_share:.2f}/{per_share_after_tax:.2f}"


def _get_name_concept(name: str) -> str:
    """从名称推断概念标签"""
    for kw in TECH_KEYWORDS:
        if kw in name:
            return kw
    return '科技'
