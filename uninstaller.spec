# -*- mode: python ; coding: utf-8 -*-
# 小浣熊量化选股 卸载程序打包配置
# 输出 dist\RaccoonQuant-Uninstall.exe（独立单文件 GUI）
# 被 installer.spec 的 datas 引用，打包进 RaccoonQuant-Setup.exe

import os

block_cipher = None

a = Analysis(
    ['uninstaller.py'],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(SPECPATH, 'VERSION'), '.'),
        (os.path.join(SPECPATH, 'raccoon.png'), '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='uninstall',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, 'raccoon.ico'),
)