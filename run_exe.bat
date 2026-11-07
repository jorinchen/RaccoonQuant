@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title RaccoonQuant - 启动（保持窗口）
cd /d "%~dp0"

set "EXE=%~dp0dist\RaccoonQuant.exe"

if not exist "%EXE%" (
    echo [错误] 未找到 "%EXE%"
    echo        请先运行 build.bat 打包，或检查 dist 目录。
    pause
    exit /b 1
)

echo ========================================
echo   小浣熊量化选股 - 启动
echo   说明: 即使程序闪退，本窗口也会保持打开
echo   以显示错误信息（退出码等）。
echo ========================================
echo.

"%EXE%"
set "EXIT_CODE=!ERRORLEVEL!"

echo.
echo ========================================
echo   程序已退出（退出码: !EXIT_CODE!）
if "!EXIT_CODE!"=="0" (
    echo   正常退出。
) else (
    echo   异常退出！请查看上方错误信息。
)
echo   按任意键关闭窗口...
echo ========================================
pause >nul
endlocal
