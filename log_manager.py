"""
日志管理模块

日志统一保存到 <安装目录>\log 目录，按"日期 + 栏目"分文件：
  例如 2026-08-21_早盘竞价.log、2026-08-21_每日推荐.log

栏目划分：
- morning   早盘竞价
- afternoon 尾盘2点半
- closing   收盘推荐
- daily     每日推荐
- search    搜索
- main      主页（股票列表更新等）
- system    系统（启动、全局异常等）

用法：
    import log_manager
    logger = log_manager.get_logger('morning')   # 返回 logging.Logger
    logger.info('...')
    logger.exception('...')   # 自动带堆栈
"""
import logging
import os
import threading
from datetime import datetime

import app_paths

# 栏目 key -> 文件名中的中文栏目名
SECTIONS = {
    'morning': '早盘竞价',
    'afternoon': '尾盘2点半',
    'closing': '收盘推荐',
    'daily': '每日推荐',
    'search': '搜索',
    'main': '主页',
    'system': '系统',
}

DEFAULT_FORMAT = '[%(asctime)s] [%(levelname)s] %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

_loggers = {}
_lock = threading.Lock()


class DailySectionHandler(logging.Handler):
    """按日期+栏目分文件写入的 Handler：每天自动切换新文件"""

    def __init__(self, log_dir, section_name):
        super().__init__()
        self.log_dir = log_dir
        self.section_name = section_name
        self._stream = None
        self._current_date = None
        self._hlock = threading.Lock()

    def _ensure_stream(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if self._stream is not None and self._current_date == today:
            return
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        app_paths.ensure_log_dir()
        path = os.path.join(self.log_dir, f'{today}_{self.section_name}.log')
        self._stream = open(path, 'a', encoding='utf-8')
        self._current_date = today

    def emit(self, record):
        try:
            with self._hlock:
                self._ensure_stream()
                msg = self.format(record)
                self._stream.write(msg + '\n')
                self._stream.flush()
        except Exception:
            pass

    def close(self):
        try:
            with self._hlock:
                if self._stream:
                    self._stream.close()
                    self._stream = None
        finally:
            super().close()


def get_logger(section='system'):
    """获取指定栏目的 logger。未知栏目回退到 system。"""
    if section not in SECTIONS:
        section = 'system'
    with _lock:
        if section in _loggers:
            return _loggers[section]
        logger = logging.getLogger(f'raccoon.{section}')
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = DailySectionHandler(app_paths.get_log_dir(), SECTIONS[section])
        handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DATE_FORMAT))
        logger.addHandler(handler)
        _loggers[section] = logger
        return logger


def setup_system_logging():
    """应用启动时调用：记录启动信息 + 挂接全局未捕获异常到系统日志。

    不会覆盖 main.py 已安装的调试 crash handler——通过链式调用保留原有行为。
    """
    import sys

    logger = get_logger('system')
    logger.info('========== 应用启动 ==========')

    prev_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        logger.critical(
            '未捕获异常: %s: %s',
            getattr(exc_type, '__name__', exc_type),
            exc_value,
            exc_info=(exc_type, exc_value, exc_tb))
        # 链式调用既有 hook（调试模式控制台/暂停）
        if prev_hook and prev_hook not in (sys.__excepthook__, _hook):
            try:
                prev_hook(exc_type, exc_value, exc_tb)
            except Exception:
                pass

    sys.excepthook = _hook

    # 捕获其他线程（QThread）中的未捕获异常
    prev_thread_hook = threading.excepthook if hasattr(threading, 'excepthook') else None

    def _thread_hook(args):
        try:
            logger.critical(
                '线程未捕获异常: %s: %s',
                getattr(args.exc_type, '__name__', args.exc_type), args.exc_value,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass
        if prev_thread_hook:
            try:
                prev_thread_hook(args)
            except Exception:
                pass

    if hasattr(threading, 'excepthook'):
        threading.excepthook = _thread_hook
    return logger
