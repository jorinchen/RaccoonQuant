@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

set "CLOSE_MODE=1"
if "%CLOSE_MODE%"=="" set "CLOSE_MODE=0"
if not "%CLOSE_MODE%"=="0" if not "%CLOSE_MODE%"=="1" (
    echo ERROR: 参数错误！用法: run.bat [0^|1]
    echo       0 = 保持窗口，显示运行时错误（默认）
    echo       1 = 自动关闭窗口
    pause
    exit /b 1
)

echo ========================================
echo   小浣熊量化选股
echo   数据源: 腾讯 + 东方财富 + 同花顺 + 通达信 + 雪球
echo   说明: 即使程序闪退，本窗口也会保持打开
if "%CLOSE_MODE%"=="0" (
    echo   运行模式: 0 (调试 - 保持窗口, 显示错误)
) else (
    echo   运行模式: 1 (生产 - 自动关闭)
)
echo ========================================
echo.

set "PYTHON_PATH="

if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_PATH=%~dp0venv\Scripts\python.exe"
    echo INFO: 使用虚拟环境
    goto :found_python
)

:: 自动探测 Python（读取环境变量配置）：
:: 从注册表的用户/系统 Path 环境变量里逐条目录探测 python.exe，
:: 终端会话环境变量未刷新时也能按配置找到 Python。
set "SCAN_LIST="
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "SCAN_LIST=%%b"
if defined SCAN_LIST call :scan_env_path

if not defined PYTHON_PATH (
    set "SCAN_LIST="
    for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SCAN_LIST=%%b"
    if defined SCAN_LIST call :scan_env_path
)

if defined PYTHON_PATH goto :found_python

where python >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYTHON_PATH set "PYTHON_PATH=%%i"
    )
    if defined PYTHON_PATH (
        echo INFO: 使用系统 Python: !PYTHON_PATH!
        goto :found_python
    )
)

where py >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    for /f "delims=" %%i in ('where py 2^>nul') do (
        if not defined PYTHON_PATH set "PYTHON_PATH=%%i"
    )
    if defined PYTHON_PATH (
        echo INFO: 使用 py 启动器
        goto :found_python
    )
)


echo ERROR: 未找到 Python，请安装 Python 3.12+
echo        下载地址: https://www.python.org/downloads/
pause
exit /b 1

:: 逐条扫描 SCAN_LIST（分号分隔的 Path 环境变量值），仅接受附带 pip 的 python.exe
:scan_env_path
for /f "tokens=1* delims=;" %%i in ("!SCAN_LIST!") do (
    if exist "%%i\python.exe" (
        "%%i\python.exe" -m pip --version >nul 2>&1
        if !errorlevel! equ 0 (
            set "PYTHON_PATH=%%i\python.exe"
            echo INFO: 使用系统 Python: %%i\python.exe
            goto :found_python
        )
    )
    set "SCAN_LIST=%%j"
)
if defined SCAN_LIST goto :scan_env_path
exit /b 0

:found_python
echo INFO: Python 路径: !PYTHON_PATH!
echo.

echo INFO: 检查依赖...
"!PYTHON_PATH!" -c "import PyQt6, aiohttp, numpy, chinese_calendar, pypinyin, win32com" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo INFO: 正在安装依赖...
    "!PYTHON_PATH!" -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    if !ERRORLEVEL! NEQ 0 (
        echo WARN: 清华源失败，尝试默认源...
        "!PYTHON_PATH!" -m pip install -r "%~dp0requirements.txt"
    )
)

echo INFO: 启动小浣熊量化选股...

set "APP_DIR=%~dp0"
set "APP_DIR=!APP_DIR:\=/!"
set "APP_DIR=!APP_DIR:~0,-1!"
"!PYTHON_PATH!" -X utf8 -c "import sys; sys.path.insert(0, '!APP_DIR!'); from ui.main_window import MainWindow; print('OK')" 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo ERROR: 模块加载失败，请检查依赖是否完整
    pause
    exit /b 1
)

if "%CLOSE_MODE%"=="0" (
    echo.
    echo INFO: 调试模式 - 运行时错误将显示在下方:
    echo ========================================
    "!PYTHON_PATH!" -X utf8 "%~dp0main.py"
    set "EXIT_CODE=!ERRORLEVEL!"
    echo ========================================
    echo.
    echo INFO: 程序已退出（退出码: !EXIT_CODE!），按任意键关闭...
    pause >nul
) else (
    set "PYTHONW=!PYTHON_PATH:\python.exe=\pythonw.exe!"
    if exist "!PYTHONW!" (
        start "" "!PYTHONW!" -X utf8 "%~dp0main.py"
    ) else (
        start "" "!PYTHON_PATH!" -X utf8 "%~dp0main.py"
    )
)
endlocal
