@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo ============================================================
echo   会议倒计时提醒系统（多主控版） - 打包成独立 exe
echo ============================================================
echo   当前目录: %CD%
echo.

set PY=python
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [X] 找不到 python，请先安装 Python 3 并加入 PATH
    pause
    exit /b 1
)

%PY% -c "import PyQt5" >nul 2>&1
if errorlevel 1 (
    echo [X] 找不到 PyQt5，正在安装...
    %PY% -m pip install PyQt5
)

%PY% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [i] 未检测到 PyInstaller，正在安装...
    %PY% -m pip install pyinstaller
)

echo.
echo ------------------------------------------------------------
echo [1/2] 正在打包 主控端 MeetingTimerMaster.exe
echo ------------------------------------------------------------
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --workpath _pyi_work --distpath dist ^
    --name MeetingTimerMaster ^
    --version-file build\version_master.txt ^
    --icon assets\master.ico ^
    --add-data "assets\master.ico;assets" ^
    --hidden-import PyQt5.sip ^
    --exclude-module PyQt5.QtWebEngineWidgets ^
    --exclude-module PyQt5.QtQuick ^
    --exclude-module PyQt5.QtQml ^
    --exclude-module tkinter ^
    --exclude-module matplotlib ^
    --exclude-module numpy ^
    run_master.py
if errorlevel 1 (
    echo [X] 主控端打包失败
    pause
    exit /b 1
)

echo.
echo ------------------------------------------------------------
echo [2/2] 正在打包 客户端 MeetingTimerClient.exe
echo ------------------------------------------------------------
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --workpath _pyi_work --distpath dist ^
    --name MeetingTimerClient ^
    --version-file build\version_client.txt ^
    --icon assets\client.ico ^
    --add-data "assets\client.ico;assets" ^
    --hidden-import PyQt5.sip ^
    --exclude-module PyQt5.QtWebEngineWidgets ^
    --exclude-module PyQt5.QtQuick ^
    --exclude-module PyQt5.QtQml ^
    --exclude-module tkinter ^
    --exclude-module matplotlib ^
    --exclude-module numpy ^
    run_client.py
if errorlevel 1 (
    echo [X] 客户端打包失败
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   打包完成！产物在 dist\ 目录：
echo     dist\MeetingTimerMaster.exe   主控端
echo     dist\MeetingTimerClient.exe   客户端
echo.
echo   注意：每个 exe 首次运行会弹 Windows 防火墙询问，
echo         务必勾选"专用网络"并点"允许访问"，否则局域网连不上。
echo ============================================================
dir /b dist\*.exe
pause