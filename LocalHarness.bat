@echo off
chcp 65001 >nul
setlocal EnableExtensions
title LocalHarness Server (Dev / Latest)

set "ROOT=C:\LocalHarness"
set "URL=http://127.0.0.1:7788"

cd /d "%ROOT%" || (
    echo [ERROR] Folder %ROOT% not found.
    pause
    exit /b 1
)

rem Stop old installed LocalHarness.exe in tray if it occupies port 7788
tasklist /FI "IMAGENAME eq LocalHarness.exe" 2>nul | find /I "LocalHarness.exe" >nul
if %errorlevel%==0 (
    echo Stopping old LocalHarness.exe so the latest version can start on port 7788...
    taskkill /F /IM LocalHarness.exe >nul 2>&1
    timeout /t 1 /nobreak >nul
)

rem If our fresh Python server is already listening on 7788, just open browser
powershell -NoProfile -NonInteractive -Command "try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 7788); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
    echo LocalHarness is already running on %URL%. Opening browser...
    start "" "%URL%"
    exit /b 0
)

set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not exist "%PY%" (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        set "PY=%%I"
        goto :found_py
    )
)
:found_py

if not exist "%PY%" (
    echo [ERROR] Python not found! Install Python 3.10+ or check PATH.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ==================================================
echo   LocalHarness (Latest) - Starting on %URL%
echo ==================================================
echo.

rem Open browser automatically once port 7788 is up
start "" /b powershell -NoProfile -NonInteractive -WindowStyle Hidden -Command "for($i=0;$i -lt 30;$i++){try{$c=New-Object Net.Sockets.TcpClient('127.0.0.1',7788);$c.Close();Start-Process '%URL%';break}catch{Start-Sleep -Milliseconds 500}}"

"%PY%" -m harness serve
if errorlevel 1 (
    echo.
    echo [ERROR] LocalHarness stopped with exit code %errorlevel%.
    pause
)
endlocal
