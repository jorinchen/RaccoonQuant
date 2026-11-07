"""
从 .pyc 编译缓存恢复丢失源文件的模块加载器。

背景：data_fetcher.py 源文件意外丢失，仅剩 __pycache__ 下的 .pyc。
本加载器在源码运行与打包 exe 中，用 marshal 将 pyc 反序列化为模块
注入 sys.modules，保证 `from data_fetcher import ...` 正常工作。

注意：仅作为临时兜底方案；若拿到原始 .py 源文件，直接放回项目根目录
即可（本文件与 main.py 中的注入逻辑可删除）。
"""
import marshal
import os
import sys
import types

_LOADED = set()


def _load_pyc(name, pyc_path):
    if name in _LOADED or name in sys.modules:
        return name in sys.modules
    try:
        with open(pyc_path, 'rb') as f:
            f.read(16)  # magic(4) + flags(4) + mtime(4) + size(4)
            code = marshal.load(f)
        m = types.ModuleType(name)
        m.__file__ = pyc_path
        exec(code, m.__dict__)
        sys.modules[name] = m
        _LOADED.add(name)
        return True
    except Exception:
        return False


def ensure(name, pyc_name):
    """确保模块 name 可用：若已导入则跳过，否则尝试从 pyc 加载"""
    if name in sys.modules:
        return
    candidates = []
    base = getattr(sys, '_MEIPASS', None)  # PyInstaller 解压目录
    if base:
        candidates.append(os.path.join(base, pyc_name))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, '__pycache__', pyc_name))
    for p in candidates:
        if os.path.exists(p) and _load_pyc(name, p):
            return
