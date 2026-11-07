# -*- coding: utf-8 -*-
"""data_fetcher 运行时补丁（源文件缺失，仅剩 pyc，通过 monkey patch 修复）

修复内容：
1. fetch_stock_list：新浪兜底列表（全量 5085 只、market_cap 全 0、含指数/停牌）
   不再写入正常缓存 key，且返回前过滤掉非 A 股/指数/停牌代码，
   避免 MorningTab 等拿到劣质列表后腾讯行情大面积失败导致"无数据"。
2. fetch_real_time_quotes：腾讯批量行情返回不足时，自动用东方财富
   push2delay 批量行情接口补齐缺失代码，提高行情覆盖率。
3. _cache_set：拒绝缓存空值 / 全 None 列表，避免异常结果污染缓存
   （此前 [None]*61 的 K线被当成 truthy 缓存，1 小时内一直返回 None）。
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

from concept_filter import filter_concepts

logger = logging.getLogger('data_fetcher_patch')

_APPLIED = False


def _num(v, default=0.0):
    """东财数值字段清洗：停牌/新股等场景返回 '-'、'--'、''、None，统一转数值"""
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_valid_a_stock(s):
    """过滤：仅保留正常 A 股（沪 60/688、深 00/30），排除指数/债券/退市/ST"""
    if not isinstance(s, dict):
        return False
    code = str(s.get('code') or '')
    name = str(s.get('name') or '')
    if len(code) != 6 or not code.isdigit():
        return False
    if not code.startswith(('60', '688', '00', '30', '000', '001', '002', '003')):
        return False
    upper = name.upper()
    if ('ST' in upper or '退' in upper or 'PT' in upper or '指数' in upper
            or '信用' in upper or '国债' in upper):
        return False
    return True


def _looks_like_sina_list(stocks):
    """新浪兜底特征：数量巨大且 market_cap 普遍为 0"""
    if not stocks or len(stocks) < 800:
        return False
    mc_zero = sum(1 for s in stocks
                  if isinstance(s, dict) and not s.get('market_cap'))
    return mc_zero > len(stocks) * 0.9


# === 实施公告正文提取（真实每股金额 + 送转股数） ===
# 正文实测样例（2026-09）：
#   "公司本次实际向全体股东每 10 股派发现金红利 8.00 元（含税）"      -> 002558 8.00
#   "向全体股东每10股派0.880000元人民币现金（含税"                     -> 002487 0.88
#   "向全体股东每 10 股派发现金红利 1 元（含税）"                       -> 002414 1
# 陷阱：同一份公告还含"按公司总股本折算的每 10 股现金红利（含税）=...=7.97 元"，
#       那是除权参考价而非分红额，必须排除含"折算/参考价/计算公式"的句子。
_NOTICE_DIVI_RE = re.compile(r'每\s*10\s*股[^0-9]{0,18}?(\d+(?:\.\d+)?)\s*元')
_NOTICE_BONUS_RE = re.compile(
    r'每\s*10\s*股[^0-9]{0,10}?(?:送|转增)[^0-9]{0,4}?(\d+(?:\.\d+)?)\s*股')
_NOTICE_SKIP_RE = re.compile(r'折算|参考价|计算公式|除权除息价|不派|不进行分配')


def _notice_amounts(content):
    """从实施公告正文提取 (每股含税现金, 每 10 股送转股数)。

    先压平所有空白（正文"每 10 股派发现金红\\n利 8.00 元"常被换行截断），
    再按句号/分号切句，逐句跳过"折算/参考价"类公式句，取首个有效值。
    """
    if not content:
        return 0.0, 0.0
    flat = re.sub(r'\s+', '', content)
    per_share = 0.0
    for seg in re.split(r'[。；;]', flat):
        if _NOTICE_SKIP_RE.search(seg):
            continue
        m = _NOTICE_DIVI_RE.search(seg)
        if m:
            try:
                v = float(m.group(1))
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                per_share = v / 10.0
                break
    bonus = 0.0
    if '不送红股' not in flat and '不转增' not in flat:
        bm = _NOTICE_BONUS_RE.search(flat)
        if bm:
            try:
                bonus = float(bm.group(1))
            except (TypeError, ValueError):
                bonus = 0.0
    return per_share, bonus


def apply_patches(data_fetcher):
    """对 data_fetcher 模块打补丁（幂等，可重复调用）"""
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    cls = data_fetcher.MultiSourceFetcher
    orig_fetch_stock_list = cls.fetch_stock_list
    orig_fetch_real_time_quotes = cls.fetch_real_time_quotes
    orig_cache_set = cls._cache_set

    # ============ 东财批量行情（供 Patch 1/2 共用） ============
    async def _fetch_quotes_eastmoney(self, codes):
        """东方财富 push2delay 批量行情，返回 {code: {字段...}}（与腾讯格式一致）"""
        result = {}
        for i in range(0, len(codes), 60):
            batch = codes[i:i + 60]
            secids = ','.join(('1.' if c.startswith(('6', '9')) else '0.') + c
                              for c in batch)
            url = ('https://push2delay.eastmoney.com/api/qt/ulist.np/get'
                   '?fltt=2&invt=2&fields=f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,'
                   'f15,f16,f17,f18,f20,f21,f23&secids=' + secids)
            try:
                t = await self._fetch(url, 'utf-8')
                data = json.loads(t)
                diff = (data.get('data') or {}).get('diff') or []
                for item in diff:
                    code = str(item.get('f12') or '')
                    if not code:
                        continue
                    result[code] = {
                        'name': item.get('f14') or '',
                        'code': code,
                        'current': _num(item.get('f2')),
                        'yesterday_close': _num(item.get('f18')),
                        'open': _num(item.get('f17')),
                        'high': _num(item.get('f15')),
                        'low': _num(item.get('f16')),
                        'volume': _num(item.get('f5')),
                        'amount': _num(item.get('f6')),
                        'change_pct': _num(item.get('f3')),
                        'turnover_rate': _num(item.get('f8')),
                        'pe': _num(item.get('f9')),
                        # 东财 f20 总市值单位为「元」，下游按「亿元」消费，需 /1e8
                        'market_cap': _num(item.get('f20')) / 100000000.0,
                        'volume_ratio': _num(item.get('f10')),
                        'buy_prices': [], 'buy_volumes': [],
                        'sell_prices': [], 'sell_volumes': [],
                    }
            except Exception as e:
                logger.warning('[patch] 东财批量行情失败: %r', e)
            await asyncio.sleep(0.25)
        return result

    async def _clear_cache_key(self, key):
        try:
            fp = os.path.join(data_fetcher.CACHE_DIR, '%s.json' % key)
            if os.path.exists(fp):
                os.remove(fp)
                return True
        except Exception:
            pass
        return False

    # ============ Patch 1: fetch_stock_list 新浪兜底保护 ============
    async def _patched_fetch_stock_list(self, page_count=40):
        # Patch 4 补充：残缺缓存自愈 —— 旧版东财列表 fs 写错（m:0+t:13,m:0+t:81
        # 拉到老三板 400xxx），导致缓存里缺沪市(60/68)股票。检测到此类污染缓存
        # 直接清除，强制走新逻辑重拉全量（约 5500 只）。
        ck = self._cache_key('stock_list', page_count)
        try:
            cached = self._cache_get(ck)
            if isinstance(cached, list) and len(cached) > 500:
                codes = [str(s.get('code') or '') for s in cached
                         if isinstance(s, dict)]
                sh_cnt = sum(1 for c in codes if c.startswith(('60', '68')))
                gr_cnt = sum(1 for c in codes if c.startswith(('30', '68')))
                # 正常沪深全量约 5500 只：沪(60+68)约 44%，创业板+科创板(30+68)约 37%
                if sh_cnt < len(codes) * 0.3 or gr_cnt < len(codes) * 0.2:
                    logger.warning('[patch] 残缺列表缓存(沪 %d/%d, 创科 %d/%d)，清除重拉',
                                   sh_cnt, len(codes), gr_cnt, len(codes))
                    await _clear_cache_key(self, ck)
        except Exception as e:
            logger.warning('[patch] 缓存检查异常: %r', e)

        stocks = await orig_fetch_stock_list(self, page_count)
        if not _looks_like_sina_list(stocks):
            return stocks

        logger.warning('[patch] 检测到新浪劣质列表 %d 只，尝试用东财恢复',
                       len(stocks))
        ck = self._cache_key('stock_list', page_count)

        # 1) 清除被污染缓存，避免 1 小时内一直命中劣质列表
        await _clear_cache_key(self, ck)

        # 2) 重试东财列表（短暂抖动场景可直接恢复）
        try:
            em = await self._fetch_stock_list_eastmoney()
            if em and len(em) >= 100:
                mc_zero = sum(1 for s in em
                              if isinstance(s, dict) and not s.get('market_cap'))
                if mc_zero <= len(em) * 0.8:
                    logger.warning('[patch] 东财重试成功: %d 只', len(em))
                    self._cache_set(ck, em)
                    return em
        except Exception as e:
            logger.warning('[patch] 东财重试失败: %r', e)

        # 3) 东财列表仍失败：用东财批量行情验证新浪列表，
        #    只保留有实时行情的股票（有效 A 股），并补齐 market_cap
        try:
            valid_codes = [s['code'] for s in stocks if _is_valid_a_stock(s)]
            quotes = await _fetch_quotes_eastmoney(self, valid_codes[:800])
            recovered = []
            for s in stocks:
                if not _is_valid_a_stock(s):
                    continue
                q = quotes.get(s['code'])
                if q is None:
                    continue
                s2 = dict(s)
                if q.get('market_cap'):
                    s2['market_cap'] = q['market_cap']
                if q.get('name') and not s2.get('name'):
                    s2['name'] = q['name']
                recovered.append(s2)
            if recovered:
                logger.warning('[patch] 东财行情验证恢复 %d 只有效股票',
                               len(recovered))
                self._cache_set(ck, recovered)
                return recovered
        except Exception as e:
            logger.warning('[patch] 东财行情验证失败: %r', e)

        # 4) 全部失败：清缓存返回空，下次重新拉取
        await _clear_cache_key(self, ck)
        logger.warning('[patch] 东财恢复失败，返回空列表')
        return []

    cls.fetch_stock_list = _patched_fetch_stock_list

    # ============ Patch 2: fetch_real_time_quotes 东财补齐 ============
    async def _patched_fetch_real_time_quotes(self, codes):
        result = await orig_fetch_real_time_quotes(self, codes)
        if not codes:
            return result
        missing = [c for c in codes if c not in result]
        if len(result) < len(codes) * 0.7 and missing:
            logger.warning('[patch] 腾讯行情覆盖率低 %d/%d，东财补齐 %d 只',
                           len(result), len(codes), len(missing))
            try:
                em = await _fetch_quotes_eastmoney(self, missing)
                for k, v in em.items():
                    if k not in result:
                        result[k] = v
            except Exception as e:
                logger.warning('[patch] 东财补齐失败: %r', e)
        return result

    cls.fetch_real_time_quotes = _patched_fetch_real_time_quotes

    # ============ Patch 3: _cache_set 拒绝缓存空值/全 None 列表 ============
    def _patched_cache_set(self, key, value, ttl=None):
        if value in (None, [], {}, '', 0, False):
            logger.warning('[patch] 拒绝缓存空值 key=%s', key)
            return None
        if isinstance(value, list) and value and all(v is None for v in value):
            logger.warning('[patch] 拒绝缓存全 None 列表 key=%s', key)
            return None
        return orig_cache_set(self, key, value, ttl)

    cls._cache_set = _patched_cache_set

    # ============ Patch 4: 修正东财列表 fs 参数 + 并发翻页提速 ============
    # 1) 原代码用 'm:0+t:13,m:0+t:81' 拉"沪主板+科创板"，实测该组合实际返回
    #    老三板 400xxx（6843 只），沪市 60/68 一只都拿不到，导致列表长期缺失沪市。
    #    东财实测正确写法：m:1+t:2（沪主板 1844）+ m:1+t:23（科创板 570），
    #    全量组合 m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23 = 5549 只。
    # 2) 提速：clist 的 pz 参数实测上限就是 100（pz=200 每页仍只回 100 条且
    #    会导致分页判断错乱丢数据），只能靠并发。实测沪市单市场：串行 31 页
    #    12.8s；并发 2→6.0s；并发 4→3.0s 且 31/31 页完整；并发 6→2.8s；
    #    但两个市场各自 8 并发（总 16）会触发东财限流（74.7s 且丢页）。
    #    因此改为两个市场共享同一全局 Semaphore(4)：总并发 4，约 6s 拿全
    #    5000+ 只且不触发限流。每页用裸 _fetch 避免 _fetch_json 缓存问题。
    orig_fetch_stock_list_eastmoney = cls._fetch_stock_list_eastmoney

    async def _patched_fetch_stock_list_eastmoney(self):
        bases = ('https://push2delay.eastmoney.com',
                 'https://82.push2delay.eastmoney.com',
                 'https://72.push2delay.eastmoney.com',
                 'https://44.push2delay.eastmoney.com')
        PAGE_SIZE = 100
        # 两个市场共享同一并发闸：总并发 4，避免触发东财限流
        sem = asyncio.Semaphore(4)

        def _parse_page(data):
            """解析一页 clist 响应 → (stocks, total)；异常返回 ([], 0)"""
            if not data:
                return [], 0
            try:
                d = data.get('data') or {}
            except AttributeError:
                return [], 0
            items = d.get('diff') or []
            stocks = []
            for item in items:
                code = str(item.get('f12') or '')
                name = str(item.get('f14') or '')
                if not code or not name or not code.isdigit() or len(code) != 6:
                    continue
                if not _is_valid_a_stock({'code': code, 'name': name}):
                    continue
                mc_val = item.get('f20') or 0
                if mc_val in (0, '-', ''):
                    mc = 0
                else:
                    try:
                        mc = float(mc_val) / 100000000.0
                    except (ValueError, TypeError):
                        mc = 0
                pe_val = item.get('f9') or 0
                if pe_val:
                    try:
                        pe = float(pe_val)
                    except (ValueError, TypeError):
                        pe = 0
                else:
                    pe = 0
                stocks.append({'code': code, 'name': name,
                               'market_cap': mc, 'pe': pe,
                               'sector': item.get('f100') or ''})
            return stocks, int(d.get('total') or 0)

        async def _fetch_market(fs_val):
            """先取第 1 页得 total，再并发翻剩余页（与另一市场共享全局并发）"""
            page1 = None
            for base in bases:
                url = ('%s/api/qt/clist/get?pn=1&pz=%d&po=0&np=1&fltt=2'
                       '&invt=2&fid=f12&fs=%s&fields=f2,f3,f4,f5,f9,f12,f14,'
                       'f15,f16,f17,f18,f20,f21,f100' % (base, PAGE_SIZE, fs_val))
                try:
                    t = await self._fetch(url, 'utf-8')
                    if t:
                        page1 = json.loads(t)
                        break
                except Exception:
                    continue
            if not page1 or not page1.get('data'):
                return []
            stocks, total = _parse_page(page1)
            pages = max(1, -(-total // PAGE_SIZE))

            async def fetch_page(page):
                async with sem:
                    for base in bases:
                        url = ('%s/api/qt/clist/get?pn=%d&pz=%d&po=0&np=1'
                               '&fltt=2&invt=2&fid=f12&fs=%s&fields=f2,f3,f4,'
                               'f5,f9,f12,f14,f15,f16,f17,f18,f20,f21,f100'
                               % (base, page, PAGE_SIZE, fs_val))
                        try:
                            t = await asyncio.wait_for(
                                self._fetch(url, 'utf-8'), timeout=10)
                            if t:
                                d = json.loads(t)
                                if d and d.get('data'):
                                    return d
                        except Exception:
                            continue
                    return None

            for d in await asyncio.gather(
                    *(fetch_page(p) for p in range(2, pages + 1))):
                if d:
                    stocks.extend(_parse_page(d)[0])
            return stocks

        sh_task = asyncio.create_task(_fetch_market('m:0+t:6,m:0+t:80'))
        sz_task = asyncio.create_task(_fetch_market('m:1+t:2,m:1+t:23'))
        sh_stocks, sz_stocks = await asyncio.gather(sh_task, sz_task)
        merged = {}
        for s in (sh_stocks or []) + (sz_stocks or []):
            merged[s['code']] = s

        # 东财翻页受接口限流影响可能拿不全（如只剩 1500 只），
        # 用新浪全量列表补齐缺失代码，保证列表完整性（沪深 5000+ 只）。
        if len(merged) < 4000:
            try:
                sina = await self._fetch_stock_list_sina(page_count=40)
                added = 0
                for s in sina or []:
                    code = str(s.get('code') or '')
                    if not code or code in merged:
                        continue
                    if not _is_valid_a_stock(s):
                        continue
                    merged[code] = s
                    added += 1
                if added:
                    logger.warning('[patch] 东财列表不全(%d)，新浪补齐 %d 只 -> %d',
                                   sum(1 for _ in sh_stocks or []) +
                                   sum(1 for _ in sz_stocks or []),
                                   added, len(merged))
            except Exception as e:
                logger.warning('[patch] 新浪补齐失败: %r', e)
        return list(merged.values())

    cls._fetch_stock_list_eastmoney = _patched_fetch_stock_list_eastmoney

    # ============ Patch 5: 市场热点板块（东财概念板块涨幅榜 + 成分股） ============
    # 过滤财务预披露/业绩类板块（预增/预减/首亏/扭亏等不是市场热点题材）
    _BAD_BOARD_KW = ('预增', '预减', '首亏', '扭亏', '续亏', '续盈',
                     '业绩', '预告', '年报', '中报', '季报', 'ST', '退市')

    async def _fetch_hot_boards(self, max_boards=12):
        """东财概念板块涨幅榜前 N 名，返回 [{'code': 'BK...', 'name': '...', 'change_pct': x}, ...]"""
        bases = ('https://push2.eastmoney.com',
                 'https://push2delay.eastmoney.com',
                 'https://82.push2delay.eastmoney.com',
                 'https://72.push2delay.eastmoney.com',
                 'https://44.push2delay.eastmoney.com')
        for base in bases:
            try:
                url = ('%s/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2'
                       '&fid=f3&fs=m:90+t:3+f:!50&fields=f3,f12,f14'
                       % (base, max(1, max_boards)))
                t = await self._fetch(url, 'utf-8')
                if not t:
                    continue
                data = json.loads(t)
                diff = (data.get('data') or {}).get('diff') or []
                boards = []
                for item in diff:
                    code = str(item.get('f12') or '')
                    name = str(item.get('f14') or '')
                    if not code or not name:
                        continue
                    if any(kw in name for kw in _BAD_BOARD_KW):
                        continue
                    chg = item.get('f3')
                    try:
                        chg = float(chg) if chg not in (None, '-', '') else 0.0
                    except (ValueError, TypeError):
                        chg = 0.0
                    boards.append({'code': code, 'name': name, 'change_pct': chg})
                if boards:
                    return boards
            except Exception as e:
                logger.warning('[patch] 热点板块 %s 失败: %r', base, e)
        logger.warning('[patch] 获取热点板块失败（全部域名）')
        return []

    async def _fetch_board_members(self, board_code, max_members=60):
        """东财板块成分股，返回 [{'code': '600xxx', 'name': '...'}, ...]"""
        bases = ('https://push2.eastmoney.com',
                 'https://push2delay.eastmoney.com',
                 'https://82.push2delay.eastmoney.com',
                 'https://72.push2delay.eastmoney.com',
                 'https://44.push2delay.eastmoney.com')
        for base in bases:
            members = []
            try:
                url = ('%s/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2'
                       '&fid=f3&fs=b:%s&fields=f12,f14,f3'
                       % (base, max(1, max_members), board_code))
                t = await self._fetch(url, 'utf-8')
                if not t:
                    continue
                data = json.loads(t)
                diff = (data.get('data') or {}).get('diff') or []
                for item in diff:
                    code = str(item.get('f12') or '')
                    name = str(item.get('f14') or '')
                    if code and code.isdigit() and len(code) == 6:
                        members.append({'code': code, 'name': name})
                if members:
                    return members
            except Exception as e:
                logger.warning('[patch] 板块成分 %s/%s 失败: %r', base, board_code, e)
        logger.warning('[patch] 获取板块成分失败（全部域名）: %s', board_code)
        return []

    cls.fetch_hot_boards = _fetch_hot_boards
    cls.fetch_board_members = _fetch_board_members

    # ============ Patch 5b: 真实除权除息日（双接口结合） ============
    # 主源：RPT_LICO_FN_CPD（拿 ASSIGNDSCRPT 含 10派X元 → 每股分红）
    # 副源：np-anotice-stock（拿"分配方案实施"公告 notice_date → 真实除权日）
    # 关联方式：对 RPT_LICO_FN_CPD 返回的每只股票，调用 np-anotice-stock 查真实除权日
    # 覆盖估算（REPORTDATE + 75 天）。
    # 优势：
    # - 每股分红来源 RPT_LICO_FN_CPD 字段稳定，含税/扣税后/转增都有
    # - 真实除权日来自公告，避免 REPORTDATE+75 估算偏差
    # - 接口简单，主表拉一次 + 副接口按 codes 分批查
    # ASSIGNDSCRPT 实际样例：
    #   "10派2.49元(含税,扣税后2.241元)"
    #   "10派2.5元(含税,扣税后2.25元)10转5股"
    #   "10派1元(含税,扣税后0.9元)10送红股2股(含税)"
    #   "10派3元(含税,扣税后2.7元)10转3股10送红股2股"
    # 注：东财用的是 "10转X股"（单字"转"），不是"转增X股"
    _DIVI_RE = re.compile(r'10派([\d.]+)元')
    _AFTER_TAX_RE = re.compile(r'扣税后([\d.]+)元')
    _GIFT_RE = re.compile(r'10转([\d.]+)股')
    _BONUS_RE = re.compile(r'10送红股([\d.]+)股')

    async def _fetch_impl_dates(self, codes, begin_date, end_date, batch_size=80):
        """副源：np-anotice-stock 实施公告 → code -> {div_date, name}（真实除权日）

        关键：除权日必须从公告正文提取，而不是用 notice_date（公告日）——
        公告日早于股权登记日 5-6 天，例如 002558 公告 9-16、股权登记 9-21、
        除权 9-22，用 notice_date 会被"明天或之后"硬约束误删。
        """
        impl_map = {}
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            stock_list = ','.join(batch)
            url = (
                f'https://np-anotice-stock.eastmoney.com/api/security/ann'
                f'?cb=&sr=-1&page_size=200'
                f'&page_index=1&ann_type=A&client_source=web'
                f'&stock_list={stock_list}'
                f'&begin_time={begin_date}&end_time={end_date}'
            )
            try:
                data = await self._fetch_json(url, timeout=15)
            except Exception:
                continue
            if not data or not data.get('data') or not data['data'].get('list'):
                continue
            for item in data['data']['list']:
                cols = item.get('columns') or []
                col_name = cols[0].get('column_name', '') if cols else ''
                if col_name != '分配方案实施':
                    continue
                codes_in_item = item.get('codes') or []
                if not codes_in_item:
                    continue
                c = codes_in_item[0]
                code = (c.get('stock_code') or '').strip()
                if not code.isdigit():
                    continue
                nd = (item.get('notice_date') or '')[:10]
                if not nd or nd < begin_date:
                    continue
                art_code = (item.get('art_code') or '').strip()
                # 同 code 保留最新一条实施公告（以 notice_date 判定）
                if code not in impl_map or nd > impl_map[code]['div_date']:
                    impl_map[code] = {
                        'div_date': nd,   # 占位，下面会从公告正文覆盖为真实除权日
                        'name': (c.get('short_name') or '').strip(),
                        'art_code': art_code,
                        'per_share': 0.0,    # 占位，下面从公告正文"每10股派X元"提取
                        'bonus_share': 0.0,
                    }

        # === 第二步：并发拉每只股票的最新实施公告正文，
        # 同时提取"除权（息）日"（覆盖占位）与"每 10 股派 X 元"（填补主源缺失的金额） ===
        if impl_map:
            tasks = [self._fetch_real_ex_date(rec) for rec in impl_map.values()]
            details = await asyncio.gather(*tasks, return_exceptions=True)
            for code, det in zip(impl_map.keys(), details):
                if not isinstance(det, dict):
                    continue
                if det.get('div_date'):
                    impl_map[code]['div_date'] = det['div_date']
                if det.get('per_share', 0) > 0:
                    impl_map[code]['per_share'] = det['per_share']
                    impl_map[code]['amount_from_notice'] = True
                if det.get('bonus_share', 0) > 0:
                    impl_map[code]['bonus_share'] = det['bonus_share']
        return impl_map

    async def _fetch_real_ex_date(self, rec):
        """从实施公告正文（np-cnotice-stock）提取真实除权日与每股金额。

        返回 {'div_date': 'YYYY-MM-DD' 或 '', 'per_share': float, 'bonus_share': float}。
        失败/超时返回空 dict 字段，调用方继续用 notice_date 占位、金额留 0。
        """
        art_code = rec.get('art_code') or ''
        if not art_code:
            return {}
        url = (
            f'https://np-cnotice-stock.eastmoney.com/api/content/ann'
            f'?art_code={art_code}&client_source=web&page_index=1'
        )
        try:
            data = await self._fetch_json(url, timeout=6)
        except Exception:
            return {}
        if not data or not data.get('data'):
            return {}
        content = data['data'].get('notice_content') or ''
        # 金额先算（与除权日正则互不影响）
        per_share, bonus_share = _notice_amounts(content)
        # 兼容三种写法（注意"除权日（除息日）"与"为：日期"之间可能有换行）：
        #   "除权日（除息日）\n为：2026 年 09 月 22 日"  ← 002558 实际写法
        #   "除权除息日为：2026 年 09 月 22 日"          ← 简化写法
        #   "除权除息日：2026-09-22"                    ← 横线分隔
        m = re.search(
            r'除权日[（(]除息日[）)]\s*[为：:]?\s*[:：]?\s*'
            r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
            content,
        )
        if not m:
            m = re.search(
                r'除权除息日\s*[为：:]?\s*[:：]?\s*'
                r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
                content,
            )
        if not m:
            m = re.search(
                r'除权除息日\s*[为：:]?\s*[:：]?\s*'
                r'(\d{4})\s*[\-\/年]\s*(\d{1,2})\s*[\-\/月]\s*(\d{1,2})',
                content,
            )
        if not m:
            # 退化：含"除权"开头到首个"YYYY 年 MM 月 DD 日"
            # 排除"除权除息参考价""除权除息价格"这类公式描述（后面跟等号/价格）
            m = re.search(
                r'除权[^\n：:=]{0,18}?\s*[为：:]?\s*[:：]?\s*'
                r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
                content,
            )
        div_date = ''
        if m:
            y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
            div_date = f'{y}-{mo}-{d}'
        return {
            'div_date': div_date,
            'per_share': per_share,
            'bonus_share': bonus_share,
        }

    async def _fetch_dividend_calendar(self, codes=None,
                                       start_date_str='',
                                       lookback_days=540,
                                       batch_size=80):
        """双接口结合：RPT_LICO_FN_CPD（每股分红）+ np-anotice-stock（真实除权日）。

        参数：
          codes: 候选股票代码列表（默认 None 表示全表扫描，可能慢）
          start_date_str: 起始除权日（含）；为空时取今天（含今天除权）
          lookback_days: 报告期回溯天数（默认 540 天，覆盖近一年）

        返回 [{'code','name','div_date','per_share','bonus_share','plan'}, ...]，
        div_date 是真实除权日（优先）或估算。

        过滤：每股（含税）> 0.2 元 且 div_date >= 明天。
        金额来源：主源 RPT_LICO_FN_CPD 的 ASSIGNDSCRPT；主源缺失时用副源
        "分配方案实施"公告正文提取的"每 10 股派 X 元"（amount_from_notice=True）。
        """
        if start_date_str:
            base_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        else:
            base_date = datetime.now()
        report_start = (base_date - timedelta(days=lookback_days)).strftime('%Y-%m-%d')

        # 主源：拉 RPT_LICO_FN_CPD 分红预案
        # 关键设计：
        # 1) 排序 NOTICE_DATE 降序（而非 REPORTDATE 降序）——分红的"近期"由公告日决定。
        # 2) 双窗口拉取，确保能覆盖：
        #    - 窗口 A: NOTICE_DATE ∈ [base_date-60, base_date]（约 5 页 ~ 2500 行）
        #       → 抓最近 2 个月发布的中报/季报/年报方案（绝大多数活跃方案）
        #    - 窗口 B: NOTICE_DATE ∈ [base_date-180, base_date-60]（约 15 页 ~ 7500 行）
        #       → 抓年报+更早半年报方案（覆盖年报发布期）
        #    两次合计约 20 页 × 500 行 = 10000 行，1 分钟内能完成。
        # 3) 必须带 SECURITY_TYPE_CODE="058001001" 限定 A 股（去掉 49 万行噪声）。
        # 4) 客户端按 TRADE_MARKET_ZJG 限定主板（0101=沪市主板 / 0201=深市主板），
        #    排除创业板/科创板/北交所/三板。
        rows = []
        bases = (
            'https://datacenter-web.eastmoney.com',
            'https://datacenter.eastmoney.com',
        )
        for base in bases:
            try:
                all_rows = []
                # 双窗口：先近后远
                windows = [
                    (60, 0),     # 近 60 天
                    (180, 60),   # 60~180 天前
                ]
                for span, offset in windows:
                    nb_start = (base_date - timedelta(days=span + offset)).strftime('%Y-%m-%d')
                    nb_end = (base_date - timedelta(days=offset)).strftime('%Y-%m-%d')
                    page = 0
                    max_pages = 40
                    while page < max_pages:
                        page += 1
                        url = (
                            '/api/data/v1/get?reportName=RPT_LICO_FN_CPD'
                            '&columns=SECURITY_CODE,SECURITY_NAME_ABBR,ASSIGNDSCRPT,'
                            'PAYYEAR,REPORTDATE,TRADE_MARKET_ZJG,NOTICE_DATE'
                            '&pageNumber={page}&pageSize=500'
                            f'&filter=(SECURITY_TYPE_CODE%3D%22058001001%22)'
                            f'(NOTICE_DATE%3E%3D%27{nb_start}%27)'
                            f'(NOTICE_DATE%3C%3D%27{nb_end}%27)'
                            '&sortColumns=NOTICE_DATE&sortTypes=-1'
                        ).format(page=page)
                        # 单页最多重试 2 次（应对偶发 timeout）
                        page_data = []
                        for retry in range(3):
                            data = await self._fetch_json(base + url, timeout=20)
                            rc = (data or {}).get('result', {}) or {}
                            page_data = rc.get('data') or []
                            if page_data or (data and data.get('success') is False and not page_data):
                                # 明确空页（已到末页）不重试
                                break
                            if page_data:
                                break
                            # 超时/网络错误，重试
                            import asyncio as _asyncio
                            await _asyncio.sleep(0.5 * (retry + 1))
                        if not page_data:
                            break
                        all_rows.extend(page_data)
                        # 末页判断：单页 < 500 行说明已读完
                        if len(page_data) < 500:
                            break
                if all_rows:
                    rows = all_rows
                    break
            except Exception:
                continue

        # 解析主源 + 估算除权日
        # 主板限定：沪 0101 / 深 0201；排除 0102/0103/0202/0203/04xx
        _MAIN_BOARD_ZJG = ('0101', '0201')
        plan_map = {}
        for row in rows:
            code = (row.get('SECURITY_CODE') or '').strip()
            name = (row.get('SECURITY_NAME_ABBR') or '').strip()
            plan = row.get('ASSIGNDSCRPT') or ''
            tm = (row.get('TRADE_MARKET_ZJG') or '').strip()
            if not code or not code.isdigit() or not name or '不分配' in plan:
                continue
            # 仅保留主板（避免创业板/科创板/北交所等被算进来）
            if tm not in _MAIN_BOARD_ZJG:
                continue
            m = _DIVI_RE.search(plan)
            if not m:
                continue
            try:
                per_share = float(m.group(1)) / 10.0
            except (TypeError, ValueError):
                continue
            if per_share <= 0:
                continue
            bonus_share = 0.0
            bm = _GIFT_RE.search(plan)
            if bm:
                try:
                    # 正则已含 "10" 修饰，直接是"每10股转X股"中的 X
                    bonus_share += float(bm.group(1))
                except (TypeError, ValueError):
                    pass
            sbm = _BONUS_RE.search(plan)
            if sbm:
                try:
                    bonus_share += float(sbm.group(1))
                except (TypeError, ValueError):
                    pass
            # 扣税后每股金额（0 表示字段缺失）
            per_share_after_tax = 0.0
            atm = _AFTER_TAX_RE.search(plan)
            if atm:
                try:
                    per_share_after_tax = float(atm.group(1)) / 10.0
                except (TypeError, ValueError):
                    per_share_after_tax = 0.0
            try:
                report_dt = datetime.strptime(
                    (row.get('REPORTDATE') or '')[:10], '%Y-%m-%d')
                est_div = (report_dt + timedelta(days=75)).strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                est_div = ''
            if not est_div:
                continue
            # 此处仅做宽泛过滤：保留估算日 >= 今天-180 天（覆盖半年内所有方案），
            # 避免 plan_map 过大；真正的 ">= 今天" 判断统一放在副源关联之后。
            if est_div < (base_date - timedelta(days=180)).strftime('%Y-%m-%d'):
                continue
            # 同 code 多方案时，优先保留"派现金额最大"的方案（避免小分红覆盖大分红）
            # 例如 600160 巨化股份：年报 10派3.80 > 半年报 10派2.20 > 三季报 10派1.80
            if code in plan_map:
                if per_share <= plan_map[code]['per_share']:
                    continue
            plan_map[code] = {
                'code': code,
                'name': name,
                'div_date': est_div,
                'per_share': per_share,
                'per_share_after_tax': per_share_after_tax,
                'bonus_share': bonus_share,
                'payyear': row.get('PAYYEAR') or '',
                'plan': plan,
                'report_date': (row.get('REPORTDATE') or '')[:10],
            }

        # 副源：用候选 codes 拉实施公告真实除权日
        # 副源是独立来源：即使 RPT_LICO_FN_CPD 缺失该股票（借壳上市等），
        # 只要公告接口查到"分配方案实施"就纳入（每股金额未知时以 0 表示）。
        candidate_codes = codes or list(plan_map.keys())
        begin_date = (base_date - timedelta(days=180)).strftime('%Y-%m-%d')
        end_date = (base_date + timedelta(days=90)).strftime('%Y-%m-%d')
        impl_map = await self._fetch_impl_dates(
            candidate_codes, begin_date, end_date, batch_size)

        # 用户硬约束：分红日期必须 >= 明天（今天/已过一律剔除）
        tomorrow_str = (base_date + timedelta(days=1)).strftime('%Y-%m-%d')
        results = []
        # 1. 主源命中：副源有真实除权日则覆盖估算值
        # 关键：主源估算的除权日 est_div = REPORTDATE + 75 天，但 REPORTDATE 是财报周期（如
        # 半年报 6-30）→ est_div 9-13 < 真实 9-17，所以"div_date >= base_str"会误删。
        # 放宽为 div_date >= base_date - 180 天（半年内）的方案都保留，
        # 让副源的真实除权日（最近 90 天）能覆盖替换这些估算值。
        # 最终由"div_date >= tomorrow"硬约束统一把关（主源/副源共用）。
        cutoff = (base_date - timedelta(days=180)).strftime('%Y-%m-%d')
        # 每股分红统一硬约束：> 0.2 元（含税）。此处提前剔除可避免 UI 侧再为
        # 这些股票拉行情/概念/财务（白花几百次请求）。
        MIN_PER_SHARE = 0.2
        for code, item in plan_map.items():
            if code in impl_map:
                item['div_date'] = impl_map[code]['div_date']
                item['date_real'] = True
                # 主源金额缺失（0）时用公告正文提取的金额补上
                if item.get('per_share', 0) <= 0 and impl_map[code].get('per_share', 0) > 0:
                    item['per_share'] = impl_map[code]['per_share']
            if item['div_date'] < cutoff:
                continue
            if item['div_date'] < tomorrow_str:
                continue
            if item.get('per_share', 0) <= MIN_PER_SHARE:
                continue
            results.append(item)
        # 2. 副源独有：主源缺失但公告接口有实施公告的股票
        #    金额取公告正文提取值（amount_from_notice 标记来源，UI 侧据此
        #    在"含税/扣税后"列显示 '-'，因为公告无个人投资者扣税口径）
        for code, impl in impl_map.items():
            if code in plan_map:
                continue
            if impl['div_date'] < tomorrow_str:
                continue
            _ps = impl.get('per_share', 0.0)
            if _ps <= MIN_PER_SHARE:
                continue
            results.append({
                'code': code,
                'name': impl['name'],
                'div_date': impl['div_date'],
                'per_share': _ps,
                'per_share_after_tax': 0.0,   # 公告扣税后口径多为港资税率，不采用
                'bonus_share': impl.get('bonus_share', 0.0),
                'amount_from_notice': True,
                'plan': '权益分派实施（每10股派%.2f元）' % (_ps * 10),
                'date_real': True,
            })
        return results

    cls._fetch_impl_dates = _fetch_impl_dates
    # 关键：_fetch_real_ex_date 是 _fetch_impl_dates 内部调用 self._fetch_real_ex_date 时查找的辅助方法，
    # 必须显式绑定到类上，否则 self 上找不到该属性会 AttributeError
    cls._fetch_real_ex_date = _fetch_real_ex_date
    cls.fetch_dividend_calendar = _fetch_dividend_calendar

    # ============ Patch 6: 板块概念过滤与主页列表统一 ============
    # 原版 fetch_stock_concepts 只用 _CONCEPT_FILTER（约 100 个风格标签）过滤，
    # 而主页列表（stock_list_updater._format_sector）此前用另一套
    # _STYLE_TAGS/_STYLE_RE，且格式为 '行业|概念1,概念2'、截断 8 个，
    # 导致同一只股票两侧显示不一致。现统一走 concept_filter（合并两套
    # 规则 + 正则），格式统一为 '、' 连接、不截断。
    orig_fetch_stock_concepts = cls.fetch_stock_concepts

    async def _patched_fetch_stock_concepts(self, code):
        con = await orig_fetch_stock_concepts(self, code)
        if not con:
            return con
        return filter_concepts(con)

    cls.fetch_stock_concepts = _patched_fetch_stock_concepts

    logger.info('[patch] data_fetcher 补丁已应用（Patch1-6）')
