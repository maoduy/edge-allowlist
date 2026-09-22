@echo off
REM Force KidNest to re-read the allowlist from the Google Sheet right now.
REM Usage:  kidnest update   (or just: kidproxy)
setlocal
set PORT=8080
if /I "%~1"=="test" (
  powershell.exe -ExecutionPolicy Bypass -File "%ProgramFiles%\KidNest\kidnest-test.ps1" %2 %3 %4 %5
  exit /b
)
if /I "%~1"=="unlock" (
  powershell.exe -ExecutionPolicy Bypass -File "%ProgramFiles%\KidNest\kidnest-pause.ps1"
  exit /b
)
if /I "%~1"=="pause" (
  powershell.exe -ExecutionPolicy Bypass -File "%ProgramFiles%\KidNest\kidnest-pause.ps1" %2 %3
  exit /b
)
if /I "%~1"=="resume" (
  powershell.exe -ExecutionPolicy Bypass -File "%ProgramFiles%\KidNest\kidnest-pause.ps1" -Resume
  exit /b
)
if /I "%~1"=="uninstall" (
  powershell.exe -ExecutionPolicy Bypass -File "%ProgramFiles%\KidNest\reset-clean.ps1"
  exit /b
)
if /I "%~1"=="status" (set ACTION=/) else (set ACTION=/update)
curl.exe -s --max-time 60 -x http://127.0.0.1:%PORT% "http://kidnest.local%ACTION%"
if errorlevel 1 echo Khong ket noi duoc KidNest tren 127.0.0.1:%PORT%.
endlocal
