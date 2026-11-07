# -*- coding: utf-8 -*-
"""主页数据源连通性诊断 v2（模拟 UI 真实调用方式，临时脚本，测完即删）"""
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')

try:
    import data_fetcher
except ImportError:
    from pyc_loader import ensure as _ensure
    _ensure('data_fetcher', 'data_fetcher.cpython-311.pyc')
    import data_fetcher
print('[1] data_fetcher loaded from:', data_fetcher.__file__)

from data_fetcher_patch import apply_patches
apply_patches(data_fetcher)
print('[2] patch applied')

import inspect
print('[sig] fetch_kline_data:', inspect.signature(data_fetcher.MultiSourceFetcher.fetch_kline_data))
print('[sig] fetch_money_flow:', inspect.signature(data_fetcher.MultiSourceFetcher.fetch_money_flow))
print('[sig] fetch_telegraph:', inspect.signature(data_fetcher.MultiSourceFetcher.fetch_telegraph))


async def main():
    # 严格模拟 UI：async with
    async with data_fetcher.MultiSourceFetcher() as fetcher:
        print('async with OK, has _bg_tasks:', hasattr(fetcher, '_bg_tasks'))

        try:
            t0 = time.time()
            stocks = await fetcher.fetch_stock_list(page_count=5)
            print(f'[4] fetch_stock_list -> {len(stocks) if stocks else "EMPTY"} 只, 耗时 {time.time()-t0:.1f}s')
            if stocks:
                print('    样例:', json.dumps(stocks[0], ensure_ascii=False)[:200])
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[4] fetch_stock_list 异常:', type(e).__name__, e)

        try:
            t0 = time.time()
            quotes = await fetcher.fetch_real_time_quotes(['600519', '000001', '300750'])
            print(f'[5] fetch_real_time_quotes -> {len(quotes) if quotes else "EMPTY"} 条, 耗时 {time.time()-t0:.1f}s')
            for k, v in (quotes or {}).items():
                print('    ', k, json.dumps(v, ensure_ascii=False)[:150])
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[5] fetch_real_time_quotes 异常:', type(e).__name__, e)

        try:
            t0 = time.time()
            n = await fetcher.fetch_money_flow('600519')
            print(f'[6] fetch_money_flow -> {len(n) if n else "EMPTY"} 条, 耗时 {time.time()-t0:.1f}s')
            if n:
                print('    样例:', json.dumps(n, ensure_ascii=False)[:200])
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[6] fetch_money_flow 异常:', type(e).__name__, e)

        try:
            t0 = time.time()
            k = await fetcher.fetch_kline_data('600519', count=5)
            print(f'[7] fetch_kline_data -> {len(k) if k else "EMPTY"} 条, 耗时 {time.time()-t0:.1f}s')
            if k:
                print('    样例:', json.dumps(k[0], ensure_ascii=False)[:200])
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[7] fetch_kline_data 异常:', type(e).__name__, e)

        try:
            t0 = time.time()
            te = await fetcher.fetch_telegraph()
            print(f'[8] fetch_telegraph -> {len(te) if te else "EMPTY"} 条, 耗时 {time.time()-t0:.1f}s')
            if te:
                print('    样例:', json.dumps(te[0], ensure_ascii=False)[:200])
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[8] fetch_telegraph 异常:', type(e).__name__, e)

        try:
            t0 = time.time()
            pr = await fetcher.fetch_price_changes('600519')
            print(f'[9] fetch_price_changes -> {pr if pr else "EMPTY"}, 耗时 {time.time()-t0:.1f}s')
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[9] fetch_price_changes 异常:', type(e).__name__, e)

        try:
            t0 = time.time()
            con = await fetcher.fetch_stock_concepts('600519')
            print(f'[10] fetch_stock_concepts -> {len(con) if con else "EMPTY"} 条, 耗时 {time.time()-t0:.1f}s')
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[10] fetch_stock_concepts 异常:', type(e).__name__, e)

        try:
            t0 = time.time()
            rang = await fetcher.fetch_price_range('600519')
            print(f'[11] fetch_price_range -> {rang if rang else "EMPTY"}, 耗时 {time.time()-t0:.1f}s')
        except Exception as e:
            import traceback; traceback.print_exc()
            print('[11] fetch_price_range 异常:', type(e).__name__, e)


if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
