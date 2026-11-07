# Decompiled with PyLingual (https://pylingual.io)
# Internal filename: 'C:\\Users/Jorin/CodeBuddy/20260804122338/stock_app\\data_fetcher.py'
# Bytecode version: 3.11a7e (3495)
# Source timestamp: 2026-08-09 02:56:43 UTC (1786244203)

"""\n多源异步数据获取器 - 腾讯 + 东方财富 + 百度\n可用数据源:\n- 腾讯 qt.gtimg.cn: 实时行情、分时图、日/周/月K线、盘口\n- 东方财富 datacenter: 财务指标、大宗交易、股票列表\n- 东方财富 push2: 新闻资讯、股票列表\n- 百度 pae.baidu.com: 备用分时数据\n"""
import sys
import asyncio
import aiohttp
import json
import time
import hashlib
import os
import re
import random
import logging
from typing import Optional, Dict, List
if sys.platform == 'win32':
    if not isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
def new_async_loop():
    """创建适用于 aiohttp 的 asyncio 事件循环。\n    Windows 上 ProactorEventLoop 不支持 add_reader/add_writer，\n    会导致 aiohttp 回退到极慢的轮询模式。这里显式创建 SelectorEventLoop，\n    彻底避免 PyInstaller 打包后导入顺序差异导致策略失效的问题。\n    """
    if sys.platform == 'win32':
        try:
            from asyncio.windows_events import SelectorEventLoop
            return SelectorEventLoop()
        except ImportError:
            pass
    return asyncio.new_event_loop()
from config import CACHE_DIR, CACHE_TTL, MAX_CONCURRENT, REQUEST_TIMEOUT, EXCLUDE_PREFIXES, EXCLUDE_KEYWORDS, MARKET_CAP_THRESHOLD, REQUEST_DELAY
from tdx_fetcher import get_stock_list as _tdx_stock_list
from tdx_fetcher import get_quotes as _tdx_quotes
from tdx_fetcher import get_kline as _tdx_kline
from tdx_fetcher import is_available as _tdx_available
logging.basicConfig(level=logging.WARNING, format='[API] %(levelname)s: %(message)s')
logger = logging.getLogger('api')
UA_LIST = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36']
def random_ua():
    return random.choice(UA_LIST)
_REFERER_MAP = {'gtimg.cn': 'https://gu.qq.com/', 'eastmoney.com': 'https://quote.eastmoney.com/', 'emdatah5.eastmoney.com': 'https://emdatah5.eastmoney.com/dc/'}
def _default_headers(url=''):
    """构建默认请求头，自动附加 Referer"""
    h = {'User-Agent': random_ua()}
    if url:
        for domain, ref in _REFERER_MAP.items():
            if domain in url:
                h['Referer'] = ref
                break
    return h
def _strip_news_html(text):
    """去除新闻 HTML 标签与空白，压缩多余换行"""
    if not text:
        return ''
    else:
        t = re.sub('<[^>]+>', '', text)
        t = re.sub('\\s+', ' ', t)
        return t.strip()
def _prefix(code):
    return 'sh' if code.startswith(('6', '9')) else 'sz'
def _em_secid(code):
    """东方财富 secid: 1.SHCODE 或 0.SZCODE"""
    m = '1' if code.startswith(('6', '9')) else '0'
    return f'{m}.{code}'
def _normalize_code_em(code):
    """把任意格式股票代码规范化为东财 F10 接口需要的 SH600265 / SZ000001 格式。\n\n    支持 600265、SH600265、sh600265、1.600265、0.000001、600265.SH、\n    600265.SZ 等常见写法；无法提取 6 位数字时返回空串（调用方应跳过）。\n    """
    if code is None:
        return ''
    else:
        s = str(code).strip()
        m = re.search('(\\d{6})', s)
        if not m:
            return ''
        else:
            num = m.group(1)
            up = s.upper()
            if up.startswith('SH') or up.startswith('1.') or up.endswith('.SH'):
                return 'SH' + num
            else:
                if up.startswith('SZ') or up.startswith('0.') or up.endswith('.SZ'):
                    return 'SZ' + num
                else:
                    return ('SH' if num.startswith(('6', '9')) else 'SZ') + num
def should_exclude_stock(code: str, name: str, market_cap: float=0) -> bool:
    for p in EXCLUDE_PREFIXES:
        if code.startswith(p):
            return True
    for kw in EXCLUDE_KEYWORDS:
        if kw in name:
            return True
    if market_cap > 0 and market_cap < MARKET_CAP_THRESHOLD:
            return True
    return False
