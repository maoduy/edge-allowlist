<#
KidNest - tam dung / bat lai (Administrator only).

  kidnest-pause.ps1              -> tam dung cho den khi bat lai bang tay
  kidnest-pause.ps1 -Minutes 60  -> tam dung 60 phut roi tu bat lai
  kidnest-pause.ps1 -Resume      -> bat lai ngay
  kidnest-pause.ps1 -Status      -> dang bat hay dang tam dung?

Pausing stops the proxy AND points the machine back at a direct connection. Those two
have to happen together: stopping the proxy while the machine still routes through it
is what takes the internet away from everyone.

The browser lockdown (extensions, DevTools, InPrivate) is left in place - only the
proxy is lifted.
#>
param(
  [int]$Minutes = 0,
  [switch]$Resume,
  [switch]$Status,
  [int]$Port = 8080
)

$ErrorActionPreference = "SilentlyContinue"
$Dir   = "C:\Program Files\KidNest"
$IS    = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings"
$POL   = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"
$EDGE  = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
$CHROME= "HKLM:\SOFTWARE\Policies\Google\Chrome"
$IEPOL = "HKLM:\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel"
$RESUME_TASK = "KidNest Resume"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  $a = @("-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"")
  if ($Minutes) { $a += @("-Minutes", $Minutes) }
  if ($Resume)  { $a += "-Resume" }
  if ($Status)  { $a += "-Status" }
  try { Start-Process powershell -Verb RunAs -ArgumentList $a -ErrorAction Stop } catch {
    Write-Host "Chi Administrator moi tam dung duoc KidNest." -ForegroundColor Red; Start-Sleep 4
  }
  exit
}

function Set-ProfileProxy($on) {
    # Routing lives in each enforced account's own hive now, so clearing HKLM is not
    # enough - the kid's proxy would survive and that account would stay offline.
    New-PSDrive -PSProvider Registry -Name HKU -Root HKEY_USERS -EA SilentlyContinue | Out-Null
    $profiles = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" -EA SilentlyContinue |
                Where-Object { $_.PSChildName -match '^S-1-5-21-' }
    foreach ($pr in $profiles) {
        $sid  = $pr.PSChildName
        $path = (Get-ItemProperty $pr.PSPath -Name ProfileImagePath -EA SilentlyContinue).ProfileImagePath
        $loaded = $false
        if (-not (Test-Path "HKU:\$sid")) {
            $dat = Join-Path $path "NTUSER.DAT"
            if (-not (Test-Path $dat)) { continue }
            & reg.exe load "HKU\$sid" $dat *> $null
            if ($LASTEXITCODE -ne 0) { continue }
            $loaded = $true
        }
        foreach ($b in "Microsoft\Edge", "Google\Chrome") {
            $k = "HKU:\$sid\Software\Policies\$b"
            if (-not (Test-Path $k)) { continue }
            if ($on) {
                Set-ItemProperty -Path $k -Name ProxyMode   -Value "fixed_servers"
                Set-ItemProperty -Path $k -Name ProxyServer -Value "127.0.0.1:$Port"
            } else {
                Remove-ItemProperty -Path $k -Name ProxyMode, ProxyServer -Force -EA SilentlyContinue
            }
        }
        $uis = "HKU:\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        if (Test-Path $uis) {
            Set-ItemProperty -Path $uis -Name ProxyEnable -Value $(if ($on) { 1 } else { 0 }) -Type DWord -EA SilentlyContinue
        }
        if ($loaded) { [gc]::Collect(); & reg.exe unload "HKU\$sid" *> $null }
    }
}

function Get-State {
  $t = Get-ScheduledTask -TaskName "KidNest" -ErrorAction SilentlyContinue
  if (-not $t) { return "not-installed" }
  if ($t.State -eq "Disabled") { return "paused" }
  return "running"
}

