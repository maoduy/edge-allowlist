<#
KidNest installer - run in PowerShell **as Administrator** on the kid's Windows PC.

  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -ExemptUsers dad,mom     # extra never-filtered accounts
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -EnforceUsers kid        # filter ONLY this account

What it does
  1. Installs mitmproxy (mitmdump.exe) + the addon into C:\Program Files\KidNest (admin-only folder)
  2. Runs it as a SYSTEM scheduled task at boot (+ a 5-minute watchdog). Standard users cannot stop it.
  3. Trusts the proxy's certificate machine-wide (needed to inspect youtube.com for the channel filter)
  4. Logs every page the filtered accounts visit, readable at http://kidproxy.local/log
     (row = URL, column = day, cell = number of hits; blocked attempts listed separately).
     Pass -LogSheetId <id> to also mirror it into a Google Sheet, one tab per month "MM-yyyy".
  5. Forces Google as the default search provider and new tab page (Bing is not on the allowlist).
  6. Points Edge, Chrome and the Windows system proxy at 127.0.0.1:8080 by policy, locks the proxy UI,
     blocks extensions / DevTools / InPrivate so the proxy cannot be bypassed from inside the browser.
Members of the local Administrators group are never filtered; everyone else is.
#>
param(
  [string[]]$ExemptUsers = @(),
  [string[]]$EnforceUsers = @(),
  [int]$Port = 8080,
  # Default control sheet for this build; the setup window pre-fills the same one and
  # lets it be replaced. Keep in step with DEFAULT_SHEET in setup/kidnest_setup.py.
  [string]$SheetId = "1VtlZ1FJlUmRQ3VDx7Gzlve7LZ-oHo79P9iFbNj-9VCs",
  [string]$SitesTab = "websites",
  [string]$ChannelsTab = "youtube",
  [string]$LogSheetId = "",                      # Google Sheet id to log visited URLs into
  [string]$LogCredentials = "",                  # its service-account json (default: .\kidproxy-sheets.json)
  [string]$MitmVersion = "12.2.3"
)
$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this script as Administrator."
}
$Dir  = "C:\Program Files\KidNest"
# Runtime files go to ProgramData, never Program Files. Controlled Folder Access blocks
# unrecognised binaries from writing into Program Files without saying so, which makes a
# working proxy look like one that never started.
$Data = "C:\ProgramData\KidNest"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
# admins and SYSTEM may write; the filtered accounts may read their own log, nothing more
& icacls $Data /inheritance:r /grant "*S-1-5-18:(OI)(CI)F" /grant "*S-1-5-32-544:(OI)(CI)F" `
         /grant "*S-1-5-32-545:(OI)(CI)RX" *> $null
# Retire a previous KidProxy install so the two do not both hold the port.
foreach ($t in "KidProxy Watchdog", "KidProxy") {
  if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the old $t task"
  }
}
$legacy = "C:\Program Files\KidProxy"
if ((Test-Path $legacy) -and -not (Test-Path $Dir)) {
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  foreach ($keep in "ca","urllog-state.json","urls.jsonl","usage-state.json","channel-ids.json") {
    if (Test-Path "$legacy\$keep") { Copy-Item "$legacy\$keep" $Dir -Recurse -Force }   # keep history + CA
  }
  Write-Host "Carried the certificate and history over from KidProxy"
}
foreach ($old in $legacy, $Dir) {                 # state used to sit next to the program
  foreach ($f in "ca", "kidproxy.log", "urls.jsonl", "urllog-state.json",
                 "usage-state.json", "channel-ids.json") {
    if ((Test-Path "$old\$f") -and -not (Test-Path "$Data\$f")) {
      Move-Item "$old\$f" "$Data\$f" -Force -ErrorAction SilentlyContinue
      Write-Host "Moved $f to $Data"
    }
  }
}
$Proxy = "127.0.0.1:$Port"
New-Item -ItemType Directory -Force -Path $Dir, "$Data\ca" | Out-Null

# 1. mitmproxy binary
$bundled = Join-Path $PSScriptRoot "mitmdump.exe"
if ((-not (Test-Path "$Dir\mitmdump.exe")) -and (Test-Path $bundled)) {
  Copy-Item $bundled $Dir -Force                      # shipped inside KidNest Setup.exe
  Write-Host "Using the bundled mitmproxy"
}
if (-not (Test-Path "$Dir\mitmdump.exe")) {
  Write-Host "Downloading mitmproxy $MitmVersion ..."
  $zip = Join-Path $env:TEMP "mitmproxy-$MitmVersion.zip"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri "https://downloads.mitmproxy.org/$MitmVersion/mitmproxy-$MitmVersion-windows-x86_64.zip" -OutFile $zip -UseBasicParsing
  $ex = Join-Path $env:TEMP "mitmproxy-$MitmVersion"
  Expand-Archive -Path $zip -DestinationPath $ex -Force
  Copy-Item (Join-Path $ex "mitmdump.exe") $Dir -Force
  Remove-Item $zip, $ex -Recurse -Force -ErrorAction SilentlyContinue
}

# 2. addon + config (stop a running instance first so the new code is picked up)
Stop-ScheduledTask -TaskName "KidNest" -ErrorAction SilentlyContinue
Stop-Process -Name mitmdump -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Copy-Item (Join-Path $PSScriptRoot "kidproxy.py") $Dir -Force
Copy-Item (Join-Path $PSScriptRoot "sheetlog.py") $Dir -Force
$reset = Join-Path $PSScriptRoot "reset-clean.ps1"
if (Test-Path $reset) { Copy-Item $reset $Dir -Force }        # doubles as the uninstaller
$unlock = Join-Path $PSScriptRoot "KidNest-Unlock.bat"        # for when the proxy dies
if (Test-Path $unlock) { Copy-Item $unlock $Dir -Force }
foreach ($extra in "kidnest-pause.ps1", "diagnose.ps1") {
  $src = Join-Path $PSScriptRoot $extra
  if (Test-Path $src) { Copy-Item $src $Dir -Force }
}
# A one-click way back, on the desktop of whoever installed this - an administrator.
# Not the Public desktop: the kid has no use for it and no need to see it.
if (Test-Path "$Dir\KidNest-Unlock.bat") {
  try {
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "KidNest - Khoi phuc mang.lnk"
    $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $sc.TargetPath = "$Dir\KidNest-Unlock.bat"
    $sc.WorkingDirectory = $Dir
    $sc.Description = "Tra lai ket noi mang neu KidNest gap su co"
    $sc.Save()
  } catch { Write-Host "(could not create the desktop shortcut: $($_.Exception.Message))" }
}
# "kidproxy update" from any terminal, for standard users too
$cmd = Get-Content (Join-Path $PSScriptRoot "kidproxy.cmd") -Raw
$cmd -replace 'set PORT=8080', "set PORT=$Port" |
  Set-Content -Path "$env:SystemRoot\System32\kidnest.cmd" -Encoding ASCII
$cfg = @{ exemptUsers = @($ExemptUsers); enforceUsers = @($EnforceUsers)
          dataDir = $Data; logFile = "$Data\kidproxy.log" }
if ($SheetId) {
  if ($SheetId -match "/spreadsheets/d/([A-Za-z0-9_-]{20,})") { $SheetId = $Matches[1] }
  elseif ($SheetId -match "^([A-Za-z0-9_-]{20,})") { $SheetId = $Matches[1] }   # trailing /edit?usp=...
  $cfg.sheetId = $SheetId; $cfg.sitesTab = $SitesTab; $cfg.channelsTab = $ChannelsTab
  Write-Host "Control sheet: $SheetId (tabs: $SitesTab / $ChannelsTab)"
} else { throw "No control sheet given. Pass -SheetId <link or id>." }
# URL logging is local by default - read it at http://kidproxy.local/log, no account needed.
$cfg.urlLog = @{ enabled = $true; flushSeconds = 300; sheetAllRequests = $false;
                 localFile = "$Data\urls.jsonl"; localMaxMB = 20 }
if ($LogSheetId) {
  # Optional mirror into Google Sheets, for reading the log away from this PC.
  if (-not $LogCredentials) { $LogCredentials = Join-Path $PSScriptRoot "kidproxy-sheets.json" }
  if (-not (Test-Path $LogCredentials)) { throw "The sheet mirror needs a service-account key: $LogCredentials not found." }
  Copy-Item $LogCredentials "$Dir\kidproxy-sheets.json" -Force
  $cfg.urlLog.sheetId = $LogSheetId
  $cfg.urlLog.credentials = "kidproxy-sheets.json"
  Write-Host "URL log -> local + sheet $LogSheetId"
} else {
  Write-Host "URL log -> local only (http://kidproxy.local/log)"
}
# NOT Set-Content -Encoding UTF8: on Windows PowerShell 5.1 that writes a BOM.
[System.IO.File]::WriteAllText("$Dir\kidproxy.json", ($cfg | ConvertTo-Json -Depth 5),
                               (New-Object System.Text.UTF8Encoding($false)))

# 3. scheduled tasks (SYSTEM, at boot, no time limit, restart on failure) + watchdog
$exe = "$Dir\mitmdump.exe"
$arg = "--listen-host 127.0.0.1 --listen-port $Port --set confdir=`"$Data\ca`" -s `"$Dir\kidproxy.py`" -q"
$action = New-ScheduledTaskAction -Execute $exe -Argument $arg
$trigger = New-ScheduledTaskTrigger -AtStartup
try { $trigger.Delay = "PT15S" } catch {}          # let the network come up first
$trigger2 = New-ScheduledTaskTrigger -AtLogOn      # belt and braces if AtStartup is missed
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "KidNest" -Action $action -Trigger $trigger,$trigger2 -Settings $settings -Principal $principal -Force | Out-Null
$wd = New-ScheduledTaskAction -Execute "schtasks.exe" -Argument "/Run /TN KidNest"
# A -Once trigger whose start time is in the past does NOT resume after a reboot, so the
# watchdog stopped running the moment the PC was restarted. Repeat off a daily trigger and
# fire at startup as well.
$wdt = New-ScheduledTaskTrigger -Daily -At (Get-Date).Date.AddMinutes(1)
$wdt.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$wdt2 = New-ScheduledTaskTrigger -AtStartup
$wdSet = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "KidNest Watchdog" -Action $wd -Trigger $wdt,$wdt2 -Settings $wdSet -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "KidNest"

