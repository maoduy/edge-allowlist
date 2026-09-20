@echo off
title KidNest - Khoi phuc ket noi mang
REM =====================================================================
REM  Chay file nay khi may khong vao duoc mang vi KidNest/KidProxy.
REM  Chi can bam doi (double-click). No tu xin quyen Administrator.
REM  Khong can mang, khong can go lenh nao.
REM =====================================================================

net session >nul 2>&1
if errorlevel 1 (
  echo Dang xin quyen Administrator...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo ================================================
echo   KidNest - khoi phuc ket noi mang
echo ================================================
echo.

schtasks /end    /tn "KidNest"                >nul 2>&1
schtasks /delete /tn "KidNest"            /f  >nul 2>&1
schtasks /delete /tn "KidNest Watchdog"   /f  >nul 2>&1
schtasks /delete /tn "KidProxy"           /f  >nul 2>&1
schtasks /delete /tn "KidProxy Watchdog"  /f  >nul 2>&1
taskkill /f /im mitmdump.exe                  >nul 2>&1
echo   [1/4] Da dung va go cac tac vu nen.

reg delete "HKLM\SOFTWARE\Policies\Microsoft\Edge"  /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\Google\Chrome"   /f >nul 2>&1
reg delete "HKCU\SOFTWARE\Policies\Microsoft\Edge"  /f >nul 2>&1
reg delete "HKCU\SOFTWARE\Policies\Google\Chrome"   /f >nul 2>&1
echo   [2/4] Da xoa chinh sach trinh duyet.

reg delete "HKLM\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxySettingsPerUser /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel" /v Proxy /f >nul 2>&1
reg add    "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul 2>&1
reg add    "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyOverride /f >nul 2>&1
echo   [3/4] Da tat proxy cho toan may.

netsh winhttp reset proxy >nul 2>&1
gpupdate /force           >nul 2>&1
echo   [4/4] Xong.

echo.
echo ================================================
echo   DA KHOI PHUC. Hay DONG HAN trinh duyet
echo   (ca cua so an) roi mo lai.
echo.
echo   Khong can khoi dong lai may.
echo ================================================
echo.
pause