# ---------------------------------------------------------------- status
if ($Status) {
  $s = Get-State
  Write-Host ""
  switch ($s) {
    "not-installed" { Write-Host "  KidNest chua duoc cai." }
    "paused" {
      Write-Host "  KidNest: DANG TAM DUNG - may dang vao mang truc tiep." -ForegroundColor Yellow
      $r = Get-ScheduledTask -TaskName $RESUME_TASK -ErrorAction SilentlyContinue | Get-ScheduledTaskInfo
      if ($r.NextRunTime) { Write-Host "  Se tu bat lai luc $($r.NextRunTime)" }
    }
    default {
      $up = [bool](Get-Process mitmdump -ErrorAction SilentlyContinue)
      Write-Host "  KidNest: DANG BAT. Proxy $(if ($up) { 'dang chay' } else { 'KHONG chay (!)' })" `
        -ForegroundColor $(if ($up) { "Green" } else { "Red" })
    }
  }
  Write-Host ""
  exit 0
}

# ---------------------------------------------------------------- resume
if ($Resume) {
  Set-ItemProperty -Path $EDGE   -Name ProxyMode   -Value "fixed_servers"
  Set-ItemProperty -Path $EDGE   -Name ProxyServer -Value "127.0.0.1:$Port"
  Set-ItemProperty -Path $CHROME -Name ProxyMode   -Value "fixed_servers"
  Set-ItemProperty -Path $CHROME -Name ProxyServer -Value "127.0.0.1:$Port"
  Set-ItemProperty -Path $POL    -Name ProxySettingsPerUser -Value 0 -Type DWord
  Set-ItemProperty -Path $IS     -Name ProxyEnable -Value 1 -Type DWord
  Set-ItemProperty -Path $IS     -Name ProxyServer -Value "127.0.0.1:$Port"
  Set-ItemProperty -Path $IEPOL  -Name Proxy -Value 1 -Type DWord

  Set-ProfileProxy $true
  Enable-ScheduledTask -TaskName "KidNest" | Out-Null
  Enable-ScheduledTask -TaskName "KidNest Watchdog" | Out-Null
  Start-ScheduledTask  -TaskName "KidNest"
  Unregister-ScheduledTask -TaskName $RESUME_TASK -Confirm:$false

  # do not leave the machine routed at a proxy that did not come back up
  $up = $false
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    if (Get-Process mitmdump -ErrorAction SilentlyContinue) { $up = $true; break }
  }
  if (-not $up) {
    Write-Host "  Proxy khong khoi dong lai duoc - de may o che do truc tiep." -ForegroundColor Red
    Set-ItemProperty -Path $IS -Name ProxyEnable -Value 0 -Type DWord
    Remove-ItemProperty -Path $EDGE   -Name ProxyMode, ProxyServer
    Remove-ItemProperty -Path $CHROME -Name ProxyMode, ProxyServer
    & netsh winhttp reset proxy | Out-Null
    Write-Host "  Hay chay diagnose.ps1 de biet ly do."
    exit 1
  }
  & gpupdate /force | Out-Null
  Write-Host ""
  Write-Host "  KidNest DA BAT LAI. Dong han trinh duyet roi mo lai." -ForegroundColor Green
  Write-Host ""
  exit 0
}

# ---------------------------------------------------------------- pause
if ((Get-State) -eq "not-installed") { Write-Host "KidNest chua duoc cai."; exit 1 }

Disable-ScheduledTask -TaskName "KidNest Watchdog" | Out-Null
Disable-ScheduledTask -TaskName "KidNest" | Out-Null
Stop-ScheduledTask    -TaskName "KidNest"
Stop-Process -Name mitmdump -Force

# the proxy is gone, so nothing must still be pointed at it - including each
# enforced account's own hive, which is where routing actually lives
Set-ProfileProxy $false
Set-ItemProperty    -Path $IS -Name ProxyEnable -Value 0 -Type DWord
Remove-ItemProperty -Path $EDGE   -Name ProxyMode, ProxyServer
Remove-ItemProperty -Path $CHROME -Name ProxyMode, ProxyServer
Remove-ItemProperty -Path $POL    -Name ProxySettingsPerUser
Remove-ItemProperty -Path $IEPOL  -Name Proxy
& netsh winhttp reset proxy | Out-Null
& gpupdate /force | Out-Null

if ($Minutes -gt 0) {
  $when = (Get-Date).AddMinutes($Minutes)
  $act  = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-ExecutionPolicy Bypass -File `"$Dir\kidnest-pause.ps1`" -Resume"
  $trg  = New-ScheduledTaskTrigger -Once -At $when
  $pr   = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $set  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $RESUME_TASK -Action $act -Trigger $trg -Principal $pr -Settings $set -Force | Out-Null
}

Write-Host ""
Write-Host "  KidNest DA TAM DUNG. May dang vao mang truc tiep, khong loc." -ForegroundColor Yellow
if ($Minutes -gt 0) {
  Write-Host "  Se tu bat lai luc $((Get-Date).AddMinutes($Minutes).ToString('HH:mm'))."
} else {
  Write-Host "  Bat lai bang:  kidnest resume"
}
Write-Host "  Dong han trinh duyet roi mo lai."
Write-Host ""
