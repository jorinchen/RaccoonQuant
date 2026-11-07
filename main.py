"""
小浣熊量化选股 - 主入口
"""
import sys
import os

# PyInstaller + aiohttp 在 Windows 上必须使用 SelectorEventLoop，
# 否则 ProactorEventLoop 不支持 add_reader/add_writer 导致轮询模式网络请求极慢
if sys.platform == 'win32':
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 恢复丢失源文件的模块（data_fetcher）：源文件意外丢失，仅剩 __pycache__ 编译缓存。
# 源码模式与打包 exe 均通过 pyc_loader 从 .pyc 注入；若原始 .py 放回后此段自动失效。
try:
    import data_fetcher  # noqa: F401
except ImportError:
    try:
        from pyc_loader import ensure as _ensure_pyc
        _ensure_pyc('data_fetcher', 'data_fetcher.cpython-311.pyc')
    except Exception:
        pass

# 应用 data_fetcher 运行时补丁（修复：新浪兜底列表污染缓存、腾讯行情覆盖不足、
# 空值/None 结果污染缓存）。补丁幂等，源码存在可随 exe 打包。
try:
    import data_fetcher as _df
    from data_fetcher_patch import apply_patches as _apply_patches
    _apply_patches(_df)
except Exception as _e:
    # 之前是静默 pass，导致补丁失效后只能靠运行时 AttributeError 才发现。
    # 现在打到诊断日志 + stderr 弹窗可见
    try:
        from diag import log as _diag_log
        _diag_log('apply_patches 失败: %r' % (_e,))
    except Exception:
        pass
    import traceback
    print('[main] apply_patches failed:', _e, file=__import__('sys').stderr)
    traceback.print_exc()

# 诊断日志：记录模块加载状态，供打包后问题排查
try:
    from diag import log as _diag_log
    _diag_log('启动: frozen=%s MEIPASS=%s' % (
        getattr(sys, 'frozen', False), getattr(sys, '_MEIPASS', None)))
    _diag_log('data_fetcher 已加载: %s, MultiSourceFetcher=%s' % (
        'data_fetcher' in sys.modules,
        hasattr(sys.modules.get('data_fetcher'), 'MultiSourceFetcher')))
    _diag_log('补丁已应用: %s' % (
        hasattr(getattr(sys.modules.get('data_fetcher'),
                        'MultiSourceFetcher', None), 'fetch_hot_boards')))
except Exception:
    pass


def _install_crash_handler():
    """开启 Debug 模式：程序发生未捕获异常时，控制台窗口保持打开，
    打印完整错误堆栈，按回车键后才退出，避免闪退后看不到报错。

    通过环境变量 RACCOON_DEBUG=0 可关闭该行为（默认开启）。
    仅对 Python 层异常有效；C/C++ 层崩溃（如访问违规）无法拦截。
    """
    import threading
    import traceback

    if os.environ.get('RACCOON_DEBUG', '1').strip() == '0':
        return

    def _pause(tag):
        try:
            print(f"\n===== [{tag}] 程序即将退出，控制台保持打开以便查看错误 =====",
                  file=sys.stderr, flush=True)
            input("按回车键关闭窗口...")
        except Exception:
            pass

    def _hook(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
        _pause('异常')

    def _thread_hook(args):
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback,
                                  file=sys.stderr)
        try:
            from diag import excepthook as _diag_exc
            _diag_exc(args.exc_type, args.exc_value, args.exc_traceback)
        except Exception:
            pass
        _pause('后台线程异常')

    sys.excepthook = _hook
    if hasattr(threading, 'excepthook'):
        threading.excepthook = _thread_hook
    print("[RaccoonQuant] Debug 模式已开启：异常退出时控制台窗口将保持打开。", flush=True)

# 静默 Qt Multimedia FFmpeg 后端的 INFO 级日志（如 "Using Qt multimedia with FFmpeg version..."）。
# 该消息只是说明音频播放后端已就绪，并非错误。必须在 PyQt6 导入前设置。
if sys.platform == 'win32':
    os.environ.setdefault('QT_LOGGING_RULES',
                          'qt.multimedia.ffmpeg.info=false')

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QFont
from PyQt6.QtCore import Qt, QLockFile
from PyQt6.QtNetwork import QLocalSocket
from ui.main_window import MainWindow

SERVER_NAME = "RaccoonQuantStock"

# 单实例锁：QLocalServer 检测存在竞态（快速双击时第一个实例的 QLocalServer
# 尚未创建，多个实例都能通过检测）。用文件锁 QLockFile 兜底，保证同一时刻
# 只有一个实例真正运行，避免多实例并发请求数据源导致限流/无数据。
LOCK_FILE = os.path.join(
    os.environ.get('TEMP', os.path.expanduser('~')),
    'RaccoonQuant_single_instance.lock',
)


