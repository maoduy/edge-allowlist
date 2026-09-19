@echo off
REM Force KidNest to re-read the allowlist from the Google Sheet right now.
REM Usage:  kidnest update   (or just: kidproxy)
setlocal
set PORT=8080
if /I "%~1"=="status" (set ACTION=/) else (set ACTION=/update)
curl.exe -s --max-time 60 -x http://127.0.0.1:%PORT% "http://kidnest.local%ACTION%"
if errorlevel 1 echo Khong ket noi duoc KidNest tren 127.0.0.1:%PORT%.
endlocal
