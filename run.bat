@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 人格中枢

if not exist "%~dp0data" mkdir "%~dp0data"

rem ---- 优先用同目录的 exe（不依赖任何 Python 环境）
if exist "%~dp0persona-hub.exe" (
    "%~dp0persona-hub.exe"
    set "RC=%ERRORLEVEL%"
    if not "%RC%"=="0" goto FAILED
    exit /b 0
)

rem ---- 没有 exe 就退回源码运行
rem 注意：Windows 的「应用执行别名」会放一个 python.exe 存根在 WindowsApps 下，
rem where 能找到它，但运行它会弹出微软应用商店——表现就是「双击后什么都没发生」。
where python >nul 2>nul
if errorlevel 1 goto NOPY

set "ISSTUB="
for /f "delims=" %%i in ('where python 2^>nul') do (
    echo %%i | find /i "WindowsApps" >nul
    if not errorlevel 1 set "ISSTUB=1"
)
if defined ISSTUB goto STUB

echo   正在启动…（用 Python 运行源码）
echo.
python hub.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto FAILED
exit /b 0

:NOPY
echo   [×] 没有找到 python 命令。
echo.
echo   两种运行方式：
echo     1) 直接用 persona-hub.exe（推荐，不需要 Python）
echo     2) 装 Python 3.10 或以上，安装时勾选 "Add Python to PATH"
echo.
pause
exit /b 1

:STUB
echo   [×] 找到的 python 是 Windows 的「应用执行别名」，不是真的 Python。
echo       运行它只会弹出微软应用商店——所以看起来像「什么都没发生」。
echo.
echo   解决办法（任选一个）：
echo     1) 用 persona-hub.exe（推荐）
echo     2) 装真 Python 后，到「设置 → 应用 → 高级应用设置 → 应用执行别名」
echo        把 python.exe / python3.exe 两个开关关掉，再重新打开本程序
echo.
pause
exit /b 1

:FAILED
echo.
echo   [×] 启动失败，退出码 %RC%。
echo.
echo   可以试试：
echo     - 关掉所有已经打开的人格中枢窗口后重试
echo     - 确认 8799 端口没被别的程序占着
echo.
pause
exit /b 1
