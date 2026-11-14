@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
set "PYARGS="
if exist "%PYTHON%" goto :python_ready
set "PYTHON=py"
set "PYARGS=-3.10"
:python_ready
set "DEST=dist"
if defined LH_BUILD_DIST set "DEST=%LH_BUILD_DIST%"
if not exist "%DEST%" mkdir "%DEST%"
if errorlevel 1 exit /b 1
set "TAG=%RANDOM%-%RANDOM%"
set "STAGE=%DEST%\.stage-%TAG%"
set "PREVIOUS=%DEST%\.previous-%TAG%"
if exist "%STAGE%" exit /b 1
if exist "%PREVIOUS%" exit /b 1

rem Build separately; the installed copy is untouched until every step succeeds.
"%PYTHON%" %PYARGS% -m PyInstaller --noconfirm --clean --onedir --name LocalHarness --console --distpath "%STAGE%" --specpath build --hidden-import dns.resolver --hidden-import dns.query --hidden-import dns.rdatatype --hidden-import psutil --hidden-import requests --hidden-import rich --hidden-import pystray._win32 --hidden-import qrcode --hidden-import qrcode.image.svg lh_entry.py
if errorlevel 1 goto :fail
xcopy /e /i /y harness\webui "%STAGE%\LocalHarness\webui" >nul
if errorlevel 1 goto :fail
xcopy /e /i /y harness\webui webui >nul
if errorlevel 1 goto :fail
xcopy /e /i /y plugins "%STAGE%\LocalHarness\plugins" >nul
if errorlevel 1 goto :fail
copy /y LICENSE "%STAGE%\LocalHarness\LICENSE" >nul
if errorlevel 1 goto :fail
copy /y README.md "%STAGE%\LocalHarness\README.md" >nul
if errorlevel 1 goto :fail

"%PYTHON%" %PYARGS% -c "import os,sys,psutil; target=os.path.normcase(os.path.abspath(sys.argv[1])); busy=any(os.path.normcase(p.info.get('exe') or '')==target for p in psutil.process_iter(['exe'])); sys.exit(1 if busy else 0)" "%DEST%\LocalHarness\LocalHarness.exe"
if errorlevel 1 (
    echo Close the installed LocalHarness before rebuilding. New build remains in "%STAGE%".
    exit /b 1
)
for %%d in (data work plugins) do (
    if exist "%DEST%\LocalHarness\%%d" (
        xcopy /e /i /y "%DEST%\LocalHarness\%%d" "%STAGE%\LocalHarness\%%d" >nul
        if errorlevel 1 goto :fail
    )
)
if exist "%DEST%\LocalHarness\config.json" (
    copy /y "%DEST%\LocalHarness\config.json" "%STAGE%\LocalHarness\config.json" >nul
    if errorlevel 1 goto :fail
)
if exist "%DEST%\LocalHarness" (
    move "%DEST%\LocalHarness" "%PREVIOUS%" >nul
    if errorlevel 1 goto :fail
)
move "%STAGE%\LocalHarness" "%DEST%\LocalHarness" >nul
if errorlevel 1 goto :rollback
rmdir "%STAGE%"
echo Build complete: %DEST%\LocalHarness\LocalHarness.exe
if exist "%PREVIOUS%" echo Previous build and data retained in: %PREVIOUS%
exit /b 0

:rollback
if exist "%PREVIOUS%" move "%PREVIOUS%" "%DEST%\LocalHarness" >nul
:fail
echo Build failed. Installed data and staged files were kept; check output above.
exit /b 1
