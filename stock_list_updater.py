"""
多源股票列表更新器

从腾讯、东方财富、同花顺、通达信、雪球等数据源拉取沪深 A 股列表，
合并去重后写入 MySQL（thk_wx_stock_list），表中不存在的股票自动添加。
"""
import asyncio
import json
import logging
import re

import app_paths
import mysql_store
from log_manager import get_logger

logger = get_logger('main')

# 腾讯行情单次最大代码数（避免 URL 过长被截断）
_QQ_BATCH = 50
# sector_concept 中概念标签数量上限（0 表示不限，与搜索栏保持一致）
_MAX_CONCEPTS = 0


def _infer_exchange(code):
    """根据 A 股代码前缀推断交易所缩写（SH/SZ/BJ，国内行情软件惯例）"""
    c = str(code or '').strip()
    if c.startswith('92'):
        return 'BJ'           # 北交所新代码段 920xxx
    if c.startswith(('6', '9')):
        return 'SH'           # 沪主板/科创板(688/689)/沪B(900)
    if c.startswith(('0', '2', '3')):
        return 'SZ'           # 深主板/创业板(300/301/302)/深B(200)
    if c.startswith(('4', '8')):
        return 'BJ'           # 北交所 4xx/8xx
    return ''


def _to_float(val, default=0.0):
    """行情/列表接口对停牌、新股等返回 '-'、'--'、''，统一安全转 float"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _norm_stock(item):
    """统一 stock dict 字段：code/name/name_initials/current_price/sector_concept"""
    code = str(item.get('code') or '').strip()
    return {
        'code': code,
        'name': str(item.get('name') or '').strip(),
        'name_initials': str(item.get('name_initials') or '').strip(),
        'current_price': _to_float(item.get('current_price')
                                   or item.get('current')),
        'sector_concept': str(item.get('sector_concept')
                              or item.get('sector')
                              or '').strip(),
        'stock_exchange': _infer_exchange(code),
    }


async def _fetch_eastmoney(fetcher):
    """东方财富全量列表（主源，含板块概念 sector）"""
    return await fetcher._fetch_stock_list_eastmoney()


async def _fetch_tdx():
    """通达信 TDX 全量列表（补充源，模块级函数，无需实例）。

    TDX 列表（代码/名称）为低频数据，每次点击"更新股票"都重新拉取会触发
    mootdx 服务器遍历连接（实测约 11 秒）。本地缓存 24 小时，命中时秒回。
    """
    try:
        import os
        import tempfile
        import time
        import data_fetcher
        cache_dir = os.path.join(tempfile.gettempdir(), 'RaccoonQuant', 'cache')
        cache_fp = os.path.join(cache_dir, 'tdx_stock_list.json')
        # 缓存命中（24h 内）
        if os.path.exists(cache_fp):
            try:
                if time.time() - os.path.getmtime(cache_fp) < 86400:
                    with open(cache_fp, 'r', encoding='utf-8') as f:
                        cached = json.load(f)
                    if cached:
                        logger.info('[updater] TDX 列表命中缓存: %d 只', len(cached))
                        return cached
            except Exception:
                pass
        # 可用性检查放线程池，避免 mootdx bestip 扫描阻塞事件循环
        available = await asyncio.to_thread(data_fetcher._tdx_available)
        if not available:
            return []
        stocks = await asyncio.to_thread(data_fetcher._tdx_stock_list)
        if stocks:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_fp, 'w', encoding='utf-8') as f:
                    json.dump(stocks, f, ensure_ascii=False)
            except Exception:
                pass
        return stocks
    except Exception as e:
        logger.warning('[updater] 通达信列表失败: %r', e)
        return []


async def _fetch_sina(fetcher):
    """新浪全量列表（兜底源）"""
    try:
        return await fetcher._fetch_stock_list_sina(page_count=40)
    except Exception as e:
        logger.warning('[updater] 新浪列表失败: %r', e)
        return []


async def _fetch_tencent_quotes(fetcher, codes):
    """腾讯行情批量获取现价（并发分批，避免 URL 过长且大幅提速）"""
    result = {}
    batches = [codes[i:i + _QQ_BATCH]
               for i in range(0, len(codes), _QQ_BATCH)]
    # 并发 8 批：110 批串行 → 约 14 轮，网络往返由串行叠加变为并行
    sem = asyncio.Semaphore(8)

    async def fetch_batch(batch):
        async with sem:
            try:
                return await asyncio.wait_for(
                    fetcher.fetch_real_time_quotes(batch), timeout=15)
            except Exception as e:
                logger.warning('[updater] 腾讯行情批次失败: %r', e)
                return {}

    for quotes in await asyncio.gather(*(fetch_batch(b) for b in batches)):
        for code, q in (quotes or {}).items():
            cur = q.get('current')
            if cur:
                price = _to_float(cur)
                if price > 0:
                    result[code] = price
    return result


# 板块概念过滤规则统一收敛到 concept_filter（与搜索栏 fetch_stock_concepts
# 共用同一套风格/地域/行情/报告类标签过滤），保证两处展示一致
from concept_filter import filter_concepts
_EM_BOARD_URL = ('https://datacenter-web.eastmoney.com/api/data/v1/get?'
                 'sortColumns=SECURITY_CODE&sortTypes=1&pageSize=%d&pageNumber=%d'
                 '&reportName=RPT_F10_CORETHEME_BOARDTYPE&columns=ALL')


def _record_board(board_map, wanted, row):
    """记录一条 股票→板块 关联（wanted 集合外的直接丢弃）"""
    code = str(row.get('SECURITY_CODE') or '').strip()
    if code not in wanted:
        return
    name = str(row.get('BOARD_NAME') or '').strip()
    level = row.get('BOARD_LEVEL')
    if name:
        board_map.setdefault(code, []).append((name, level))


def _format_sector(records, max_concepts):
    """从板块关联记录生成 sector_concept：与搜索栏一致的'、'连接概念列表。

    统一走 concept_filter 过滤（风格/地域/行情/报告类标签剔除），
    行业层级（电子/光学光电子/面板等）一并保留为概念展示，保证与
    data_fetcher.fetch_stock_concepts（搜索栏）返回一致。
    """
    if not records:
        return ''
    concepts = filter_concepts([n for n, _ in records])
    if max_concepts and max_concepts > 0:
        concepts = concepts[:max_concepts]
    return '、'.join(concepts)


async def _fetch_concepts_em(fetcher, codes, max_concepts=_MAX_CONCEPTS,
                             progress_cb=None):
    """东财 datacenter 反查板块概念：全量分页拉取 股票×板块 关联表
    （约 9.2 万条，pageSize=5000 仅 19 页），构建 {code: '概念1、概念2,...'}。

    datacenter 接口 pageSize 实测上限 5000，500/页需 185 页 → 5000/页仅 19 页，
    耗时从 ~25s 降到 ~3s。相比逐只接口（5123 次请求），全量反查仅 19 个并发
    请求，稳定且快。
    """
    result = {}
    if not codes:
        return result
    wanted = set(str(c) for c in codes)
    board_map = {}

    def _progress(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    PAGE_SIZE = 5000
    sem = asyncio.Semaphore(20)

    async def fetch_page(page):
        async with sem:
            url = _EM_BOARD_URL % (PAGE_SIZE, page)
            for attempt in range(3):
                try:
                    raw = await asyncio.wait_for(fetcher._fetch(url), timeout=12)
                    if raw:
                        return json.loads(raw)
                except Exception as e:
                    logger.warning('[updater] 板块关联第 %d 页第 %d 次失败: %r',
                                   page, attempt + 1, e)
                    await asyncio.sleep(0.2 * (attempt + 1))
            return None

    # 第 1 页确定总页数
    first = await fetch_page(1)
    if not first:
        logger.warning('[updater] 板块概念数据拉取失败')
        return result
    total_pages = int(((first.get('result') or {}).get('pages')) or 1)
    for row in (first.get('result') or {}).get('data') or []:
        _record_board(board_map, wanted, row)

    # 并发拉取剩余页
    _progress(f'正在从东财获取板块概念（1/{total_pages} 页）...')
    done = 1
    pages = [fetch_page(p) for p in range(2, total_pages + 1)]
    for i, data in enumerate(asyncio.as_completed(pages)):
        d = await data
        if d:
            for row in (d.get('result') or {}).get('data') or []:
                _record_board(board_map, wanted, row)
        done += 1
        if done % 20 == 0 or done == total_pages:
            _progress(f'正在从东财获取板块概念（{done}/{total_pages} 页）...')

    for code, records in board_map.items():
        s = _format_sector(records, max_concepts)
        if s:
            result[code] = s
    return result


def _clean_sector(val):
    """清洗板块字段：过滤 'sz000001' 式市场代码、'-'、空值"""
    val = str(val or '').strip()
    if not val or val in ('-', '--'):
        return ''
    import re
    if re.fullmatch(r'(sh|sz|bj)\d{6}', val):
        return ''
    return val


def _valid_stock(item):
    """过滤指数/信用/国债/退市/ST 等非正常交易 A 股"""
    import data_fetcher
    try:
        if not data_fetcher.should_exclude_stock(
                str(item.get('code') or ''),
                str(item.get('name') or ''),
                float(item.get('market_cap') or 0)):
            return True
    except Exception:
        pass
    return False


async def collect_stock_list(fetcher, progress_cb=None):
    """多源采集股票列表，返回规范化后的 dict 列表（已去重、含拼音与现价）"""
    def _progress(msg):
        if progress_cb:
            progress_cb(msg)

    _progress('正在从东方财富获取全量股票列表...')
    try:
        em = await _fetch_eastmoney(fetcher)
    except Exception as e:
        logger.warning('[updater] 东方财富列表失败: %r', e)
        em = []

    # 通达信与新浪互为独立补充源，并行拉取（TDX 慢也不阻塞新浪）
    _progress(f'东财 {len(em)} 只，正在从通达信/新浪补充...')
    tdx_task = asyncio.create_task(_fetch_tdx())
    sina_task = asyncio.create_task(_fetch_sina(fetcher))
    tdx, sina = await asyncio.gather(tdx_task, sina_task)

    # 合并：东财优先（板块概念以 f100 行业板块为准），
    # TDX/新浪仅补充缺失代码；新浪 sector 是市场代码（sz000001），须清洗丢弃
    merged = {}
    for s in em:
        code = str(s.get('code') or '').strip()
        name = str(s.get('name') or '').strip()
        if not code or not name:
            continue
        merged[code] = {
            'code': code,
            'name': name,
            'name_initials': str(s.get('name_initials') or '').strip(),
            'current_price': _to_float(s.get('current_price') or s.get('current')),
            'sector_concept': _clean_sector(s.get('sector_concept')
                                            or s.get('sector') or ''),
            'stock_exchange': _infer_exchange(code),
        }

    for s in tdx + sina:
        code = str(s.get('code') or '').strip()
        name = str(s.get('name') or '').strip()
        if not code or not name or not _valid_stock(s):
            continue
        if code not in merged:
            merged[code] = {
                'code': code,
                'name': name,
                'name_initials': str(s.get('name_initials') or '').strip(),
                'current_price': _to_float(s.get('current_price') or s.get('current')),
                'sector_concept': _clean_sector(s.get('sector_concept')
                                                or s.get('sector') or ''),
                'stock_exchange': _infer_exchange(code),
            }

    _progress(f'合并去重后共 {len(merged)} 只，正在生成拼音首字母...')
    import re
    try:
        import data_fetcher
        for s in merged.values():
            if not s['name_initials']:
                try:
                    # 去除名称中全角/半角空格（如 '万 科Ａ'），避免拼音带空格
                    clean_name = re.sub(r'\s+', '', s['name'])
                    s['name_initials'] = data_fetcher.get_pinyin_initials(clean_name)
                except Exception:
                    s['name_initials'] = ''
    except Exception:
        pass

    _progress(f'正在从腾讯获取实时价格（{len(merged)} 只）...')
    prices = await _fetch_tencent_quotes(fetcher, list(merged.keys()))
    for code, price in prices.items():
        if code in merged and price > 0:
            merged[code]['current_price'] = price

    # 板块概念增强：东财列表 f100 只有行业名（如"银行"），
    # 用 datacenter 反查补全概念标签（如"白酒Ⅱ|酿酒概念,味蕾经济,..."）。
    # 概念为空时保留行业名兜底。
    concepts = await _fetch_concepts_em(fetcher, list(merged.keys()),
                                        progress_cb=_progress)
    if concepts:
        for s in merged.values():
            cpt = concepts.get(s['code'])
            if cpt:
                s['sector_concept'] = cpt

    result = list(merged.values())
    _progress(f'多源采集完成，共 {len(result)} 只股票')
    return result


async def update_stock_list(fetcher, progress_cb=None):
    """更新 MySQL 股票列表：多源采集 + 缺失添加/已存在更新。返回 (总数, 新增数)。"""
    stocks = await collect_stock_list(fetcher, progress_cb)
    added, updated = mysql_store.insert_missing(stocks)
    # 同步保存一份到安装目录 data 目录，供各栏目在数据库不可用/为空时直接加载
    try:
        app_paths.save_stock_list(stocks)
        logger.info('[updater] 已保存股票列表到安装目录: %s',
                    app_paths.get_stock_list_file())
    except Exception:
        logger.warning('[updater] 保存股票列表到安装目录失败', exc_info=True)
    logger.info('[updater] 更新完成：共 %d 只，新增 %d 只，更新 %d 只',
                len(stocks), added, updated)
    return len(stocks), added
