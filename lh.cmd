@echo off
rem LocalHarness launcher: beryot exe, yesli on sobran, inache ishodniki.
setlocal
if exist "C:\LocalHarness\dist\LocalHarness\LocalHarness.exe" (
  start "" "C:\LocalHarness\dist\LocalHarness\LocalHarness.exe"
  exit /b 0
)
set PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe
if not exist "%PY%" set PY=python
cd /d C:\LocalHarness
set PYTHONUTF8=1
"%PY%" -m harness serve
endlocal