class MultiSourceFetcher:
    def __init__(self):
        self.session = None
        _tdx_ok = _tdx_available()
        _loop_type = type(asyncio.get_event_loop()).__name__ if hasattr(asyncio, 'get_event_loop') else 'N/A'
        logger.info(f"Fetcher init — TDX={('OK' if _tdx_ok else 'OFF')} loop={_loop_type}")
    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT, limit_per_host=8, ssl=False, ttl_dns_cache=300, force_close=True)
        self.session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
        self._bg_tasks = []
        return self
    async def __aexit__(self, *args):
        for t in getattr(self, '_bg_tasks', []):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._bg_tasks.clear()
        if self.session:
            await self.session.close()
    def _cache_key(self, prefix, *args):
        return hashlib.md5(f"{prefix}_{'_'.join((str(a) for a in args))}".encode()).hexdigest()

    def _cache_get(self, key, ttl=None):
        f = os.path.join(CACHE_DIR, f'{key}.json')
        if not os.path.exists(f):
            return None
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug('[cache] 读取缓存失败 key=%s: %s', key, e)
            return None
        if time.time() - d.get('ts', 0) < (ttl if ttl is not None else CACHE_TTL):
            return d.get('value')
        return None

    def _cache_set(self, key, value, ttl=None):
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            path = os.path.join(CACHE_DIR, f'{key}.json')
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump({'ts': time.time(), 'value': value, 'ttl': ttl}, fh, ensure_ascii=False)
        except (OSError, TypeError) as e:
            logger.debug('[cache] 写入缓存失败 key=%s: %s', key, e)
            return None
    async def _fetch(self, url, encoding='utf-8', extra_headers=None):
        """GET 请求，自动带 Referer，返回解码文本"""
        if not self.session:
            return None
        h = _default_headers(url)
        if extra_headers:
            h.update(extra_headers)
        try:
            async with self.session.get(url, headers=h) as r:
                if r.status != 200:
                    logger.warning(f'HTTP {r.status}: {url}')
                    return None
                raw = await r.read()
                for enc in [encoding, 'gbk', 'gb2312', 'utf-8']:
                    try:
                        return raw.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return raw.decode('utf-8', errors='ignore')
        except asyncio.TimeoutError:
            logger.warning(f'Timeout: {url}')
            return None
        except Exception as e:
            logger.warning(f'Fetch error: {e} | {url}')
            return None
    async def _fetch_silent(self, url, encoding='utf-8', extra_headers=None, silence_5xx=False):
        """GET 请求（静默版）：非 200 时不打 WARNING，仅 >=500 且 !silence_5xx 时记录一次 info"""
        if not self.session:
            return None
        h = _default_headers(url)
        if extra_headers:
            h.update(extra_headers)
        try:
            async with self.session.get(url, headers=h) as r:
                if r.status != 200:
                    if r.status >= 500 and not silence_5xx:
                        logger.info(f'HTTP {r.status} (silent): {url}')
                    return None
                raw = await r.read()
                for enc in [encoding, 'gbk', 'gb2312', 'utf-8']:
                    try:
                        return raw.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return raw.decode('utf-8', errors='ignore')
        except Exception:
            return None
    async def _fetch_json(self, url, extra_headers=None, timeout=None):
        """GET 请求，返回 JSON（兼容 text/plain 等非标准 Content-Type 和 JSONP）"""
        if not self.session:
            return None
        h = _default_headers(url)
        if extra_headers:
            h.update(extra_headers)
        kwargs = {'headers': h}
        if timeout is not None:
            kwargs['timeout'] = aiohttp.ClientTimeout(total=timeout)
        try:
            async with self.session.get(url, **kwargs) as r:
                if r.status != 200:
                    logger.warning(f'HTTP {r.status}: {url}')
                    return None
                text = await r.text()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                m = re.match(r'^\s*[\w.]+\s*\((.*)\)\s*$', text, re.DOTALL)
                if m:
                    return json.loads(m.group(1))
                logger.warning(f'JSON parse error: {url}')
                return None
        except asyncio.TimeoutError:
            logger.debug(f'Timeout: {url[:60]}')
            return None
        except Exception as e:
            msg = str(e)[:80]
            if 'remote' not in msg.lower():
                logger.debug(f'Fetch JSON error: {msg} | {url}')
            return None
    async def fetch_stock_list(self, page_count=40):
        ck = self._cache_key('stock_list', page_count)
        if (c := self._cache_get(ck)):
            return c
        else:
            stale = self._cache_get(ck, ttl=float('inf'))
            stocks = []
            if _tdx_available():
                _tdx_t0 = time.time()
                tdx_stocks = await asyncio.to_thread(_tdx_stock_list)
                _tdx_dt = (time.time() - _tdx_t0) * 1000
                logger.info(f'TDX stocks() 耗时: {_tdx_dt:.0f}ms, 结果: {(len(tdx_stocks) if tdx_stocks else 0)} 条')
                if tdx_stocks:
                    stocks = tdx_stocks
                    self._cache_set(ck, stocks)
                    task = asyncio.create_task(self._supplement_and_recache(ck, stocks, page_count))
                    self._bg_tasks.append(task)
                    return stocks
            stocks = await self._fetch_stock_list_eastmoney()
            if not stocks:
                stocks = await self._fetch_stock_list_sina(page_count)
            if not stocks and stale is not None:
                logger.info('所有股票列表 API 均失败，使用过期缓存')
                return stale
            else:
                if stocks:
                    self._cache_set(ck, stocks)
                    logger.info(f'股票列表加载完成: {len(stocks)} 支')
                return stocks
    async def _supplement_and_recache(self, ck, current_stocks, page_count):
        """后台任务：从东财补充深市股票，合并后更新缓存"""
        try:
            em_stocks = await self._fetch_stock_list_eastmoney()
            if not em_stocks:
                em_stocks = await self._fetch_stock_list_sina(page_count)
            if em_stocks:
                existing = {s['code'] for s in current_stocks}
                added = 0
                for s in em_stocks:
                    if s['code'] not in existing:
                        current_stocks.append(s)
                        added += 1
                if added > 0:
                    self._cache_set(ck, current_stocks)
                    logger.info(f'东财后台补充: +{added} 支，合并后共 {len(current_stocks)} 支')
        except Exception as e:
            logger.debug(f'后台补充股票列表异常: {e}')
    async def _fetch_stock_list_eastmoney(self):
        """使用东方财富 API 获取股票列表（更稳定）\n        优化：沪深两个市场并发请求，每页 150 条降低总页数，减少延迟等待\n        """
        bases = ['https://push2delay.eastmoney.com', 'https://82.push2delay.eastmoney.com', 'https://72.push2delay.eastmoney.com', 'https://44.push2delay.eastmoney.com']
        async def _fetch_market(fs_val):
            # irreducible cflow, using cdg fallback
            stocks = []
            page = 1
            fail_streak = 0
            while True:
                base = bases[(page + fail_streak) % len(bases)]
                url = f'{base}/api/qt/clist/get?pn={page}&pz=100&po=0&np=1&fltt=2&invt=2&fid=f12&fs={fs_val}&fields=f2,f3,f4,f5,f9,f12,f14,f15,f16,f17,f18,f20,f21,f100'
                data = await self._fetch_json(url, timeout=5)
                if not data or not data.get('data'):
                    fail_streak += 1
                    if fail_streak >= 3:
                        break
                    else:
                        await asyncio.sleep(0.5)
                        continue
                else:
                    fail_streak = 0
                    d = data['data']
                    items = d.get('diff', [])
                    if not items:
                        break
                    for item in items:
                        code = item.get('f12', '')
                        name = item.get('f14', '')
                        if not (code and name and str(code).isdigit() and (len(str(code)) == 6)):
                            continue
                        else:
                            mc_val = item.get('f20') or 0
                            try:
                                mc = float(mc_val) / 100000000.0 if mc_val not in (0, '-', '') else 0
                            except (ValueError, TypeError):
                                mc = 0
                            pe_val = item.get('f9') or 0
                            try:
                                pe = float(pe_val) if pe_val else 0
                            except (ValueError, TypeError):
                                pe = 0
                            if should_exclude_stock(code, name, mc):
                                continue
                            else:
                                stocks.append({'code': code, 'name': name, 'market_cap': mc, 'pe': pe, 'sector': item.get('f100', '')})
                    total = d.get('total', 0)
                    if page * 100 >= total:
                        break
                    page += 1
                    await asyncio.sleep(0.05)
            return stocks
        sh_task = asyncio.create_task(_fetch_market('m:0+t:6,m:0+t:80'))
        sz_task = asyncio.create_task(_fetch_market('m:0+t:13,m:0+t:81'))
        sh_stocks, sz_stocks = await asyncio.gather(sh_task, sz_task)
        return sh_stocks + sz_stocks
    async def _fetch_stock_list_sina(self, page_count=40):
        """Sina API 获取股票列表（备用方案）"""
        stocks = []
        for market, node in [('sh', 'sh_a'), ('sz', 'sz_a')]:
            fail_count = 0
            for page in range(1, page_count + 1):
                if fail_count >= 2:
                    logger.info(f'Sina {node}: 连续失败，跳过剩余页')
                    break
                else:
                    url = f'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node={node}'
                    t = await self._fetch(url, encoding='gbk')
                    if t:
                        fail_count = 0
                        try:
                            for item in json.loads(t):
                                code = item.get('code', '')
                                name = item.get('name', '')
                                mc = float(item.get('mktcap', 0) or 0) / 10000
                                if should_exclude_stock(code, name, mc):
                                    continue
                                else:
                                    stocks.append({'code': code, 'name': name, 'market_cap': mc, 'pe': float(item.get('per', 0) or 0), 'sector': item.get('symbol', '')})
                        except Exception as e:
                            logger.warning(f'Parse stock list page {page}: {e}')
                    else:
                        fail_count += 1
                    await asyncio.sleep(REQUEST_DELAY)
        return stocks
    async def search_stock_suggest(self, keyword):
        """多源搜索建议：东方财富 → 腾讯 → 东财完整列表本地匹配 → 拼音数据兜底"""
        stocks = await self._search_stock_eastmoney(keyword)
        if stocks:
            return stocks
        else:
            stocks = await self._search_stock_tencent(keyword)
            if stocks:
                return stocks
            else:
                logger.info('在线 API 无结果，直接拉取东财完整列表进行本地匹配...')
                em_stocks = await self._fetch_stock_list_eastmoney()
                if em_stocks:
                    matched = match_stock_by_pinyin(em_stocks, keyword)
                    if matched:
                        return matched
                logger.info('完整列表匹配失败，使用拼音字典兜底搜索...')
                return await self._search_by_pinyin_dict(keyword)
    async def _search_stock_eastmoney(self, keyword):
        """东方财富搜索建议 API"""
        result = await self._fetch_json(f'https://searchadapter.eastmoney.com/api/suggest/get?input={keyword}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=20', timeout=8)
        if not result:
            return []
        else:
            try:
                items = result.get('QuotationCodeTable', {}).get('Data', [])
            except Exception:
                return []
            if not items or not isinstance(items, list):
                return []
            else:
                stocks = []
                for item in items:
                    code = item.get('Code', '')
                    name = item.get('Name', '')
                    if not (code and name and str(code).isdigit() and (len(str(code)) == 6)):
                        continue
                    else:
                        market = item.get('MktNum', '')
                        if market not in ['0', '1', '2', '']:
                            continue
                        else:
                            if should_exclude_stock(code, name, 0):
                                continue
                            else:
                                stocks.append({'code': code, 'name': name, 'market_cap': 0, 'pe': 0, 'sector': ''})
                return stocks
    async def _search_stock_tencent(self, keyword):
        """腾讯智能搜索 API (smartbox.gtimg.cn)，返回 A 股匹配结果"""
        import urllib.parse
        import re
        try:
            text = await self._fetch(f'https://smartbox.gtimg.cn/s3/?q={urllib.parse.quote(keyword)}&t=all&count=30', encoding='gbk')
        except Exception as e:
            logger.warning(f'腾讯搜索异常: {e}')
            return []
        if not text:
            return []
        else:
            m = re.search('v_hint=\"([^\"]*)\"', text)
            if not m or m.group(1) == 'N':
                return []
            else:
                stocks = []
                for entry in m.group(1).split('^'):
                    parts = entry.split('~')
                    if len(parts) < 3:
                        continue
                    else:
                        code = parts[1]
                        name = parts[2]
                        try:
                            name = name.encode('utf-8').decode('unicode_escape')
                        except Exception:
                            pass
                        if not str(code).isdigit() or not len(str(code)) == 6 or (not name):
                            continue
                        else:
                            if len(parts) >= 5 and parts[4] != 'GP-A':
                                    continue
                            if should_exclude_stock(code, name, 0):
                                continue
                            else:
                                stocks.append({'code': code, 'name': name, 'market_cap': 0, 'pe': 0, 'sector': ''})
                return stocks
    async def _search_by_pinyin_dict(self, keyword):
        """终极兜底：用完整股票列表（含深市）做拼音匹配，绕过不完整缓存"""
        kw = keyword.strip().lower()
        if not kw:
            return []
        for key, (data, _, _) in list(getattr(self, '_cache', {}).items()):
            if 'stock_list' in key and isinstance(data, list) and (len(data) > 500):
                sz_count = sum((1 for s in data if str(s.get('code', '')).startswith(('00', '30'))))
                if sz_count > 100:
                    return match_stock_by_pinyin(data, keyword)
        try:
            stocks = await self._fetch_stock_list_eastmoney()
        except Exception:
            stocks = []
        if not stocks:
            try:
                stocks = await self._fetch_stock_list_sina(40)
            except Exception:
                stocks = []
        if stocks:
            try:
                ck = self._cache_key('stock_list', 40)
                self._cache_set(ck, stocks)
            except Exception:
                pass
            return match_stock_by_pinyin(stocks, keyword)
        else:
            return []
    async def fetch_real_time_quotes(self, codes):
        """腾讯 qt.gtimg.cn 实时行情（已移除新浪，避免 IP 封禁）"""
        if not codes:
            return {}
        result = {}
        for i in range(0, len(codes), 50):
            batch = codes[i:i + 50]
            tc = [f'{_prefix(c)}{c}' for c in batch]
            t = await self._fetch(f"http://qt.gtimg.cn/q={','.join(tc)}", encoding='gbk')
            if t:
                for line in t.strip().split('\n'):
                    if '=' not in line:
                        continue
                    try:
                        _, vs = line.split('=', 1)
                        vs = vs.strip('"')
                        v = vs.split('~')
                        if len(v) < 50:
                            continue
                        c = v[2]
                        yc = float(v[4]) if v[4] else 0
                        cur = float(v[3]) if v[3] else 0
                        op = float(v[5]) if v[5] else 0
                        vol = float(v[6]) if v[6] else 0
                        lo = float(v[34]) if len(v) > 34 and v[34] else 0
                        chg_pct = round((cur - yc) / yc * 100, 2) if yc else 0.0
                        # 腾讯 gtimg v[] 真实布局（实测核对 600519/000001/300750）：
                        #   v[3]=现价 v[4]=昨收 v[5]=今开 v[6]=成交量(手)
                        #   v[34]=最低价 v[35]="最高/成交量/成交额"组合串
                        #   v[38]=换手率(%) v[39]=市盈率(TTM) v[43]=量比
                        #   v[44]=总市值(亿元) v[46]=市净率
                        parts35 = v[35].split('/') if len(v) > 35 and v[35] else []
                        hi = float(parts35[0]) if len(parts35) > 0 and parts35[0] else 0
                        amount = float(parts35[2]) if len(parts35) > 2 and parts35[2] else (cur * vol * 100)
                        result[c] = {
                            'name': v[1],
                            'code': c,
                            'current': cur,
                            'yesterday_close': yc,
                            'open': op,
                            'high': hi,
                            'low': lo,
                            'volume': vol,
                            'amount': amount,
                            'change_pct': chg_pct,
                            'turnover_rate': float(v[38]) if len(v) > 38 and v[38] else 0,
                            'pe': float(v[39]) if len(v) > 39 and v[39] else 0,
                            'market_cap': float(v[44]) if len(v) > 44 and v[44] else 0,
                            'volume_ratio': float(v[43]) if len(v) > 43 and v[43] else 0,
                            'buy_prices': [],
                            'buy_volumes': [],
                            'sell_prices': [],
                            'sell_volumes': [],
                        }
                    except Exception as e:
                        logger.warning(f'Parse realtime quote: {e}')
            await asyncio.sleep(REQUEST_DELAY)
        if not result and _tdx_available():
            logger.info('腾讯实时行情为空，尝试通达信...')
            try:
                result = await asyncio.to_thread(_tdx_quotes, codes)
            except Exception as e:
                logger.warning(f'TDX 实时行情失败: {e}')
        return result
    async def fetch_intraday_data(self, code):
        ck = self._cache_key('intraday', code)
        if (c := self._cache_get(ck, ttl=8)):
            return c
        else:
            pf = _prefix(code)
            t = await self._fetch(f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={pf}{code}')
            result = {'code': code, 'minute_data': [], 'auction_data': [], 'buy_levels': [], 'sell_levels': [], 'order_book': {}}
            if t:
                self._parse_minute_response(t, pf, code, result)
            if result.get('auction_data') or result.get('minute_data') or result.get('current'):
                self._cache_set(ck, result, ttl=8)
                return result
            else:
                logger.warning(f'intraday no minute data: {code}')
    def _parse_minute_response(self, t, pf, code, result):
        """解析腾讯分时API响应。同时从qt字段提取行情数据(昨收/当前价/盘口)"""
        if '=' not in t:
            return
        try:
            _, js = t.split('=', 1)
            data = json.loads(js)
        except (ValueError, TypeError):
            return
        if 'data' not in data or pf + code not in data.get('data', {}):
            return
        try:
            sd = data['data'][pf + code]
            auction_node = sd.get('auction', None)
            auction_raw = None
            if isinstance(auction_node, list):
                auction_raw = auction_node
            elif isinstance(auction_node, dict):
                a_data = auction_node.get('data', None)
                if isinstance(a_data, list):
                    auction_raw = a_data
                elif isinstance(a_data, str):
                    auction_raw = a_data.strip()
                elif isinstance(a_data, dict):
                    raw = a_data.get('data', '')
                    if isinstance(raw, str):
                        auction_raw = raw.strip()
            if auction_raw is not None:
                self._parse_auction_data(auction_raw, result)
                if result['auction_data']:
                    logger.debug(f"auction parsed {len(result['auction_data'])} pts via {type(auction_raw).__name__}")
                else:
                    logger.debug(f"auction raw type={type(auction_raw).__name__} len={(len(auction_raw) if isinstance(auction_raw, (list, str)) else '?')}, parsed empty")
            else:
                logger.debug(f'auction field type={type(auction_node).__name__} value={auction_node}')
            md_raw = sd.get('data', {}).get('data', '')
            if isinstance(md_raw, list):
                for item_str in md_raw:
                    parts = item_str.split()
                    if len(parts) >= 3 and parts[0].isdigit():
                        t_int = int(parts[0])
                        if 915 <= t_int <= 1530:
                            result['minute_data'].append({'time': f'{t_int:04d}', 'price': float(parts[1]), 'volume': float(parts[2])})
            elif isinstance(md_raw, str) and md_raw.strip():
                parts = md_raw.split()
                for i in range(0, len(parts), 4):
                    if i + 2 < len(parts) and parts[i].isdigit():
                        t_int = int(parts[i])
                        if 915 <= t_int <= 1530:
                            result['minute_data'].append({'time': f'{t_int:04d}', 'price': float(parts[i + 1]), 'volume': float(parts[i + 2])})
            if not result['auction_data']:
                result['auction_data'] = [d for d in result['minute_data'] if '0915' <= d.get('time', '') <= '0925']
            qt_raw = sd.get('qt', {})
            if isinstance(qt_raw, list) and len(qt_raw) > 5:
                sd_for_qt = {'qt': {pf + code: qt_raw}}
            else:
                sd_for_qt = sd
            self._parse_qt_data(sd_for_qt, pf, code, result)
            if not result['auction_data']:
                yc = result.get('yesterday_close', 0)
                op = result.get('open', 0)
                if op > 0:
                    result['auction_data'] = [
                        {'time': '0915', 'price': yc if yc > 0 else op, 'volume': 0},
                        {'time': '0925', 'price': op, 'volume': 0},
                    ]
        except Exception as e:
            logger.warning(f'intraday parse error: {e} | {code}')
    def _parse_auction_data(self, raw, result):
        """解析竞价数据：支持 字符串/列表/嵌套列表 多种格式。统一 time 为 HHMM 字符串。"""
        items = []
        if isinstance(raw, str):
            parts = raw.split()
            items = [(parts[i], float(parts[i + 1]), float(parts[i + 2])) for i in range(0, len(parts) - 2, 3) if parts[i].isdigit()]
        else:
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str):
                        p = item.split()
                        if len(p) >= 3 and p[0].isdigit():
                                items.append((p[0], float(p[1]), float(p[2])))
                    else:
                        if isinstance(item, (list, tuple)) and len(item) >= 3:
                            items.append((str(item[0]), float(item[1]), float(item[2])))
                        else:
                            if isinstance(item, dict):
                                t = item.get('time', item.get('t', item.get('minute', '')))
                                p = item.get('price', item.get('p', 0))
                                v = item.get('volume', item.get('v', item.get('vol', 0)))
                                items.append((str(t), float(p), float(v)))
        for t, price, vol in items:
            try:
                t_norm = f'{int(t):04d}'
            except (ValueError, TypeError):
                t_norm = str(t)
            result['auction_data'].append({'time': t_norm, 'price': price, 'volume': vol})
    def _parse_qt_data(self, sd, pf, code, result):
        """从腾讯分时API的qt字段提取完整行情数据"""
        qt_data = sd.get('qt', {})
        qt_list = qt_data.get(pf + code, [])
        if not isinstance(qt_list, list) or len(qt_list) < 49:
            return None
        else:
            try:
                cur = float(qt_list[3]) if qt_list[3] else 0
                yc = float(qt_list[4]) if qt_list[4] else 0
                op = float(qt_list[5]) if qt_list[5] else 0
                result['current'] = cur
                result['yesterday_close'] = yc
                result['open'] = op
                if yc > 0 and cur > 0:
                        result['change_pct'] = (cur - yc) / yc * 100
                bl, sl = ([], [])
                for j in range(5):
                    bp_idx, bv_idx = (9 + j * 2, 10 + j * 2)
                    sp_idx, sv_idx = (19 + j * 2, 20 + j * 2)
                    if bp_idx < len(qt_list) and qt_list[bp_idx]:
                            bp = float(qt_list[bp_idx])
                            bv = float(qt_list[bv_idx]) if bv_idx < len(qt_list) and qt_list[bv_idx] else 0
                            if bp > 0:
                                bl.append({'price': bp, 'volume': bv})
                    if sp_idx < len(qt_list) and qt_list[sp_idx]:
                            sp = float(qt_list[sp_idx])
                            sv = float(qt_list[sv_idx]) if sv_idx < len(qt_list) and qt_list[sv_idx] else 0
                            if sp > 0:
                                sl.append({'price': sp, 'volume': sv})
                result['buy_levels'] = bl
                result['sell_levels'] = sl
                result['order_book'] = {'buy_levels': bl, 'sell_levels': sl}
                info = result.setdefault('stock_info', {})
                info['current'] = cur
                info['yesterday_close'] = yc
                info['open'] = float(qt_list[5]) if qt_list[5] else 0
                info['volume'] = float(qt_list[6]) if qt_list[6] else 0
                info['change_pct'] = result.get('change_pct', 0)
                info['high'] = float(qt_list[33]) if qt_list[33] else 0
                info['low'] = float(qt_list[34]) if qt_list[34] else 0
                info['amount'] = float(qt_list[37]) if qt_list[37] else 0
                info['turnover_rate'] = float(qt_list[38]) if qt_list[38] else 0
                info['amplitude'] = float(qt_list[43]) if qt_list[43] else 0
                info['pe'] = float(qt_list[44]) if qt_list[44] else 0
                info['volume_ratio'] = float(qt_list[46]) if qt_list[46] else 0
                info['limit_up'] = float(qt_list[47]) if qt_list[47] else 0
                info['limit_down'] = float(qt_list[48]) if qt_list[48] else 0
                info['market_cap'] = float(qt_list[58]) if len(qt_list) > 58 and qt_list[58] else 0
            except Exception as e:
                logger.warning(f'qt parse error: {e} | {code}')
    async def fetch_kline_data(self, code, period='daily', count=250):
        ck = self._cache_key('kline', code, period, count)
        if (c := self._cache_get(ck)):
            return c
        else:
            result = await self._fetch_tencent_kline(code, period, count)
            if not result:
                result = await self._fetch_eastmoney_kline(code, period, count)
            if not result and _tdx_available():
                    try:
                        tdx_result = await asyncio.to_thread(_tdx_kline, code, period, count)
                        if tdx_result:
                            result = tdx_result
                            logger.debug(f'TDX K线兜底成功: {code}')
                    except Exception as e:
                        logger.warning(f'TDX K线失败 ({code}): {e}')
            if result:
                self._cache_set(ck, result)
            return result if result else None
    async def _fetch_tencent_kline(self, code, period, count):
        pf = _prefix(code)
        pm = {'day': 'day', 'week': 'week', 'month': 'month'}
        p = pm.get(period, 'day')
        import asyncio
        urls = [f'http://ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline&param={pf}{code},{p},,,{count},qfq', f'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline&param={pf}{code},{p},,,{count},qfq']
        for url in urls:
            r = await self._fetch_silent(url, silence_5xx=True)
            if r:
                t = r
                break
        else:
            return None
        result = []
        try:
            if '=' in t:
                _, js = t.split('=', 1)
                data = json.loads(js)
                if 'data' in data and pf + code in data['data']:
                        kd = data['data'][pf + code]
                        key = 'qfqday' if period == 'daily' else period
                        if key in kd:
                            for item in kd[key]:
                                if len(item) >= 6:
                                    result.append({'date': item[0], 'open': float(item[1]), 'close': float(item[2]), 'high': float(item[3]), 'low': float(item[4]), 'volume': float(item[5])})
        except Exception as e:
            logger.warning(f'tencent kline parse: {e}')
        return result
    async def _fetch_eastmoney_kline(self, code, period, count):
        """东方财富 K线 (最后回退) — 包含成交金额和换手率"""
        secid = _em_secid(code)
        pm = {'daily': 101, 'weekly': 102, 'monthly': 103}
        klt = pm.get(period, 101)
        url = f'https://push2delay.eastmoney.com/api/qt/stock/kline/get?fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&ut=fa5fd1943de7a2d47e4a17b6e6a3e0b0&klt={klt}&fqt=1&secid={secid}&beg=19900101&end=20990101&lmt={count}'
        data = await self._fetch_json(url)
        if not data or not data.get('data'):
            return None
        else:
            result = []
            try:
                klines = data['data'].get('klines', [])
                for item in klines:
                    parts = item.split(',')
                    if len(parts) >= 6:
                        k = {'date': parts[0], 'open': float(parts[1]), 'close': float(parts[2]), 'high': float(parts[3]), 'low': float(parts[4]), 'volume': float(parts[5])}
                        if len(parts) >= 7:
                            k['amount'] = float(parts[6]) if parts[6] else 0
                        if len(parts) >= 11:
                            k['turnover'] = float(parts[10]) if parts[10] else 0
                        result.append(k)
            except Exception as e:
                logger.warning(f'eastmoney kline parse: {e}')
            return result
    async def fetch_price_changes(self, code):
        klines = await self.fetch_kline_data(code, count=30)
        if not klines:
            return {'change_5d': 0, 'change_10d': 0, 'change_20d': 0}
        else:
            closes = [k['close'] for k in klines]
            r = {}
            r['change_5d'] = (closes[(-1)] - closes[(-6)]) / closes[(-6)] * 100 if len(closes) >= 6 else 0
            r['change_10d'] = (closes[(-1)] - closes[(-11)]) / closes[(-11)] * 100 if len(closes) >= 11 else 0
            r['change_20d'] = (closes[(-1)] - closes[(-21)]) / closes[(-21)] * 100 if len(closes) >= 21 else 0
            return r
    async def fetch_price_range(self, code):
        """计算 T+1 买卖区间，基于20日K线支撑/阻力位\n\n        策略逻辑：\n        - 买入区间: 支撑位上方（20日低点 ~ 20日均价下方 1%），稳健入场\n        - 卖出区间: 阻力位下方（20日均价上方 1% ~ 20日高点），合理止盈\n        返回: {\'buy_low\': float, \'buy_high\': float, \'sell_low\': float, \'sell_high\': float}\n        """
        klines = await self.fetch_kline_data(code, count=30)
        if not klines or len(klines) < 20:
            return {'buy_low': 0, 'buy_high': 0, 'sell_low': 0, 'sell_high': 0}
        else:
            recent = klines[(-20):]
            lows = [k['low'] for k in recent]
            highs = [k['high'] for k in recent]
            closes = [k['close'] for k in recent]
            avg_close = sum(closes) / len(closes)
            low_20 = min(lows)
            high_20 = max(highs)
            current = closes[(-1)]
            buy_low = round(low_20 * 1.005, 2)
            buy_high = round(avg_close * 0.99, 2)
            sell_low = round(avg_close * 1.01, 2)
            sell_high = round(high_20 * 0.995, 2)
            if buy_low >= buy_high:
                buy_high = round(buy_low * 1.02, 2)
            if sell_low >= sell_high:
                sell_high = round(sell_low * 1.03, 2)
            return {'buy_low': buy_low, 'buy_high': buy_high, 'sell_low': sell_low, 'sell_high': sell_high}
    async def fetch_money_flow(self, code):
        ck = self._cache_key('flow', code)
        if (c := self._cache_get(ck)):
            return c
        else:
            result = {'main': 0, 'super_large': 0, 'large': 0, 'medium': 0, 'small': 0, 'retail': 0, 'main_ratio': 0, 'super_large_ratio': 0, 'large_ratio': 0, 'medium_ratio': 0, 'small_ratio': 0, 'retail_ratio': 0, 'main_net_5d': 0, 'main_net_10d': 0, 'main_net_20d': 0, 'data_available': False}
            secid = _em_secid(code)
            base_url = 'https://emdatah5.eastmoney.com/dc/ZJLX'
            try:
                dd = await self._fetch_json(f'{base_url}/getDBHistoryData?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56', extra_headers={'Referer': f'https://emdatah5.eastmoney.com/dc/zjlx/stock?fc={secid}'}, timeout=8)
                if dd and dd.get('data') and dd['data'].get('klines'):
                            klines = dd['data']['klines']
                            if klines:
                                latest = klines[(-1)].split(',')
                                if len(latest) >= 6:
                                    result['super_large'] = float(latest[5])
                                    result['large'] = float(latest[4])
                                    result['medium'] = float(latest[3])
                                    result['small'] = float(latest[2])
                                    result['main'] = result['super_large'] + result['large']
                                    result['retail'] = result['medium'] + result['small']
                                    total_abs = abs(result['super_large']) + abs(result['large']) + abs(result['medium']) + abs(result['small'])
                                    if total_abs > 0:
                                        result['main_ratio'] = round(result['main'] / total_abs * 100, 2)
                                        result['super_large_ratio'] = round(result['super_large'] / total_abs * 100, 2)
                                        result['large_ratio'] = round(result['large'] / total_abs * 100, 2)
                                        result['medium_ratio'] = round(result['medium'] / total_abs * 100, 2)
                                        result['small_ratio'] = round(result['small'] / total_abs * 100, 2)
                                        result['retail_ratio'] = round(result['retail'] / total_abs * 100, 2)
                                    result['data_available'] = True
                                net_values = []
                                for item in klines:
                                    parts = item.split(',')
                                    if len(parts) >= 2:
                                        net_values.append(float(parts[1]))
                                if len(net_values) >= 5:
                                    result['main_net_5d'] = sum(net_values[(-5):])
                                if len(net_values) >= 10:
                                    result['main_net_10d'] = sum(net_values[(-10):])
                                if len(net_values) >= 20:
                                    result['main_net_20d'] = sum(net_values[(-20):])
            except Exception as e:
                logger.debug(f'fflow daykline: {e}')
            self._cache_set(ck, result)
            return result
    async def fetch_block_trades(self, code):
        result = []
        import urllib.parse
        try:
            f = urllib.parse.quote(f'(SECURITY_CODE=\"{code}\")')
            url = f'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DATA_BLOCKTRADE&filter={f}&columns=TRADE_DATE,DEAL_PRICE,DEAL_VOLUME,DEAL_AMT,CLOSE_PRICE&pageNumber=1&pageSize=60&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB'
            r = await self._fetch_json(url)
            if r and r.get('success') and r.get('result') and r['result'].get('data'):
                            for item in r['result']['data']:
                                cp = float(item.get('CLOSE_PRICE', 0) or 0)
                                dp = float(item.get('DEAL_PRICE', 0) or 0)
                                dr = (dp / cp - 1) * 100 if cp else 0
                                result.append({'date': (item.get('TRADE_DATE', '') or '')[:10], 'price': dp, 'volume': float(item.get('DEAL_VOLUME', 0) or 0), 'amount': float(item.get('DEAL_AMT', 0) or 0), 'discount_rate': dr})
        except Exception as e:
            logger.debug(f'em block_trades fail: {e}')
        if result:
            ck = self._cache_key('block_trades', code)
            self._cache_set(ck, result)
        return result
    async def fetch_company_notices(self, code, count=30):
        """获取公司公告（东方财富公告中心）"""
        ck = self._cache_key('notices', code, count)
        if (c := self._cache_get(ck)):
            return c
        else:
            import json as _json
            result = []
            org = '0' if code.startswith(('6', '9')) else '1'
            url = f'https://np-anotice-stock.eastmoney.com/api/security/ann?cb=&sr=-1&page_size={count}&page_index=1&ann_type=A&client_source=web&f_node=1&stock_list={org},{code}'
            text = await self._fetch(url, encoding='gbk')
            if not text:
                self._cache_set(ck, result)
                return result
            else:
                try:
                    data = _json.loads(text)
                    items = data.get('data', {}).get('list', [])
                except Exception as e:
                    logger.warning(f'notices parse error: {e}')
                    self._cache_set(ck, result)
                    return result
                for item in items[:count]:
                    result.append({'date': (item.get('notice_date', '') or '')[:10], 'title': (item.get('title', '') or '').replace('<em>', '').replace('</em>', ''), 'type': item.get('ann_type_name', '公告'), 'url': f"https://data.eastmoney.com/notices/detail/{code}/{item.get('art_code', '')}.html"})
                if not result:
                    logger.warning(f'notices empty: {code}')
                self._cache_set(ck, result)
                return result
    async def fetch_stock_news(self, code, count=30):
        """获取个股公告资讯（公告 + 新闻）"""
        ck = self._cache_key('news', code, count)
        if (c := self._cache_get(ck)):
            return c
        else:
            result = await self._fetch_announcements(code, count)
            if result:
                self._cache_set(ck, result)
                return result
            else:
                result = await self._fetch_announcements_search(code, count)
                if result:
                    self._cache_set(ck, result)
                return result
    async def _fetch_announcements(self, code, count=30):
        """东方财富 np-anotice 公告 API"""
        import json
        result = []
        url = f'https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size={count}&page_index=1&ann_type=A&client_source=web&stock_list={code}'
        text = await self._fetch(url, encoding='gbk')
        if not text:
            return []
        else:
            try:
                data = json.loads(text)
                items = data.get('data', {}).get('list', [])
            except Exception:
                return []
            for item in items:
                codes = item.get('codes', [])
                code_info = codes[0] if codes else {}
                art_code = item.get('art_code', '')
                stock_code = code_info.get('stock_code', code)
                result.append({'date': (item.get('notice_date', '') or '')[:10], 'time': '', 'title': item.get('title', ''), 'summary': '', 'url': f'https://data.eastmoney.com/notices/detail/{stock_code}/{art_code}.html' if art_code else ''})
            return result
    async def _fetch_announcements_search(self, code, count=30):
        """东方财富 search-api 搜索公告（兜底）"""
        import json
        import urllib.parse
        result = []
        param = json.dumps({'uid': '', 'keyword': code, 'type': ['notice'], 'client': 'web', 'pageIndex': 1, 'pageSize': count}, ensure_ascii=False)
        url = f'https://search-api-web.eastmoney.com/search/jsonp?cb=j&param={urllib.parse.quote(param)}'
        text = await self._fetch(url)
        if not text:
            return []
        else:
            m = __import__('re').search('\\(\\{', text)
            if m:
                text = text[m.start() + 1:]
                if text.endswith(');'):
                    text = text[:(-2)]
            try:
                data = json.loads(text)
                items = data.get('result', {}).get('notice', [])
            except Exception:
                return []
            for item in items:
                result.append({'date': (item.get('date', '') or '')[:10], 'time': '', 'title': item.get('title', ''), 'summary': (item.get('content', '') or '')[:200], 'url': item.get('url', '') or ''})
            return result
    async def fetch_financial_data(self, code):
        ck = self._cache_key('financial', code)
        if (c := self._cache_get(ck)):
            return c
        else:
            result = {}
            url = f'https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_F10_FINANCE_MAINFINADATA&columns=SECURITY_CODE,REPORT_DATE,REPORT_DATE_NAME,NOTICE_DATE,TOTALOPERATEREVE,PARENTNETPROFIT,KCFJCXSYJLR,EPSJB,EPSXS,BPS,ROEJQ,XSMLL,XSJLL,ZCFZL,LD,SD,TOTAL_SHARE,MLR,TOTALOPERATEREVETZ,PARENTNETPROFITTZ,KCFJCXSYJLRTZ&pageNumber=1&pageSize=4&sortColumns=REPORT_DATE&sortTypes=-1&source=HSF10&client=PC&filter=(SECURITY_CODE=%22{code}%22)'
            data = await self._fetch_json(url)
            try:
                if data and data.get('result') and data['result'].get('data'):
                            latest = data['result']['data'][0]
                            revenue = float(latest.get('TOTALOPERATEREVE', 0) or 0)
                            result['report_date'] = latest.get('REPORT_DATE_NAME', '') or ''
                            result['revenue'] = self._fmt_financial(revenue)
                            result['net_profit'] = self._fmt_financial(float(latest.get('PARENTNETPROFIT', 0) or 0))
                            result['deducted_profit'] = self._fmt_financial(float(latest.get('KCFJCXSYJLR', 0) or 0))
                            eps = latest.get('EPSJB')
                            result['eps'] = f'{eps:.2f}' if eps is not None and eps != '' else ''
                            bps = latest.get('BPS')
                            result['bps'] = f'{bps:.2f}' if bps is not None and bps != '' else ''
                            roe = latest.get('ROEJQ')
                            result['roe'] = f'{float(roe):.2f}%' if roe is not None and roe != '' else ''
                            gross_margin = latest.get('XSMLL')
                            result['gross_margin'] = f'{float(gross_margin):.2f}%' if gross_margin is not None and gross_margin != '' else ''
                            net_margin = latest.get('XSJLL')
                            result['net_margin'] = f'{float(net_margin):.2f}%' if net_margin is not None and net_margin != '' else ''
                            debt_ratio = latest.get('ZCFZL')
                            result['debt_ratio'] = f'{float(debt_ratio):.2f}%' if debt_ratio is not None and debt_ratio != '' else ''
                            current_ratio = latest.get('LD')
                            result['current_ratio'] = f'{float(current_ratio):.2f}' if current_ratio is not None and current_ratio != '' else ''
                            quick_ratio = latest.get('SD')
                            result['quick_ratio'] = f'{float(quick_ratio):.2f}' if quick_ratio is not None and quick_ratio != '' else ''
                            shares = float(latest.get('TOTAL_SHARE', 0) or 0)
                            result['total_shares'] = self._fmt_financial(shares) if shares > 0 else ''
                            rev_tz = latest.get('TOTALOPERATEREVETZ')
                            result['revenue_growth'] = f'{float(rev_tz):.2f}%' if rev_tz is not None and rev_tz != '' else ''
                            profit_tz = latest.get('PARENTNETPROFITTZ')
                            result['net_profit_growth'] = f'{float(profit_tz):.2f}%' if profit_tz is not None and profit_tz != '' else ''
                            deducted_tz = latest.get('KCFJCXSYJLRTZ')
                            result['deducted_profit_growth'] = f'{float(deducted_tz):.2f}%' if deducted_tz is not None and deducted_tz != '' else ''
                            mlr = float(latest.get('MLR', 0) or 0)
                            result['gross_profit'] = self._fmt_financial(mlr) if mlr > 0 else ''
                if not result:
                    await self._fetch_financial_fallback(code, result)
            except Exception as e:
                logger.warning(f'EM datacenter financial parse: {e}')
                try:
                    await self._fetch_financial_fallback(code, result)
                except Exception as e2:
                    logger.warning(f'financial fallback also failed: {e2}')
            for key in ['revenue', 'net_profit', 'deducted_profit', 'eps', 'bps', 'roe', 'gross_margin', 'net_margin', 'debt_ratio', 'current_ratio', 'quick_ratio', 'total_shares', 'circulating_shares', 'pe_ttm', 'pb', 'revenue_growth', 'net_profit_growth', 'deducted_profit_growth', 'gross_profit', 'report_date']:
                result.setdefault(key, '')
            self._cache_set(ck, result)
            return result
    async def _fetch_financial_fallback(self, code, result):
        """财务数据回退：利润表+资产负债表"""
        # ***<module>.MultiSourceFetcher._fetch_financial_fallback: Failure: Different control flow
        income_url = f'https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_DMSK_FN_INCOME&columns=SECURITY_CODE,NOTICE_DATE,REPORT_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT,OPERATE_PROFIT,TOTAL_PROFIT,SALE_EXPENSE,MANAGE_EXPENSE,FINANCE_EXPENSE,INVEST_INCOME&pageNumber=1&pageSize=4&sortColumns=NOTICE_DATE&sortTypes=-1&source=HSF10&client=PC&filter=(SECURITY_CODE=%22{code}%22)'
        income_data = await self._fetch_json(income_url)
        balance_url = f'https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_DMSK_FN_BALANCE&columns=SECURITY_CODE,NOTICE_DATE,TOTAL_ASSETS,TOTAL_LIABILITIES,TOTAL_EQUITY,TOTAL_SHEQUITY,MONETARYFUNDS,INVENTORY,FIXED_ASSET&pageNumber=1&pageSize=4&sortColumns=NOTICE_DATE&sortTypes=-1&source=HSF10&client=PC&filter=(SECURITY_CODE=%22{code}%22)'
        balance_data = await self._fetch_json(balance_url)
        if income_data and income_data.get('result') and income_data['result'].get('data'):
                    latest = income_data['result']['data'][0]
                    revenue = float(latest.get('TOTAL_OPERATE_INCOME', 0) or 0)
                    result['revenue'] = self._fmt_financial(revenue)
                    result['net_profit'] = self._fmt_financial(float(latest.get('PARENT_NETPROFIT', 0) or 0))
                    result['deducted_profit'] = self._fmt_financial(float(latest.get('DEDUCT_PARENT_NETPROFIT', 0) or 0))
                    result['net_margin'] = self._fmt_ratio(float(latest.get('PARENT_NETPROFIT', 0) or 0), revenue)
                    if len(income_data['result']['data']) >= 2:
                        prev_rev = float(income_data['result']['data'][1].get('TOTAL_OPERATE_INCOME', 0) or 0)
                        if prev_rev > 0:
                            result['revenue_growth'] = f'{round((revenue - prev_rev) / prev_rev * 100, 2)}%'
        if balance_data:
            if not balance_data.get('result') or balance_data['result'].get('data'):
                    bl = balance_data['result']['data'][0]
                    equity = float(bl.get('TOTAL_EQUITY', 0) or 0)
                    debt = float(bl.get('TOTAL_LIABILITIES', 0) or 0)
                    assets = float(bl.get('TOTAL_ASSETS', 0) or 0)
                    if assets > 0:
                        result['debt_ratio'] = f'{round(debt / assets * 100, 2)}%'
                    if debt > 0 and assets > 0:
                            ca = float(bl.get('MONETARYFUNDS', 0) or 0) + float(bl.get('INVENTORY', 0) or 0)
                            result['current_ratio'] = f'{round(assets / debt, 2)}'
    @staticmethod
    def _fmt_financial(value):
        """格式化财务数字：自动转换为亿/万"""
        if value is None or value == 0:
            return ''
        else:
            v = float(value)
            if abs(v) >= 100000000.0:
                return f'{v / 100000000.0:.2f}亿'
            else:
                if abs(v) >= 10000.0:
                    return f'{v / 10000.0:.2f}万'
                else:
                    return f'{v:.2f}'
    @staticmethod
    def _fmt_ratio(numerator, denominator):
        """计算比率"""
        if not denominator:
            return ''
        else:
            return f'{round(numerator / denominator * 100, 2)}%'
    async def fetch_institution_research(self, code):
        ck = self._cache_key('research', code)
        if (c := self._cache_get(ck)):
            return c
        else:
            result = []
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
            try:
                data = await self._fetch_json(f'https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_ORG_SURVEY&columns=SECURITY_CODE,RECEIVE_START_DATE,NOTICE_DATE,RECEIVE_OBJECT,ORG_TYPE,RECEIVE_WAY_EXPLAIN,RECEIVE_PLACE,CONTENT&pageNumber=1&pageSize=60&sortColumns=NOTICE_DATE&sortTypes=-1&source=HSF10&client=PC&filter=(SECURITY_CODE=%22{code}%22)')
                if data and data.get('success') and data.get('result'):
                            for item in data['result'].get('data', []):
                                date_str = item.get('RECEIVE_START_DATE') or item.get('NOTICE_DATE') or ''
                                if date_str < cutoff:
                                    continue
                                else:
                                    summary = (item.get('CONTENT') or '').replace('\n', ' ').replace('\r', ' ')
                                    result.append({'date': date_str[:10] if date_str else '', 'institution': item.get('RECEIVE_OBJECT', '') or '', 'institution_type': item.get('ORG_TYPE', '') or '', 'summary': summary, 'way': item.get('RECEIVE_WAY_EXPLAIN', '') or '', 'place': item.get('RECEIVE_PLACE', '') or ''})
            except Exception as e:
                logger.debug(f'research: {e}')
            self._cache_set(ck, result)
            return result
    async def fetch_trade_details(self, code):
        """成交明细（分钟级聚合，最新在前），多源兜底：腾讯 -> 东方财富趋势，不做缓存"""
        result = []
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?_var=minute&code={('sh' if code.startswith('6') else 'sz')}{code}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Referer': 'https://gu.qq.com/'}, timeout=aiohttp.ClientTimeout(total=6)) as r:
                    text = await r.text()
            if not text or not text.startswith('minute='):
                logger.warning(f'trade details[tx] invalid response: {code}')
            else:
                data = json.loads(text[7:])
                inner = data.get('data', {})
                prefix = f"{('sh' if code.startswith('6') else 'sz')}{code}"
                stock_data = inner.get(prefix, {})
                minutes = stock_data.get('data', {}).get('data', [])
                if minutes:
                    items = []
                    prev_price = None
                    for item_str in minutes:
                        parts = item_str.split()
                        if len(parts) < 3:
                            continue
                        try:
                            hm = parts[0].strip()
                            price = float(parts[1])
                            volume = int(parts[2])
                        except (ValueError, TypeError):
                            continue
                        time_str = f'{hm[:2]}:{hm[2:]}:00' if len(hm) >= 4 else hm
                        if prev_price is None:
                            trade_type = 'neutral'
                        elif price > prev_price:
                            trade_type = 'buy'
                        elif price < prev_price:
                            trade_type = 'sell'
                        else:
                            trade_type = 'neutral'
                        prev_price = price
                        items.append({'time': time_str, 'price': price, 'volume': volume, 'type': trade_type})
                    if items:
                        items.reverse()
                        result = items
        except Exception as e:
            logger.warning(f'trade details[tx] error: {e} for {code}')
        if not result:
            em_code = f"{('1' if code.startswith('6') else '0')}.{code}"
            url = f'https://push2delay.eastmoney.com/api/qt/stock/trends2/get?fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ut=7eea3edcaed734be934f08119a5c98e7&ndays=1&iscr=0&secid={em_code}'
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'}, timeout=aiohttp.ClientTimeout(total=6)) as r:
                        data = await r.json()
                lines = data.get('data', {}).get('trends', [])
                if not lines:
                    logger.info(f'trade details[em] no trends data: {code}')
                else:
                    items = []
                    prev_price = None
                    for line_str in lines:
                        parts = line_str.split(',')
                        if len(parts) < 8:
                            continue
                        try:
                            hm = parts[0]
                            price = float(parts[2])
                            vol = int(float(parts[5]))
                        except (ValueError, TypeError, IndexError):
                            continue
                        if ' ' in hm:
                            hm = hm.split()[-1]
                        if len(hm) >= 5:
                            hm = hm[:5]
                        time_str = f'{hm}:00' if ':' in hm else f'{hm[:2]}:{hm[2:]}:00'
                        if prev_price is None:
                            trade_type = 'neutral'
                        elif price > prev_price:
                            trade_type = 'buy'
                        elif price < prev_price:
                            trade_type = 'sell'
                        else:
                            trade_type = 'neutral'
                        prev_price = price
                        items.append({'time': time_str, 'price': price, 'volume': vol, 'type': trade_type})
                    if items:
                        items.reverse()
                        result = items
            except Exception as e:
                logger.warning(f'trade details[em] error: {e} for {code}')
        if not result:
            logger.info(f'trade details empty for: {code}')
        return result
    _CONCEPT_FILTER = {'百元股', '转债标的', '中小100', '央视50', '中证500', '小盘股', '昨日连板', '上证50', '北京板块', '中特估', '沪深300', '四川板块', '科技风格', '河南板块', '大盘成长', '破增发价股', '央国企改革', '西藏板块', '券商金股', '深圳特区', '西部大开发', '云南板块', '保险重仓', '昨日涨停', '湖南板块', '青海板块', '广东板块', '2026一季报预增', '行业龙头', 'AH股', '信托重仓', '河北板块', '宁组合', '天津板块', '甘肃板块', '基金重仓', '新疆板块', '沪股通', '深证300', '福建板块', '山东板块', '券商重仓', '安徽板块', '昨日跌停', '证金持股', '周期股', '东方财富热股', '辽宁板块', '科创100', '创业板综', '内蒙古板块', '高成长股', 'MSCI中国', '标准普尔', '密集调研', '深成500', '低价股', '宁夏板块', '社保重仓', '重庆板块', '黑龙江板块', '破净股', '吉林板块', '江苏板块', '广西板块', '富时罗素', '茅指数', '先进制造风格', 'HS300', '创业成份', '2026中报预增', '国证2000', '机构重仓', '权重股', '浙江板块', '贵州板块', '2025三季报预增', '昨日触板', '科创50', '湖北板块', '2025年报预增', '预亏预减', '中盘股', '雄安新区', '大盘股', 'AB股', '陕西板块', '大盘价值', '股权分散', '上证180', '深股通', '江西板块', '山西板块', '海南板块', '深证100R', '预盈预增', '融资融券', 'QFII重仓', '上海板块', '内蒙古'}
    async def fetch_stock_concepts(self, code):
        """获取股票板块概念（东方财富 CoreConception API）"""
        em_code = _normalize_code_em(code)
        if not em_code:
            return []
        else:
            ck = self._cache_key('concepts_em', em_code)
            if (c := self._cache_get(ck)):
                return c
            else:
                concepts = []
                try:
                    url = f'https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={em_code}'
                    t = await self._fetch(url, encoding='utf-8')
                    if t:
                        data = json.loads(t)
                        boards = data.get('ssbk', [])
                        for b in boards:
                            name = b.get('BOARD_NAME', '')
                            name_clean = name.strip('_ ')
                            if name_clean and name_clean not in self._CONCEPT_FILTER:
                                    concepts.append(name_clean)
                except Exception:
                    pass
                self._cache_set(ck, concepts)
                return concepts
    async def fetch_telegraph(self, count=30):
        """获取实时财经快讯（财联社电报，失败时兜底新浪 7x24）
        返回: [{id, time, title, content, level}]，新→旧排序
        """
        base = {'app': 'CailianpressWeb', 'os': 'web', 'sv': '8.7.9'}
        try:
            params = {**base, 'name': 'telegraph'}
            sign = hashlib.md5(hashlib.sha1('&'.join(f'{k}={params[k]}' for k in sorted(params)).encode()).hexdigest().encode()).hexdigest()
            url = 'https://www.cls.cn/api/cache?' + '&'.join(f'{k}={v}' for k, v in params.items()) + f'&sign={sign}'
            data = await self._fetch_json(url, extra_headers={'Referer': 'https://www.cls.cn/telegraph', 'Origin': 'https://www.cls.cn'})
            items = ((data or {}).get('data', {}) or {}).get('roll_data', []) or []
            result = []
            for it in items:
                content = _strip_news_html(it.get('content', '') or it.get('brief', '') or '')
                title = _strip_news_html(it.get('title', '')) or content[:40]
                if not content:
                    continue
                ct = it.get('ctime') or 0
                try:
                    t = time.strftime('%m-%d %H:%M', time.localtime(int(ct)))
                except Exception:
                    t = ''
                result.append({'id': it.get('id'), 'time': t, 'title': title, 'content': content, 'level': it.get('level', 'C') if it.get('level') else 'C'})
            if result:
                return result
        except Exception:
            pass
        try:
            url = f'https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={count}&zhibo_id=152'
            data = await self._fetch_json(url, extra_headers={'Referer': 'https://finance.sina.com.cn/'})
            res = (data or {}).get('result', {}) or {}
            dat = res.get('data', {}) or {}
            feed = dat.get('feed', {}) or {}
            items = feed.get('list', []) or []
            result = []
            for it in items:
                content = _strip_news_html(it.get('rich_text', '') or it.get('rich_text2', ''))
                if not content:
                    continue
                ct = it.get('create_time', '')
                result.append({'id': it.get('id'), 'time': str(ct)[5:16], 'title': content[:40], 'content': content, 'level': it.get('tag', 'C') or 'C'})
            return result
        except Exception:
            return []
