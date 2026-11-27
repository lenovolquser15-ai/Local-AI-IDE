@echo off
chcp 65001 >nul
setlocal EnableExtensions
title LocalHarness v1.0.8.1 (Vulkan GPU + CPU Guard + Workspaces)

set "ROOT=C:\LocalHarness"
set "PORT=7788"
set "URL=http://127.0.0.1:7788"

cd /d "%ROOT%" || (
    echo [ERROR] Folder %ROOT% not found.
    pause
    exit /b 1
)

rem Sync webui -> harness\webui so latest UI is always served
if exist "%ROOT%\webui\app.js" (
    xcopy /Y /Q "%ROOT%\webui\*" "%ROOT%\harness\webui\" >nul 2>&1
)

rem Stop old tray LocalHarness.exe if running
tasklist /FI "IMAGENAME eq LocalHarness.exe" 2>nul | find /I "LocalHarness.exe" >nul
if %errorlevel%==0 (
    echo [LocalHarness] Stopping old LocalHarness.exe tray instance...
    taskkill /F /IM LocalHarness.exe >nul 2>&1
    timeout /t 1 /nobreak >nul
)

rem Restart any old Python server holding port 7788 so fresh code is loaded
powershell -NoProfile -NonInteractive -Command "$conn = Get-NetTCPConnection -LocalPort 7788 -State Listen -ErrorAction SilentlyContinue; if ($conn) { foreach ($p in ($conn.OwningProcess | Select-Object -Unique)) { if ($p -gt 4) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } }; Start-Sleep -Milliseconds 700 }" >nul 2>&1

set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not exist "%PY%" (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        set "PY=%%I"
        goto :found_py
    )
)
:found_py

if not exist "%PY%" (
    echo [ERROR] Python 3.10+ not found! Install Python or check PATH.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ==============================================================
echo   LocalHarness (Latest) - Vulkan / Goal Review / No-Tool-Use
echo   Server URL: %URL%
echo ==============================================================
echo.

rem Open browser automatically as soon as port 7788 starts listening
start "" /b powershell -NoProfile -NonInteractive -WindowStyle Hidden -Command "for($i=0;$i -lt 40;$i++){try{$c=New-Object Net.Sockets.TcpClient('127.0.0.1',7788);$c.Close();Start-Process '%URL%';break}catch{Start-Sleep -Milliseconds 400}}"

"%PY%" -m harness serve
if errorlevel 1 (
    echo.
    echo [ERROR] LocalHarness exited with code %errorlevel%.
    pause
)
endlocal
