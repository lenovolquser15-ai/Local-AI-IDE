@echo off
rem Build LocalHarness-Setup installer. Requires the clean release build first:
rem   LH_BUILD_DIST=dist/release build-exe.cmd
setlocal
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
  exit /b 1
)
if not exist "dist\release\LocalHarness\LocalHarness.exe" (
  echo Release build missing. Run: LH_BUILD_DIST=dist/release build-exe.cmd
  exit /b 1
)
"%ISCC%" installer.iss