# 4. did it ACTUALLY start? The CA file is not proof - an upgrade carries the old one
# over, so it exists whether or not the proxy ever ran. The log and the process are.
$logFile = "$Data\kidproxy.log"
Remove-Item $logFile -Force -ErrorAction SilentlyContinue
$up = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  if ((Get-Process mitmdump -ErrorAction SilentlyContinue) -and (Test-Path $logFile)) { $up = $true; break }
}
if ($up) {
  Write-Host "Proxy is running."
} else {
  Write-Host ""
  Write-Host "*** KidNest did NOT start. Diagnosing... ***" -ForegroundColor Yellow
  $info = Get-ScheduledTask -TaskName "KidNest" -EA SilentlyContinue | Get-ScheduledTaskInfo
  if ($info) {
    Write-Host ("  task result = 0x{0:X}   last run = {1}" -f $info.LastTaskResult, $info.LastRunTime)
  } else { Write-Host "  the KidNest task is not registered" }
  Write-Host "  mitmdump process: $(if (Get-Process mitmdump -EA SilentlyContinue) { 'running' } else { 'not running' })"
  Write-Host "  log file        : $(if (Test-Path $logFile) { 'written' } else { 'never written' })"
  Write-Host "  starting it by hand to capture the real error..."
  $o = "$env:TEMP\kidnest-start-out.txt"; $e = "$env:TEMP\kidnest-start-err.txt"
  try {
    $ph = Start-Process -FilePath "$Dir\mitmdump.exe" -ArgumentList $arg -PassThru `
            -RedirectStandardOutput $o -RedirectStandardError $e -WindowStyle Hidden -EA Stop
    Start-Sleep -Seconds 10
    if (-not $ph.HasExited) {
      $ph.Kill()
      Write-Host "  -> it RUNS when started by hand, so the scheduled task is what is failing." -ForegroundColor Yellow
      Write-Host "     Check Task Scheduler > KidNest > History, and whether Fast Startup is on."
    } else {
      Write-Host "  -> mitmdump exited immediately with code $($ph.ExitCode):" -ForegroundColor Red
      Get-Content $e, $o -EA SilentlyContinue | Where-Object { $_ } | Select-Object -First 15 |
        ForEach-Object { Write-Host "     $_" }
    }
  } catch {
    Write-Host "  -> could not even launch mitmdump.exe: $($_.Exception.Message)" -ForegroundColor Red
  }
  # NEVER point the machine at a proxy that is not answering. Doing so takes the
  # internet away from every account, including the administrator's - enforceUsers
  # decides who is filtered, not who is routed through the proxy.
  Write-Host "Nothing was pointed at the proxy. Clearing anything an earlier version set," -ForegroundColor Yellow
  Write-Host "this machine keeps working, then stopping." -ForegroundColor Yellow
  Remove-Item "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -Recurse -Force -EA SilentlyContinue
  Remove-Item "HKLM:\SOFTWARE\Policies\Google\Chrome" -Recurse -Force -EA SilentlyContinue
  Remove-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings" `
    -Name ProxySettingsPerUser -Force -EA SilentlyContinue
  Remove-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel" `
    -Name Proxy -Force -EA SilentlyContinue
  Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" `
    -Name ProxyEnable -Value 0 -Type DWord -EA SilentlyContinue
  & netsh winhttp reset proxy | Out-Null
  Write-Host ""
  Write-Host "Install stopped. Your internet is untouched. Send the lines above for help." -ForegroundColor Yellow
  exit 1
}

# 5. trust the proxy CA machine-wide
$cer = "$Data\ca\mitmproxy-ca-cert.cer"
for ($i = 0; $i -lt 20 -and -not (Test-Path $cer); $i++) { Start-Sleep -Seconds 1 }
if (Test-Path $cer) {
  certutil -addstore -f Root $cer | Out-Null
} else {
  Write-Host "No CA generated yet - HTTPS will not work until the proxy starts." -ForegroundColor Yellow
}

# 6+7. Route ONLY the chosen accounts through the proxy.
#
# The proxy used to be set machine-wide (ProxySettingsPerUser=0 + HKLM policies). That
# routed every account through it, administrators included - they were never filtered,
# but they were still routed, so a proxy that failed to start took the whole machine
# off the internet. Policies go into each chosen user's own hive instead.
#
# HKCU\Software\Policies is read-only for a standard user, so the kid cannot undo this,
# while an administrator's own profile is left completely untouched.

function Resolve-Sid($name) {
  try { (New-Object Security.Principal.NTAccount($name)).Translate(
          [Security.Principal.SecurityIdentifier]).Value } catch { $null }
}

$targets = @()
if ($EnforceUsers) {
  $targets = @($EnforceUsers)
} else {
  # S-1-5-32-544, not the word "Administrators": the group name is translated on a
  # localised Windows, and a friend's machine may not be in English.
  $admins = @(Get-LocalGroupMember -SID S-1-5-32-544 -EA SilentlyContinue |
              ForEach-Object { ($_.Name -split '\\')[-1] })
  if (-not $admins) {
    $admins = @(Get-LocalGroupMember -Group Administrators -EA SilentlyContinue |
                ForEach-Object { ($_.Name -split '\\')[-1] })
  }
  $targets = @(Get-LocalUser | Where-Object { $_.Enabled -and $admins -notcontains $_.Name } |
               ForEach-Object { $_.Name })
  $targets = @($targets | Where-Object { $ExemptUsers -notcontains $_ })
}
if (-not $targets) { throw "No accounts to protect. Pass -EnforceUsers <name>." }

New-PSDrive -PSProvider Registry -Name HKU -Root HKEY_USERS -EA SilentlyContinue | Out-Null

foreach ($u in $targets) {
  $sid = Resolve-Sid $u
  if (-not $sid) { Write-Host "Skipping unknown account '$u'" -ForegroundColor Yellow; continue }
  $loaded = $false
  if (-not (Test-Path "HKU:\$sid")) {
    $dat = "C:\Users\$u\NTUSER.DAT"
    if (-not (Test-Path $dat)) { Write-Host "No profile for '$u' yet - log in once, then re-run." -ForegroundColor Yellow; continue }
    & reg.exe load "HKU\$sid" $dat *> $null
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not open '$u' profile (logged in?)" -ForegroundColor Yellow; continue }
    $loaded = $true
  }
  foreach ($b in "Microsoft\Edge", "Google\Chrome") {
    $k = "HKU:\$sid\Software\Policies\$b"
    New-Item -Path $k -Force | Out-Null
    Set-ItemProperty -Path $k -Name ProxyMode   -Value "fixed_servers"
    Set-ItemProperty -Path $k -Name ProxyServer -Value $Proxy
    Set-ItemProperty -Path $k -Name DeveloperToolsAvailability -Value 2 -Type DWord
    Set-ItemProperty -Path $k -Name BrowserGuestModeEnabled -Value 0 -Type DWord
    # Bing is not on the allowlist, so an address-bar search would hit a block page.
    Set-ItemProperty -Path $k -Name DefaultSearchProviderEnabled -Value 1 -Type DWord
    Set-ItemProperty -Path $k -Name DefaultSearchProviderName -Value "Google"
    Set-ItemProperty -Path $k -Name DefaultSearchProviderSearchURL -Value "https://www.google.com/search?q={searchTerms}"
    Set-ItemProperty -Path $k -Name DefaultSearchProviderSuggestURL -Value "https://www.google.com/complete/search?output=chrome&q={searchTerms}"
    Set-ItemProperty -Path $k -Name NewTabPageLocation -Value "https://www.google.com/"
    New-Item -Path "$k\ExtensionInstallBlocklist" -Force | Out-Null
    Set-ItemProperty -Path "$k\ExtensionInstallBlocklist" -Name "1" -Value "*"
    Remove-Item -Path "$k\ExtensionInstallForcelist" -Recurse -Force -EA SilentlyContinue
  }
  Set-ItemProperty -Path "HKU:\$sid\Software\Policies\Microsoft\Edge" -Name InPrivateModeAvailability -Value 1 -Type DWord
  Set-ItemProperty -Path "HKU:\$sid\Software\Policies\Google\Chrome"  -Name IncognitoModeAvailability -Value 1 -Type DWord
  # WinINET proxy for that account, for apps that are not Edge or Chrome
  $uis = "HKU:\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
  New-Item -Path $uis -Force | Out-Null
  Set-ItemProperty -Path $uis -Name ProxyEnable   -Value 1 -Type DWord
  Set-ItemProperty -Path $uis -Name ProxyServer   -Value $Proxy
  Set-ItemProperty -Path $uis -Name ProxyOverride -Value "<local>"
  if ($loaded) { [gc]::Collect(); & reg.exe unload "HKU\$sid" *> $null }
  Write-Host "Protected account: $u"
}

# Make sure no machine-wide proxy survives from an earlier version - that is what used
# to knock the administrator off the internet.
Remove-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxySettingsPerUser -Force -EA SilentlyContinue
Remove-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel" -Name Proxy -Force -EA SilentlyContinue
Set-ItemProperty    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -Value 0 -Type DWord -EA SilentlyContinue
foreach ($k in "HKLM:\SOFTWARE\Policies\Microsoft\Edge", "HKLM:\SOFTWARE\Policies\Google\Chrome") {
  Remove-ItemProperty -Path $k -Name ProxyMode, ProxyServer -Force -EA SilentlyContinue
}

# 8. Add/Remove Programs entry - HKLM, so uninstalling needs an administrator
$unKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\KidNest"
if (Test-Path "$Dir\reset-clean.ps1") {
  New-Item -Path $unKey -Force | Out-Null
  Set-ItemProperty -Path $unKey -Name DisplayName     -Value "KidNest"
  Set-ItemProperty -Path $unKey -Name DisplayVersion  -Value "1.1.0"
  Set-ItemProperty -Path $unKey -Name Publisher       -Value "KidNest"
  Set-ItemProperty -Path $unKey -Name InstallLocation -Value $Dir
  Set-ItemProperty -Path $unKey -Name NoModify        -Value 1 -Type DWord
  Set-ItemProperty -Path $unKey -Name NoRepair        -Value 1 -Type DWord
  Set-ItemProperty -Path $unKey -Name UninstallString `
    -Value "powershell.exe -ExecutionPolicy Bypass -File `"$Dir\reset-clean.ps1`""
}

Write-Host ""
Write-Host "KidNest installed. Log: $Data\kidproxy.log"
Write-Host "If the PC ever loses internet: use the desktop shortcut"
Write-Host "  \"KidNest - Khoi phuc mang\", or run: kidnest unlock"
if (Test-Path "$logFile") { Get-Content $logFile -Tail 3 }
Write-Host "Restart the browser (or the PC). Admin accounts are not filtered; standard users are."
