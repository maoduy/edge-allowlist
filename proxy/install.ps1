<#
KidProxy installer - run in PowerShell **as Administrator** on the kid's Windows PC.

  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -ExemptUsers dad,mom     # extra never-filtered accounts
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -EnforceUsers kid        # filter ONLY this account

What it does
  1. Installs mitmproxy (mitmdump.exe) + kidproxy.py into C:\Program Files\KidProxy (admin-only folder)
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
  [string]$LogSheetId = "",                      # Google Sheet id to log visited URLs into
  [string]$LogCredentials = "",                  # its service-account json (default: .\kidproxy-sheets.json)
  [string]$MitmVersion = "12.2.3"
)
$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this script as Administrator."
}
$Dir = "C:\Program Files\KidProxy"
$Proxy = "127.0.0.1:$Port"
New-Item -ItemType Directory -Force -Path $Dir, "$Dir\ca" | Out-Null

# 1. mitmproxy binary
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
Stop-ScheduledTask -TaskName "KidProxy" -ErrorAction SilentlyContinue
Stop-Process -Name mitmdump -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Copy-Item (Join-Path $PSScriptRoot "kidproxy.py") $Dir -Force
Copy-Item (Join-Path $PSScriptRoot "sheetlog.py") $Dir -Force
# "kidproxy update" from any terminal, for standard users too
$cmd = Get-Content (Join-Path $PSScriptRoot "kidproxy.cmd") -Raw
$cmd -replace 'set PORT=8080', "set PORT=$Port" |
  Set-Content -Path "$env:SystemRoot\System32\kidproxy.cmd" -Encoding ASCII
$cfg = @{ exemptUsers = @($ExemptUsers); enforceUsers = @($EnforceUsers); logFile = "$Dir\kidproxy.log" }
# URL logging is local by default - read it at http://kidproxy.local/log, no account needed.
$cfg.urlLog = @{ enabled = $true; flushSeconds = 300; sheetAllRequests = $false;
                 localFile = "$Dir\urls.jsonl"; localMaxMB = 20 }
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
$cfg | ConvertTo-Json -Depth 5 | Set-Content -Path "$Dir\kidproxy.json" -Encoding UTF8

# 3. scheduled tasks (SYSTEM, at boot, no time limit, restart on failure) + watchdog
$exe = "$Dir\mitmdump.exe"
$arg = "--listen-host 127.0.0.1 --listen-port $Port --set confdir=`"$Dir\ca`" -s `"$Dir\kidproxy.py`" -q"
$action = New-ScheduledTaskAction -Execute $exe -Argument $arg
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "KidProxy" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
$wd = New-ScheduledTaskAction -Execute "schtasks.exe" -Argument "/Run /TN KidProxy"
$wdt = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "KidProxy Watchdog" -Action $wd -Trigger $wdt -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1)) -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "KidProxy"

# 4. trust the proxy CA machine-wide
$cer = "$Dir\ca\mitmproxy-ca-cert.cer"
for ($i = 0; $i -lt 60 -and -not (Test-Path $cer); $i++) { Start-Sleep -Seconds 1 }
if (-not (Test-Path $cer)) { throw "Proxy did not start (no CA generated). See $Dir\kidproxy.log" }
certutil -addstore -f Root $cer | Out-Null

# 5. browser policies
foreach ($k in "HKLM:\SOFTWARE\Policies\Microsoft\Edge", "HKLM:\SOFTWARE\Policies\Google\Chrome") {
  New-Item -Path $k -Force | Out-Null
  Set-ItemProperty -Path $k -Name ProxyMode -Value "fixed_servers"
  Set-ItemProperty -Path $k -Name ProxyServer -Value $Proxy
  Set-ItemProperty -Path $k -Name DeveloperToolsAvailability -Value 2 -Type DWord
  Set-ItemProperty -Path $k -Name BrowserGuestModeEnabled -Value 0 -Type DWord
  # Edge/Chrome default to Bing, which is not on the allowlist - an address-bar search would
  # just hit a block page. Point search, suggestions and the new tab page at Google instead.
  Set-ItemProperty -Path $k -Name DefaultSearchProviderEnabled -Value 1 -Type DWord
  Set-ItemProperty -Path $k -Name DefaultSearchProviderName -Value "Google"
  Set-ItemProperty -Path $k -Name DefaultSearchProviderSearchURL -Value "https://www.google.com/search?q={searchTerms}"
  Set-ItemProperty -Path $k -Name DefaultSearchProviderSuggestURL -Value "https://www.google.com/complete/search?output=chrome&q={searchTerms}"
  Set-ItemProperty -Path $k -Name NewTabPageLocation -Value "https://www.google.com/"
  New-Item -Path "$k\ExtensionInstallBlocklist" -Force | Out-Null
  Set-ItemProperty -Path "$k\ExtensionInstallBlocklist" -Name "1" -Value "*"
  Remove-Item -Path "$k\ExtensionInstallForcelist" -Recurse -Force -ErrorAction SilentlyContinue  # from the earlier attempt
}
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -Name InPrivateModeAvailability -Value 1 -Type DWord
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Google\Chrome" -Name IncognitoModeAvailability -Value 1 -Type DWord

# 6. Windows-wide proxy for every app and user, and lock the proxy settings UI
$pol = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"
New-Item -Path $pol -Force | Out-Null
Set-ItemProperty -Path $pol -Name ProxySettingsPerUser -Value 0 -Type DWord
$is = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings"
Set-ItemProperty -Path $is -Name ProxyEnable -Value 1 -Type DWord
Set-ItemProperty -Path $is -Name ProxyServer -Value $Proxy
Set-ItemProperty -Path $is -Name ProxyOverride -Value "<local>"
$ie = "HKLM:\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel"
New-Item -Path $ie -Force | Out-Null
Set-ItemProperty -Path $ie -Name Proxy -Value 1 -Type DWord

Write-Host ""
Write-Host "KidProxy installed. Log: $Dir\kidproxy.log"
Get-Content "$Dir\kidproxy.log" -Tail 3
Write-Host "Restart the browser (or the PC). Admin accounts are not filtered; standard users are."
