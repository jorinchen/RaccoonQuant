# -*- coding: utf-8 -*-
"""运行时诊断日志：记录 data_fetcher 加载/补丁/网络自检，写入 %TEMP%\\RaccoonQuant_diag.log。
源码运行与打包 exe 均适用；网络自检仅在环境变量 RACCOON_DIAG=1 时执行。
"""
import datetime
import os
import traceback

LOG = os.path.join(os.environ.get('TEMP', os.path.expanduser('~')),
                   'RaccoonQuant_diag.log')


def log(msg):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def excepthook(exc_type, exc_value, exc_tb):
    log('UNCAUGHT EXCEPTION:\n' +
        ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))


async def _raw_http_test():
    """绕过 data_fetcher，直接测试 aiohttp 原始请求"""
    import aiohttp
    log('diag: aiohttp version=%s' % getattr(aiohttp, '__version__', '?'))
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get('https://hq.sinajs.cn/list=sh600519',
                             headers={'Referer': 'https://finance.sina.com.cn'}) as r:
                txt = await r.text()
                log('diag: aiohttp 原始请求 OK: status=%s body=%r' % (r.status, txt[:60]))
    except Exception as e:
        log('diag: aiohttp 原始请求 FAIL: %r\n%s' % (e, traceback.format_exc()))


async def network_diag():
    """网络数据自检：逐项调用关键数据接口，结果写入诊断日志"""
    import asyncio
    import json
    log('==== 网络自检开始 ====')
    await _raw_http_test()
    try:
        import data_fetcher
    except Exception:
        log('diag: import data_fetcher FAIL: %r' % (traceback.format_exc(),))
        return
    log('diag: data_fetcher 已加载, MultiSourceFetcher=%s' %
        hasattr(data_fetcher, 'MultiSourceFetcher'))
    try:
        from data_fetcher_patch import apply_patches
        apply_patches(data_fetcher)
        log('diag: 补丁已应用, fetch_hot_boards=%s' %
            hasattr(data_fetcher.MultiSourceFetcher, 'fetch_hot_boards'))
    except Exception:
        log('diag: 补丁应用 FAIL:\n%s' % traceback.format_exc())
    log('diag: CACHE_DIR=%s' % getattr(data_fetcher, 'CACHE_DIR', 'N/A'))
    try:
        async with data_fetcher.MultiSourceFetcher() as fetcher:
            checks = [
                ('fetch_real_time_quotes',
                 lambda: fetcher.fetch_real_time_quotes(['000001', '600519'])),
                ('fetch_stock_list',
                 lambda: fetcher.fetch_stock_list()),
                ('fetch_financial_data',
                 lambda: fetcher.fetch_financial_data('000001')),
                ('fetch_telegraph',
                 lambda: fetcher.fetch_telegraph(count=5)),
            ]
            for name, fn in checks:
                try:
                    r = await asyncio.wait_for(fn(), timeout=20)
                    if name == 'fetch_stock_list':
                        log('diag: %s OK: %d 支' % (name, len(r) if r else 0))
                    elif name == 'fetch_telegraph':
                        log('diag: %s OK: %d 条' % (name, len(r) if r else 0))
                    else:
                        log('diag: %s OK: %s' % (
                            name, json.dumps(r, ensure_ascii=False)[:300]))
                except asyncio.TimeoutError:
                    log('diag: %s 超时(20s)！' % name)
                except Exception as e:
                    log('diag: %s FAIL: %r\n%s' % (
                        name, e, traceback.format_exc()))
    except Exception:
        log('diag: MultiSourceFetcher 初始化 FAIL:\n%s' % traceback.format_exc())
    log('==== 网络自检结束 ====')
