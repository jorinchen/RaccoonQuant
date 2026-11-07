@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title RaccoonQuant 打包工具
echo ========================================
echo   RaccoonQuant 股票分析系统 - 打包工具
echo ========================================
echo.
cd /d "%~dp0"

:: 尝试多种 Python 路径
set PY_CMD=

:: 自动探测 Python（读取环境变量配置，并校验 pip 可用）：
::   1) 从注册表的用户/系统 Path 环境变量里逐条目录探测 python.exe（优先使用配置里的真实安装）；
::   2) 再回退到当前会话 PATH 中的 python/python3/py。
:: 仅接受“能正常运行 --version 且带 pip”的解释器。

set "SCAN_LIST="
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "SCAN_LIST=%%b"
if defined SCAN_LIST call :scan_env_path
if defined PY_CMD goto :found_python

set "SCAN_LIST="
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SCAN_LIST=%%b"
if defined SCAN_LIST call :scan_env_path
if defined PY_CMD goto :found_python

for %%p in (python python3 py) do (
    %%p --version >nul 2>&1
    if !errorlevel! equ 0 (
        %%p -m pip --version >nul 2>&1
        if !errorlevel! equ 0 (
            set PY_CMD=%%p
            goto :found_python
        )
    )
)

echo  [错误] 未找到 Python，请先安装 Python 3.10+
echo        下载地址: https://www.python.org/downloads/
pause
exit /b 1

:: 逐条扫描 SCAN_LIST（分号分隔的 Path 环境变量值），仅接受附带 pip 的 python.exe
:scan_env_path
for /f "tokens=1* delims=;" %%i in ("!SCAN_LIST!") do (
    if exist "%%i\python.exe" (
        "%%i\python.exe" -m pip --version >nul 2>&1
        if !errorlevel! equ 0 (
            set "PY_CMD=%%i\python.exe"
            goto :found_python
        )
    )
    set "SCAN_LIST=%%j"
)
if defined SCAN_LIST goto :scan_env_path
exit /b 0

:found_python
echo 检测到 Python:
"%PY_CMD%" --version
echo.

:: 确保 pip 可用
"%PY_CMD%" -m pip --version >nul 2>&1
if !errorlevel! neq 0 (
    echo [错误] pip 未安装，请重新安装 Python 并勾选 "Add Python to PATH"
    pause
    exit /b 1
)

:: 安装 PyInstaller
echo  1、安装 PyInstaller...
"%PY_CMD%" -m pip install pyinstaller --quiet --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
if !errorlevel! neq 0 (
    echo [错误] PyInstaller 安装失败
    pause
    exit /b 1
)

:: 安装项目依赖
echo  2、安装项目依赖...
"%PY_CMD%" -m pip install -r requirements.txt --quiet --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
if !errorlevel! neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

:: 生成图标
echo  3、生成图标...
"%PY_CMD%" generate_icon.py
if !errorlevel! neq 0 (
    echo [警告] 图标生成失败，将使用默认图标
)

:: 清理旧构建
echo  4、开始打包 ...
if exist "dist" rmdir /s /q "dist" 2>nul
if exist "build" rmdir /s /q "build" 2>nul

"%PY_CMD%" run_pyi.py --clean RaccoonQuant.spec

if !errorlevel! neq 0 (
    echo.
    echo [错误] 主程序打包失败，请检查上方错误信息
    pause
    exit /b 1
)

echo.
echo  5、打包卸载程序...
"%PY_CMD%" run_pyi.py --clean uninstaller.spec

if !errorlevel! neq 0 (
    echo.
    echo [错误] 卸载程序打包失败，请检查上方错误信息
    pause
    exit /b 1
)

echo.
echo  6、打包安装程序（可自定义安装位置）...
"%PY_CMD%" run_pyi.py --clean installer.spec

if !errorlevel! neq 0 (
    echo.
    echo [错误] 安装程序打包失败，请检查上方错误信息
    pause
    exit /b 1
)

echo.
echo ========================================
echo   打包完成!
echo   主程序: dist\RaccoonQuant\RaccoonQuant.exe
echo   卸载程序: dist\uninstall.exe
echo   安装包: dist\setup.exe
echo.
echo   使用说明:
echo   1. 运行 RaccoonQuant-Setup.exe，自定义选择安装位置
echo   2. 或直接将 dist\RaccoonQuant 目录复制到任意目录使用
echo ========================================
echo.
pause