def _load_pinyin_data():
    char_groups = {'a': '安爱奥澳阿艾按昂敖', 'b': '百宝北贝倍博步白保邦宝半包毕波玻比币必标滨柏坂蚌', 'c': '创长川城成程超诚传策财材产车储昌辰淳慈楚畅彩', 'd': '大东电达德地迪动多登道丹典帝度低鼎当东渡独代', 'e': '恩鄂尔二', 'f': '风方福丰发富飞复凡菲锋封分粉服飞辅富峰', 'g': '国工广高光公港钢格贵冠甘歌谷硅古关广桂', 'h': '华海航河汉宏恒合沪湖环惠化好杭豪虹虎辉恒恒', 'j': '金建机京江集佳技交军九巨嘉捷洁健经俱晶景聚骏', 'k': '科康凯可开矿快控空宽昆康酷', 'l': '联龙力利蓝路林绿罗莱兰洛隆朗流来立拉兰菱', 'm': '民明美码茂迈马墨梦木盟名梦茅牧', 'n': '能南宁农纳诺南娘年', 'o': '欧', 'p': '普平鹏牌派匹品普品盘', 'q': '强前汽齐全青其千秦启泉群旗乾奇桥', 'r': '日人瑞荣润软若融融燃', 's': '上深三沙世生时数双石顺水商尚苏斯设丝杉色深神盛森', 't': '天通太铁同泰腾唐团拓钛碳特田泰通台', 'w': '万微五物武文网伟望王外未皖维威', 'x': '新信息先兴行西学星小芯细夏湘旭先翔', 'y': '一医银亿用有云亚远洋英越阳药伊友易韵宇渝元永义', 'z': '中智正自资子重招珠众紫张浙卓珠周洲兆庄州'}
    pinyin = {}
    for initial, chars in char_groups.items():
        for ch in chars:
            pinyin[ch] = initial
    return pinyin
