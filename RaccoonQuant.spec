# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 - RaccoonQuant 股票分析系统
"""

import os
import re
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
from PyInstaller.utils.win32 import versioninfo as _vi

block_cipher = None

# ================= 版本号自动递增 =================
# 每次打包自动将 patch 版本 +1，并写入 exe 版本资源
_VERSION_FILE = os.path.join(SPECPATH, 'VERSION')


def _load_version():
    try:
        with open(_VERSION_FILE, 'r', encoding='utf-8') as f:
            txt = f.read().strip()
        if re.fullmatch(r'\d+\.\d+\.\d+', txt):
            return txt
    except Exception:
        pass
    return '1.0.0'


def _bump_version(ver):
    major, minor, patch = (int(x) for x in ver.split('.'))
    return '%d.%d.%d' % (major, minor, patch + 1)


APP_VERSION = _bump_version(_load_version())
with open(_VERSION_FILE, 'w', encoding='utf-8') as f:
    f.write(APP_VERSION)

_version_parts = tuple(int(x) for x in APP_VERSION.split('.'))
version_info = _vi.VSVersionInfo(
    ffi=_vi.FixedFileInfo(
        filevers=_version_parts + (0,),
        prodvers=_version_parts + (0,),
        mask=0x3f,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        _vi.StringFileInfo([
            _vi.StringTable(
                '080404b0',
                [
                    _vi.StringStruct('CompanyName', 'RaccoonQuant'),
                    _vi.StringStruct('FileDescription', 'RaccoonQuant 股票分析系统'),
                    _vi.StringStruct('FileVersion', APP_VERSION),
                    _vi.StringStruct('InternalName', 'RaccoonQuant'),
                    _vi.StringStruct('OriginalFilename', 'RaccoonQuant.exe'),
                    _vi.StringStruct('ProductName', 'RaccoonQuant'),
                    _vi.StringStruct('ProductVersion', APP_VERSION),
                ],
            )
        ]),
        _vi.VarFileInfo([_vi.VarStruct('Translation', [2052, 1200])]),
    ],
)
# ==================================================

# PyQt6 需要的隐藏导入
hidden_imports = [
    'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets', 'PyQt6.QtNetwork',
    'PyQt6.QtMultimedia', 'PyQt6.QtTextToSpeech',
    # edge-tts 神经语音（自动收集全部子模块，避免手工写错模块名）
] + collect_submodules('edge_tts') + [
    'aiohttp', 'numpy', 'chinese_calendar', 'pypinyin',
    # data_fetcher_patch 在 main.py 里通过 try/except 动态 import，PyInstaller 静态分析扫不到，
    # 必须显式声明，否则补丁不会进 EXE，运行时所有修复都失效。
    'data_fetcher_patch',
]

# 收集 PyQt6 平台插件数据
qt_plugin_path = None
try:
    import PyQt6.QtCore
    qt_plugin_path = os.path.join(os.path.dirname(PyQt6.QtCore.__file__), 'Qt6', 'plugins')
except:
    pass

added_files = []
if qt_plugin_path and os.path.isdir(qt_plugin_path):
    platform_dir = os.path.join(qt_plugin_path, 'platforms')
    if os.path.isdir(platform_dir):
        added_files.append((platform_dir, 'PyQt6/Qt6/plugins/platforms'))
    styles_dir = os.path.join(qt_plugin_path, 'styles')
    if os.path.isdir(styles_dir):
        added_files.append((styles_dir, 'PyQt6/Qt6/plugins/styles'))
    imageformats_dir = os.path.join(qt_plugin_path, 'imageformats')
    if os.path.isdir(imageformats_dir):
        added_files.append((imageformats_dir, 'PyQt6/Qt6/plugins/imageformats'))
    # 语音合成后端（Windows SAPI）— 语音读报必需
    tts_dir = os.path.join(qt_plugin_path, 'texttospeech')
    if os.path.isdir(tts_dir):
        added_files.append((tts_dir, 'PyQt6/Qt6/plugins/texttospeech'))
    # 多媒体后端（QMediaPlayer 播放 edge-tts 语音）— ffmpeg/windowsmedia 必需
    mm_dir = os.path.join(qt_plugin_path, 'multimedia')
    if os.path.isdir(mm_dir):
        added_files.append((mm_dir, 'PyQt6/Qt6/plugins/multimedia'))

# 排除不需要的模块以减少体积
excluded_modules = [
    'tkinter', 'unittest', 'test', 'pydoc', 'distutils',
    'setuptools', 'pip', 'wheel', 'matplotlib',
    'jinja2',  # 环境未安装且非必需，消除打包警告
]

# aiohttp 需要的隐藏导入
hidden_imports += [
    'aiohttp.client', 'aiohttp.connector', 'aiohttp.client_ws',
    'aiohttp.client_reqrep', 'aiohttp.client_proto', 'aiohttp.cookiejar',
    'multidict', 'yarl', 'frozenlist',
    'charset_normalizer', 'aiosignal', 'attr', 'attrs',
    # aiohttp C 加速扩展 — 缺失会导致纯 Python 回退极度缓慢
    'aiohttp._http_parser', 'aiohttp._http_writer',
    'aiohttp._websocket',
    'aiohttp.resolver',  # DNS 解析器
    # 确保 SelectorEventLoop 可用（避免子线程回退到 ProactorEventLoop）
    'asyncio.windows_events',
]

# 通达信 TDX SDK (mootdx/tdxpy) — 缺失则股票列表退回分页 HTTP，极慢
hidden_imports += [
    'mootdx', 'mootdx.quotes', 'mootdx.affair', 'mootdx.consts',
    'mootdx.server', 'mootdx.config',
    # mootdx 依赖 tdxpy（非 pytdx）
    'tdxpy', 'tdxpy.hq', 'tdxpy.base_socket_client',
    'tdxpy.crawler', 'tdxpy.logger', 'tdxpy.reader',
    'tdxpy.parser', 'tdxpy.parser.std', 'tdxpy.parser.ext',
    'dateutil', 'dateutil.parser', 'dateutil.tz',
    # socket 网络层必须模块
    'socket', 'select', 'selectors', 'ssl', 'http.client',
]

# pypinyin 拼音数据 — 缺失则搜索功能退回在线 API
hidden_imports += ['pypinyin', 'pypinyin.pinyin_dict', 'pypinyin.style']

# MySQL 驱动（股票列表栏目）
hidden_imports += ['pymysql', 'pymysql.cursors', 'pymysql.converters']

# chinese_calendar 交易日数据
hidden_imports += ['chinese_calendar', 'chinese_calendar.constants']

# numba 的 tbbpool 线程池扩展依赖 oneTBB 运行时 tbb12.dll，
# PyInstaller 无法自动收集，这里显式打包（落到 _internal，与 tbbpool.pyd 同目录）。
_tbb_dll = os.path.join(SPECPATH, 'tbb12.dll')

a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],
    binaries=[(_tbb_dll, '.')] if os.path.exists(_tbb_dll) else [],
    datas=added_files,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
)
a.datas += Tree(
    os.path.join(SPECPATH, 'ui'),
    prefix='ui',
    excludes=['*.pyc', '__pycache__'],
)
# data_fetcher 现已恢复为正常源码模块（data_fetcher.py），由 PyInstaller 随包收集，
# 不再需要 .pyc 形式的运行时注入。

# 版本号文件随包分发：标题栏通过 sys._MEIPASS/VERSION 动态读取，
# 与 exe 文件属性版本保持同一来源（每次打包自动递增）
if os.path.exists(os.path.join(SPECPATH, 'VERSION')):
    a.datas.append(('VERSION', os.path.join(SPECPATH, 'VERSION'), 'DATA'))

pyz = PYZ(
    a.pure,
    cipher=block_cipher,
)

# onedir 模式：主程序 exe 只含脚本和 PYZ（很小），其余二进制与数据文件
# 通过 COLLECT 单独输出到 <dist>/RaccoonQuant/_internal/ 下，与 RaccoonQuant.exe 平级。
# 安装器（installer.py）会递归复制整个 RaccoonQuant 目录到用户选择的安装位置。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RaccoonQuant',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # 不显示控制台窗口
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, 'raccoon.ico'),
    version=version_info,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='RaccoonQuant',
)
