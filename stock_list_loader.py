"""
股票列表统一加载服务

各栏目（早盘竞价 / 尾盘2点半 / 收盘推荐 / 每日推荐 / 搜索）加载股票列表时
统一走本模块的 load_stock_list()，加载优先级：

  1. 数据库（MySQL 全量表）——首选，连接失败 / 表空则自动降级
  2. 安装目录本地文件 <安装目录>\data\stock_list.json
     （上次从平台获取或主页更新时保存，24h 内视为新鲜，离线秒开）
  3. 各大平台（东财 / 腾讯 / TDX / 新浪，fetch_stock_list）——联网兜底
     获取成功后自动保存到安装目录文件，并尽力写回数据库，
     保证"下次从数据库/本地文件加载"的闭环。

返回统一字段格式（见 app_paths.normalize_stock），各栏目可直接消费。
"""
import asyncio
import os
import time

import app_paths
import mysql_store

DEFAULT_MAX_AGE = 24 * 3600  # 本地文件 24 小时内视为新鲜
MIN_STOCKS = 100             # 数据库/文件行数低于此值视为无效（空库/残留）

SOURCE_DB = 'db'
SOURCE_FILE = 'file'
SOURCE_PLATFORM = 'platform'


def _rows_to_stocks(rows):
    return [n for n in (app_paths.normalize_stock(r) for r in rows) if n]


def _log(progress_cb, msg):
    if progress_cb:
        try:
            progress_cb(msg)
        except Exception:
            pass


def _file_fresh():
    """本地股票列表文件是否新鲜（存在且未超过 24h）"""
    fp = app_paths.get_stock_list_file()
    try:
        if os.path.exists(fp):
            return (time.time() - os.path.getmtime(fp)) <= DEFAULT_MAX_AGE
    except Exception:
        pass
    return False


async def load_stock_list(fetcher, progress_cb=None, allow_platform=True):
    """加载全量 A 股列表。

    fetcher: MultiSourceFetcher 实例（已通过 async with 进入）
    progress_cb: 可选进度回调（如 QThread 的 progress.emit）
    allow_platform: 为 True 时数据库/文件均不可用才联网；为 False 时仅本地源
    返回: list[dict]，字段见 app_paths.normalize_stock
    """
    # ---------- 1. 数据库优先 ----------
    _log(progress_cb, '正在从数据库加载股票列表...')
    rows = await asyncio.to_thread(mysql_store.fetch_all_stocks)
    if rows and len(rows) >= MIN_STOCKS:
        stocks = _rows_to_stocks(rows)
        if stocks:
            _log(progress_cb, f'已从数据库加载 {len(stocks)} 支股票')
            return stocks, SOURCE_DB

    # ---------- 2. 安装目录本地文件 ----------
    if _file_fresh():
        _log(progress_cb, '数据库不可用，正在从本地文件加载股票列表...')
        file_rows = await asyncio.to_thread(app_paths.load_stock_list_file)
        if file_rows and len(file_rows) >= MIN_STOCKS:
            _log(progress_cb, f'已从本地文件加载 {len(file_rows)} 支股票')
            return file_rows, SOURCE_FILE

    # ---------- 3. 各大平台获取（兜底） ----------
    if not allow_platform:
        return [], SOURCE_PLATFORM
    _log(progress_cb, '数据库为空且无本地缓存，正在从各大平台获取股票列表...')
    stocks = []
    try:
        raw = await fetcher.fetch_stock_list(page_count=40)
        stocks = _rows_to_stocks(raw)
    except Exception as e:
        _log(progress_cb, f'平台获取失败: {e}')
    if stocks:
        _log(progress_cb, f'平台获取 {len(stocks)} 支，正在保存到安装目录...')
        await asyncio.to_thread(app_paths.save_stock_list, stocks)
        # 尽力写回数据库，失败不影响本次使用（下次仍可优先读库）
        try:
            await asyncio.to_thread(mysql_store.insert_missing, stocks)
        except Exception:
            pass
    return stocks, SOURCE_PLATFORM