_pinyin_data = _load_pinyin_data()
def _to_halfwidth(text):
    """将全角ASCII字符转为半角（Sina数据中A股名称含全角字母如 京东方Ａ）"""
    result = []
    for ch in text:
        code = ord(ch)
        if 65281 <= code <= 65374:
            result.append(chr(code - 65248))
        elif code == 12288:
            result.append(' ')
        else:
            result.append(ch)
    return ''.join(result)
def get_pinyin_initials(text):
    """获取中文文本的拼音首字母"""
    text = _to_halfwidth(text)
    try:
        from pypinyin import lazy_pinyin, Style
        return ''.join([s[0] if s else '' for s in lazy_pinyin(text, style=Style.FIRST_LETTER)]).lower()
    except ImportError:
        pass
    result = []
    for ch in text:
        if 'a' <= ch.lower() <= 'z' or '0' <= ch <= '9':
            result.append(ch.lower())
        elif ch in _pinyin_data:
            result.append(_pinyin_data[ch])
        else:
            continue
    return ''.join(result)

# 拼音首字母缓存：股票列表 5000+ 只，每次搜索/按键都全量匹配，缓存后首遍即全命中
_pinyin_cache = {}


def get_pinyin_initials_cached(text):
    """带缓存的拼音首字母（用于批量匹配，避免重复调用 pypinyin）"""
    key = _to_halfwidth(text)
    v = _pinyin_cache.get(key)
    if v is None:
        v = get_pinyin_initials(key)
        _pinyin_cache[key] = v
    return v


def match_stock_by_pinyin(stocks, keyword):
    kw = keyword.strip().lower()
    if not kw:
        return []
    else:
        results = []
        for s in stocks:
            code = str(s.get('code', ''))
            name = str(s.get('name', ''))
            name_hw = _to_halfwidth(name).lower()
            py = get_pinyin_initials_cached(name).lower()
            if kw in code or kw in name_hw or kw in py or (len(kw) >= 2 and kw in name_hw):
                results.append(s)
        return results