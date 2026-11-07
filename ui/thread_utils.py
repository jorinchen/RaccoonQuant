"""
QThread 安全终止工具

PyQt6 中若 QThread 的 Python 对象在 C++ 线程仍在运行时被 GC 销毁，
会触发致命错误："QThread: Destroyed while thread is still running"（退出码
0xC0000409 / -1073740791）。

本工具保证 terminate 后循环等待线程真正结束；若超时（如网络阻塞在
asyncio 循环中），则把线程引用转入模块级引用池，待其 finished 信号
触发后再回收。退出前调用 shutdown_all() 统一等待所有登记线程结束，
从根上避免运行中的线程对象在进程退出时被销毁。
"""
import sys
import time
import traceback
from PyQt6.QtCore import QThread

# 模块级引用池：保存 terminate 超时仍未结束的线程，防止被 GC 销毁
_alive = []
# 诊断：记录每个登记线程的创建堆栈，崩溃/无法终止时打印定位
_traces = {}


def _on_finished(t):
    try:
        _alive.remove(t)
    except ValueError:
        pass
    try:
        _traces.pop(id(t), None)
    except Exception:
        pass
    try:
        t.deleteLater()
    except Exception:
        pass


def keep_alive(t):
    """将线程登记到全局引用池：防止其被 GC 销毁时仍在运行。
    线程自然结束（finished）后自动移出并销毁。幂等。

    这是「引用兜底」：即使 owner 属性（self.thread 等）被竞态置 None、
    僵尸列表被误清空，只要线程仍在运行，全局池就始终持有引用，
    杜绝 'QThread: Destroyed while thread is still running' 崩溃。"""
    if not isinstance(t, QThread):
        return
    if t not in _alive:
        _alive.append(t)
        try:
            # 去掉本函数自身的几帧，保留调用点
            _traces[id(t)] = ''.join(traceback.format_stack()[:-1])
        except Exception:
            pass
        try:
            t.finished.connect(lambda: _on_finished(t))
        except (TypeError, RuntimeError):
            pass


def _is_running(t):
    """对僵尸 QThread（C++ 对象已被 deleteLater 销毁）免疫的 isRunning 检查。"""
    try:
        return bool(isinstance(t, QThread) and t.isRunning())
    except RuntimeError:
        return False  # C++ 对象已删除，视为不再运行


def dump_tracked():
    """打印仍存活（运行中）的登记线程及其创建堆栈，用于崩溃诊断"""
    lines = []
    for t in list(_alive):
        if _is_running(t):
            lines.append(f"--- [thread_utils] QThread running: {t} id={id(t)}")
            lines.append(_traces.get(id(t), '(no trace recorded)'))
    if not lines:
        lines.append("--- [thread_utils] no running tracked threads ---")
    return '\n'.join(lines)


def retire_thread(owner, attr='thread'):
    """退役旧线程：启动新线程前调用，避免直接覆盖 self.thread 导致
    运行中的 QThread 被 GC 销毁（'QThread: Destroyed while thread is still running'）。
    - 旧线程不存在/未运行：立即置 None，零开销
    - 旧线程运行中：kill_thread 安全终止，超时自动转入引用池
    """
    old = getattr(owner, attr, None)
    if isinstance(old, QThread):
        kill_thread(old)
    setattr(owner, attr, None)


def kill_thread(t, timeout_ms=5000):
    """安全终止线程。

    - 线程未运行或非 QThread：直接返回 True
    - 正常 terminate + 等待结束：返回 True
    - 超时未结束：二次 terminate 再等待（网络线程阻塞在 asyncio 时
      一次 terminate 往往不够）；仍超时则转入引用池等 finished 信号
      回收（调用方可安全置 None）
    """
    if not isinstance(t, QThread):
        return True
    if not _is_running(t):
        return True
    try:
        t.terminate()
    except RuntimeError:
        return False  # 僵尸对象：无法终止，转引用池兜底
    waited = 0
    while waited < timeout_ms:
        if t.wait(100):
            return True
        waited += 100
    # 二次 terminate：网络线程（asyncio 阻塞）一次 terminate 往往不够
    if _is_running(t):
        try:
            t.terminate()
        except RuntimeError:
            return False
        waited = 0
        while waited < timeout_ms:
            if t.wait(100):
                return True
            waited += 100
    # 仍超时：转入引用池，由 finished 信号负责回收
    if _is_running(t):
        keep_alive(t)
        return False
    return True


def shutdown_all(timeout_ms=20000):
    """退出前统一清理：确保所有登记线程真正结束，防止进程退出时
    运行中的 QThread 对象被析构触发 0xC0000409 崩溃。

    返回仍无法结束的线程列表（此时应绕过 Python 清理直接 os._exit，
    避免 C++ 对象析构崩溃）。幂等，可安全多次调用。
    对僵尸 QThread（C++ 对象已销毁）免疫：不抛 RuntimeError。
    """
    running = [t for t in list(_alive) if _is_running(t)]
    for t in running:
        try:
            t.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if not any(_is_running(t) for t in _alive):
            break
        QThread.msleep(50)
    remaining = [t for t in list(_alive) if _is_running(t)]
    if remaining:
        # 打印无法终止线程的创建堆栈，便于定位根因
        try:
            print("[thread_utils] 退出超时，仍有线程未结束：", file=sys.stderr, flush=True)
            print(dump_tracked(), file=sys.stderr, flush=True)
        except Exception:
            pass
        return remaining
    # 全部结束：安全销毁并清空引用池
    for t in list(_alive):
        if isinstance(t, QThread):
            try:
                t.wait(0)
            except Exception:
                pass
            try:
                t.deleteLater()
            except Exception:
                pass
    _alive.clear()
    return []