def _acquire_single_lock():
    """尝试获取单实例文件锁，成功返回 QLockFile 对象，失败返回 None"""
    lock = QLockFile(LOCK_FILE)
    # 使用默认 staleLockTime（30s）：持有锁的进程若已退出，新实例可接管，
    # 避免进程异常退出后锁文件永久残留导致应用再也无法启动
    if lock.tryLock(100):
        return lock
    return None


def _notify_existing_instance():
    """通知已有实例显示窗口，返回 True 表示已有实例在运行"""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(500):
        sock.write(b"show")
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return True
    return False


def _create_raccoon_icon(size=64):
    """绘制小浣熊图标"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = size
    cx, cy = s / 2, s / 2

    # --- 耳朵 ---
    ear_w, ear_h = s * 0.22, s * 0.28
    ear_color = QColor("#7A7A7A")
    ear_inner = QColor("#D4A574")
    for side in [-1, 1]:
        ex = cx + side * s * 0.22
        ey = s * 0.15
        p.setBrush(ear_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(ex - ear_w / 2), int(ey - ear_h / 2), int(ear_w), int(ear_h))
        p.setBrush(ear_inner)
        p.drawEllipse(int(ex - ear_w / 3.5), int(ey - ear_h / 3.5), int(ear_w * 0.55), int(ear_h * 0.55))

    # --- 脸部（椭圆） ---
    face_w, face_h = s * 0.58, s * 0.52
    p.setBrush(QColor("#B0B0B0"))
    p.drawEllipse(int(cx - face_w / 2), int(cy - face_h / 2 + s * 0.02), int(face_w), int(face_h))

    # --- 白色脸颊区域 ---
    cheek_w, cheek_h = s * 0.36, s * 0.28
    p.setBrush(QColor("#E8E8E8"))
    p.drawEllipse(int(cx - cheek_w / 2), int(cy - cheek_h / 2 + s * 0.03), int(cheek_w), int(cheek_h))

    # --- 眼罩（黑色斑纹） ---
    mask_w, mask_h = s * 0.19, s * 0.13
    p.setBrush(QColor("#2C2C2C"))
    for side in [-1, 1]:
        mx = cx + side * s * 0.095
        my = cy - s * 0.06
        p.drawEllipse(int(mx - mask_w / 2), int(my - mask_h / 2), int(mask_w), int(mask_h))

    # --- 眼睛 ---
    eye_w, eye_h = s * 0.095, s * 0.1
    p.setBrush(QColor("#FFFFFF"))
    for side in [-1, 1]:
        ex_ = cx + side * s * 0.095
        ey_ = cy - s * 0.06
        p.drawEllipse(int(ex_ - eye_w / 2), int(ey_ - eye_h / 2), int(eye_w), int(eye_h))
    # 瞳孔
    pupil_r = s * 0.04
    p.setBrush(QColor("#111111"))
    for side in [-1, 1]:
        px = cx + side * s * 0.1
        py = cy - s * 0.055
        p.drawEllipse(int(px - pupil_r), int(py - pupil_r), int(pupil_r * 2), int(pupil_r * 2))
    # 高光
    p.setBrush(QColor("#FFFFFF"))
    hl_r = s * 0.018
    for side in [-1, 1]:
        hx = cx + side * s * 0.11
        hy = cy - s * 0.075
        p.drawEllipse(int(hx - hl_r), int(hy - hl_r), int(hl_r * 2), int(hl_r * 2))

    # --- 鼻子 ---
    nose_r = s * 0.04
    p.setBrush(QColor("#1A1A1A"))
    p.drawEllipse(int(cx - nose_r), int(cy - nose_r + s * 0.05), int(nose_r * 2), int(nose_r * 2))

    # --- 嘴巴 ---
    p.setPen(QPen(QColor("#555555"), max(1, s * 0.025)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    mouth_y = cy + s * 0.1
    mouth_half = s * 0.06
    p.drawArc(int(cx - mouth_half), int(mouth_y - mouth_half * 0.5),
              int(mouth_half * 2), int(mouth_half), 0, 180 * 16)

    p.end()
    return QIcon(pix)


def _spawn_and_wait():
    """父进程模式：以子进程方式运行真实应用，父进程持有 console。

    PyInstaller 打包的 exe 双击运行时，若真实应用发生原生崩溃
    （如 0xC0000409，发生在 C++ 层，Python 的 excepthook/faulthandler
    都无法拦截），进程被 Windows 直接终止，console 窗口立即关闭，
    看不到任何错误信息。

    解决思路：真实应用在子进程中运行，父进程只负责 spawn + wait。
    子进程异常/崩溃（退出码非 0）时，父进程保持窗口并提示退出码，
    确保错误信息可见；正常退出（退出码 0）时窗口自动关闭，无感。
    """
    import subprocess

    # 用命令行参数 --child 标记子进程（比环境变量更可靠：Popen 显式传参，
    # 不会因环境继承差异丢失）
    cmd = [sys.executable, '--child'] + sys.argv[1:]
    try:
        proc = subprocess.Popen(cmd)
        code = proc.wait()
    except Exception as e:
        print(f"[RaccoonQuant] 启动子进程失败: {e}", file=sys.stderr, flush=True)
        code = -1

    if code != 0:
        hexcode = code & 0xFFFFFFFF
        print(f"\n===== [RaccoonQuant] 程序异常退出（退出码: {code} / 0x{hexcode:08X}）=====",
              file=sys.stderr, flush=True)
        print("窗口保持打开以便查看上方错误信息，按回车键关闭窗口...",
              file=sys.stderr, flush=True)
        try:
            input()
        except Exception:
            pass


def _run_app():
    _install_crash_handler()
    # 初始化系统日志：日志写入 <安装目录>\log\YYYY-MM-DD_系统.log，
    # 并挂接全局未捕获异常到系统日志（链式保留调试模式控制台行为）
    try:
        from log_manager import setup_system_logging, get_logger
        setup_system_logging()
        _sys_log = get_logger('system')
        _sys_log.info('应用启动，Python=%s，frozen=%s',
                      sys.version.split()[0], getattr(sys, 'frozen', False))
    except Exception:
        _sys_log = None
    # faulthandler：发生原生崩溃（段错误/abort）时把 Python 堆栈打印到控制台，
    # 便于定位崩溃位置（对 Python 层异常无效的 C++ 层崩溃兜底）
    try:
        import faulthandler
        faulthandler.enable()
    except Exception:
        pass
    # 诊断模式（RACCOON_DIAG=1）：后台执行网络自检，结果写入 %TEMP%\RaccoonQuant_diag.log
    if os.environ.get('RACCOON_DIAG', '0').strip() == '1':
        try:
            import asyncio
            import threading
            from diag import log as _dlog, network_diag as _ndiag
            threading.Thread(target=lambda: asyncio.run(_ndiag()),
                             daemon=True, name='diag-net').start()
            _dlog('诊断模式已开启，网络自检已启动')
        except Exception as _de:
            _dlog('诊断启动失败: %r' % (_de,))
    app = QApplication(sys.argv)

    # 兜底：无论从哪条路径退出，都先确保所有后台线程结束，
    # 防止运行中的 QThread 在进程退出时被析构导致 0xC0000409 崩溃
    from ui import thread_utils

    def _on_about_to_quit():
        if _sys_log:
            _sys_log.info('应用退出，正在停止后台线程...')
        remaining = thread_utils.shutdown_all(timeout_ms=10000)
        if remaining:
            if _sys_log:
                _sys_log.warning('仍有 %d 个后台线程未结束，强制退出', len(remaining))
            os._exit(0)
        if _sys_log:
            _sys_log.info('应用正常退出')

    app.aboutToQuit.connect(_on_about_to_quit)
    app.setStyle('Fusion')
    app.setQuitOnLastWindowClosed(False)  # 托盘应用，手动控制退出

    # 单实例检测：文件锁优先（防竞态），QLocalServer 通知已有实例显示窗口
    lock = _acquire_single_lock()
    if lock is None:
        # 已有实例在运行：通知它显示窗口后退出
        try:
            _notify_existing_instance()
        except Exception:
            pass
        sys.exit(0)

    # 设置应用图标（小浣熊）
    app.setWindowIcon(_create_raccoon_icon(64))

    # 全局样式
    app.setStyleSheet("""
        QMainWindow { background-color: #1A1A2E; }
        QWidget { color: #FFFFFF; font-family: "Microsoft YaHei", "SimHei", sans-serif; }
        QToolTip { background-color: #0F3460; color: #F39C12; border: 1px solid #F39C12;
                   padding: 4px; border-radius: 4px; }
        QScrollBar:vertical { background: #1A1A2E; width: 8px; }
        QScrollBar::handle:vertical { background: #2A2A4A; border-radius: 4px; min-height: 30px; }
        QScrollBar::handle:vertical:hover { background: #F39C12; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    """)

    window = MainWindow()
    window.showMaximized()

    sys.exit(app.exec())


def main():
    # PyInstaller 打包的 exe（双击/命令行直接运行）时采用子进程包装：
    # 真实应用在子进程中运行，父进程持有 console 等待其退出。
    # 若发生原生崩溃（如 0xC0000409），子进程退出码非 0，
    # 父进程保持窗口并提示，避免闪退后看不到任何错误信息。
    # 源码方式（python main.py）直接运行，保持原有行为。
    if getattr(sys, 'frozen', False) and '--child' not in sys.argv:
        _spawn_and_wait()
        return
    _run_app()


if __name__ == '__main__':
    main()
