@echo off
setlocal
REM Build the Windows installer. Pure ASCII on purpose: .bat + UTF-8 Chinese = mojibake.

set ISCC1=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
set ISCC2=%ProgramFiles%\Inno Setup 6\ISCC.exe
set ISCC=
if exist "%ISCC1%" set ISCC=%ISCC1%
if "%ISCC%"=="" if exist "%ISCC2%" set ISCC=%ISCC2%

if "%ISCC%"=="" (
  echo [X] Inno Setup 6 not found.
  echo     Download: https://jrsoftware.org/isdl.php  ^(about 5 MB, next-next-finish^)
  echo     Then run this script again.
  exit /b 1
)

if not exist "%~dp0..\dist\jobpilot\jobpilot.exe" (
  echo [X] dist\jobpilot\jobpilot.exe not found.
  echo     Build it first:
  echo       .venv2\Scripts\pyinstaller --noconfirm packaging\jobpilot.spec
  exit /b 1
)

echo [1/2] Compiling installer...
"%ISCC%" "%~dp0inno\jobpilot.iss"
if errorlevel 1 (
  echo [X] Compile failed.
  exit /b 1
)

echo.
echo [2/2] Done. Output:
dir /b "%~dp0..\dist\installer\*.exe"
endlocal
