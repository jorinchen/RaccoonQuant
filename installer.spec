# -*- mode: python ; coding: utf-8 -*-
# 小浣熊量化选股 安装程序打包配置（onedir 模式）
# 将 dist\RaccoonQuant\ 整个目录（含 RaccoonQuant.exe + _internal 组件）
# 与 VERSION、raccoon.png 一起打入 RaccoonQuant-Setup.exe
# 注意：安装器只读取 VERSION 展示版本号，不做版本递增（避免与主程序重复 bump）

import os

block_cipher = None

a = Analysis(
    ['installer.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 整个 onedir 主程序目录（RaccoonQuant.exe + _internal 等）
        (os.path.join(SPECPATH, 'dist', 'RaccoonQuant'), 'RaccoonQuant'),
        # 独立卸载程序（先由 uninstaller.spec 打包到 dist\）
        (os.path.join(SPECPATH, 'dist', 'uninstall.exe'), '.'),
        # 安装器自身用于展示版本号的 VERSION
        (os.path.join(SPECPATH, 'VERSION'), '.'),
        # 安装器自身的图标资源
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
    name='setup',
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
    # 默认安装到 Program Files 需要管理员权限，启动时触发 UAC 提权
    uac_admin=True,
)
