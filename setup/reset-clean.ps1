<#
KidNest / KidProxy - full reset to a clean machine.

Removes EVERYTHING configured across every earlier attempt on this PC, so that
"KidNest-Setup.exe" runs against a clean slate:

  * scheduled tasks        KidProxy, KidProxy Watchdog, KidNest, KidNest Watchdog
  * program folders        C:\Program Files\KidProxy, C:\Program Files\KidNest
  * helper commands        System32\kidproxy.cmd, System32\kidnest.cmd
  * the embed page         C:\Kids  (the youtube-nocookie playlists page)
  * the extension attempt  C:\EdgeExt + ExtensionInstallForcelist
  * browser policies       every Edge and Chrome policy we ever set, HKLM and HKCU
  * the Windows proxy      machine-wide AND every user profile's own proxy settings
  * the proxy CA           mitmproxy certificates in the Root stores

EVERYTHING IS BACKED UP FIRST. Each registry key is exported to a .reg file and the
backup folder gets a restore.cmd that puts them all back. Files are copied, not just
deleted. Nothing here is one-way.

Run as Administrator:
    powershell -ExecutionPolicy Bypass -File .\reset-clean.ps1
    powershell -ExecutionPolicy Bypass -File .\reset-clean.ps1 -DryRun    # look, change nothing
#>
param(
  [switch]$DryRun,
  [string]$BackupTo = ""
)

$ErrorActionPreference = "Continue"
# Admin only, by design: this is also the uninstaller, so a standard user must not be
# able to run it. Re-launch elevated, which puts a UAC prompt in the way.
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  $argv = @("-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
  if ($DryRun)   { $argv += "-DryRun" }
  if ($BackupTo) { $argv += @("-BackupTo", "`"$BackupTo`"") }
  try {
    Start-Process powershell -Verb RunAs -ArgumentList $argv -ErrorAction Stop
  } catch {
    Write-Host "KidNest can only be removed by an administrator." -ForegroundColor Red
    Start-Sleep 4
    exit 1
  }
  exit 0
}

if (-not $BackupTo) {
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $BackupTo = Join-Path ([Environment]::GetFolderPath("Desktop")) "kidnest-backup-$stamp"
}
if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $BackupTo | Out-Null }

$done = @()
$regFiles = @()
function Note($what, $state) { $script:done += [pscustomobject]@{ Item = $what; Result = $state } }
function Say($m) { Write-Host $m }

if ($DryRun) { Say "DRY RUN - nothing will be changed.`n" } else { Say "Backup: $BackupTo`n" }

# ---------------------------------------------------------------- 1. stop it running
foreach ($t in "KidNest Watchdog", "KidNest", "KidNest Resume", "KidProxy Watchdog", "KidProxy") {
  if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
    if (-not $DryRun) {
      Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
    }
    Note "task: $t" "removed"
  } else { Note "task: $t" "not present" }
}
$proc = Get-Process mitmdump -ErrorAction SilentlyContinue
if ($proc) {
  if (-not $DryRun) { Stop-Process -Name mitmdump -Force -ErrorAction SilentlyContinue }
  Note "process: mitmdump" "stopped"
} else { Note "process: mitmdump" "not running" }

# ---------------------------------------------------------------- 2. registry
# Whole keys: exported then deleted.
$killKeys = @(
  "HKLM\SOFTWARE\Policies\Microsoft\Edge",
  "HKLM\SOFTWARE\Policies\Google\Chrome",
  "HKCU\SOFTWARE\Policies\Microsoft\Edge",
  "HKCU\SOFTWARE\Policies\Google\Chrome",
  "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\KidNest",
  "HKLM\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel",
  "HKCU\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel"
)
foreach ($k in $killKeys) {
  $ps = $k -replace '^HKLM\\', 'HKLM:\' -replace '^HKCU\\', 'HKCU:\'
  if (Test-Path $ps) {
    $safe = ($k -replace '[\\: ]', '_') + ".reg"
    if (-not $DryRun) {
      $out = Join-Path $BackupTo $safe
      & reg.exe export $k $out /y | Out-Null
      $script:regFiles += $safe
      Remove-Item -Path $ps -Recurse -Force -ErrorAction SilentlyContinue
    }
    Note "registry key: $k" "backed up + removed"
  } else { Note "registry key: $k" "not present" }
}

# Single values inside keys Windows needs to keep.
$killValues = @(
  @{ Key = "HKLM\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"; Names = @("ProxySettingsPerUser") },
  @{ Key = "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings";          Names = @("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL") }
)
foreach ($entry in $killValues) {
  $ps = $entry.Key -replace '^HKLM\\', 'HKLM:\'
  if (Test-Path $ps) {
    $safe = ($entry.Key -replace '[\\: ]', '_') + ".reg"
    if (-not $DryRun) {
      & reg.exe export $entry.Key (Join-Path $BackupTo $safe) /y | Out-Null
      $script:regFiles += $safe
    }
    foreach ($n in $entry.Names) {
      if ($null -ne (Get-ItemProperty -Path $ps -Name $n -ErrorAction SilentlyContinue)) {
        if (-not $DryRun) { Remove-ItemProperty -Path $ps -Name $n -Force -ErrorAction SilentlyContinue }
        Note "value: $($entry.Key)\$n" "backed up + removed"
      }
    }
  }
}

# ---------------------------------------------------------------- 3. every user's own proxy
# ProxyOverride lives in HKCU, so it has to be cleared per profile - including profiles
# that are not logged in, whose hive must be loaded from NTUSER.DAT first.
New-PSDrive -PSProvider Registry -Name HKU -Root HKEY_USERS -ErrorAction SilentlyContinue | Out-Null
$profiles = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" -ErrorAction SilentlyContinue |
            Where-Object { $_.PSChildName -match '^S-1-5-21-' }
foreach ($p in $profiles) {
  $sid  = $p.PSChildName
  $path = (Get-ItemProperty $p.PSPath -Name ProfileImagePath -ErrorAction SilentlyContinue).ProfileImagePath
  $name = if ($path) { Split-Path $path -Leaf } else { $sid }
  $loadedHere = $false
  if (-not (Test-Path "HKU:\$sid")) {
    $dat = Join-Path $path "NTUSER.DAT"
    if (-not (Test-Path $dat)) { continue }
    if ($DryRun) { Note "user proxy: $name" "would load hive"; continue }
    & reg.exe load "HKU\$sid" $dat *> $null
    if ($LASTEXITCODE -ne 0) { Note "user proxy: $name" "hive locked, skipped"; continue }
    $loadedHere = $true
  }
  $is = "HKU:\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
  $changed = @()
  if (Test-Path $is) {
    if (-not $DryRun) {
      & reg.exe export "HKU\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
                (Join-Path $BackupTo "user_${name}_InternetSettings.reg") /y | Out-Null
      $script:regFiles += "user_${name}_InternetSettings.reg"
    }
    foreach ($n in "ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL") {
      if ($null -ne (Get-ItemProperty -Path $is -Name $n -ErrorAction SilentlyContinue)) {
        if (-not $DryRun) { Remove-ItemProperty -Path $is -Name $n -Force -ErrorAction SilentlyContinue }
        $changed += $n
      }
    }
  }
  foreach ($pol in "$sid\Software\Policies\Microsoft\Edge", "$sid\Software\Policies\Google\Chrome",
                   "$sid\Software\Policies\Microsoft\Internet Explorer\Control Panel") {
    if (Test-Path "HKU:\$pol") {
      if (-not $DryRun) {
        $safe = "user_${name}_" + (($pol -split '\\')[-2..-1] -join '_') + ".reg"
        & reg.exe export "HKU\$pol" (Join-Path $BackupTo $safe) /y | Out-Null
        $script:regFiles += $safe
        Remove-Item "HKU:\$pol" -Recurse -Force -ErrorAction SilentlyContinue
      }
      $changed += "policies"
    }
  }
  if ($loadedHere) { [gc]::Collect(); & reg.exe unload "HKU\$sid" *> $null }
  Note "user proxy: $name" $(if ($changed) { "cleared (" + ($changed -join ", ") + ")" } else { "nothing set" })
}

# ---------------------------------------------------------------- 4. certificates
foreach ($store in "Cert:\LocalMachine\Root", "Cert:\CurrentUser\Root") {
  $certs = @(Get-ChildItem $store -ErrorAction SilentlyContinue |
             Where-Object { $_.Subject -like "*mitmproxy*" })
  foreach ($c in $certs) {
    if (-not $DryRun) {
      Export-Certificate -Cert $c -FilePath (Join-Path $BackupTo "ca-$($c.Thumbprint).cer") | Out-Null
      Remove-Item $c.PSPath -Force -ErrorAction SilentlyContinue
    }
    Note "certificate: $($c.Thumbprint) in $store" "backed up + removed"
  }
  if (-not $certs) { Note "certificate in $store" "none" }
}

# ---------------------------------------------------------------- 5. files
$killPaths = @(
  "C:\Program Files\KidNest",
  "C:\ProgramData\KidNest",
  "C:\Program Files\KidProxy",
  "C:\Kids",
  "C:\EdgeExt",
  "$env:SystemRoot\System32\kidnest.cmd",
  "$env:SystemRoot\System32\kidproxy.cmd"
)
# The data dir falls back to TEMP when ProgramData is unwritable, and the installer
# leaves a rescue shortcut on the installing administrator's desktop. Both are easy to
# forget and both keep a "clean" machine dirty.
$killPaths += @(Get-ChildItem "C:\Users\*\Desktop\KidNest*.lnk" -EA SilentlyContinue |
                ForEach-Object { $_.FullName })
$killPaths += "C:\Users\Public\Desktop\KidNest - Khoi phuc mang.lnk"
$killPaths += @(Get-ChildItem "C:\Users\*\AppData\Local\Temp\KidNest" -Directory -EA SilentlyContinue |
                ForEach-Object { $_.FullName })
$killPaths += "C:\Windows\Temp\KidNest"
$killPaths += "$env:TEMP\KidNest"

foreach ($p in $killPaths) {
  if (Test-Path $p) {
    if (-not $DryRun) {
      $dest = Join-Path $BackupTo ("files\" + (Split-Path $p -Leaf))
      New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
      Copy-Item $p $dest -Recurse -Force -ErrorAction SilentlyContinue
      Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue
    }
    Note "path: $p" "backed up + removed"
  } else { Note "path: $p" "not present" }
}

# ---------------------------------------------------------------- 5a. Defender exclusion
# The installer excludes its folder so Defender stops quarantining mitmdump. Undo that.
foreach ($ex in "C:\Program Files\KidNest", "C:\Program Files\KidProxy") {
  $cur = @((Get-MpPreference -EA SilentlyContinue).ExclusionPath)
  if ($cur -contains $ex) {
    if (-not $DryRun) { Remove-MpPreference -ExclusionPath $ex -EA SilentlyContinue }
    Note "defender exclusion: $ex" "removed"
  }
}

# ---------------------------------------------------------------- 5b. WinHTTP proxy
# Separate from the per-user proxy above; Windows services and some apps use this one.
$winhttp = (& netsh winhttp show proxy) -join " "
if ($winhttp -notmatch "Direct access") {
  if (-not $DryRun) { & netsh winhttp reset proxy | Out-Null }
  Note "winhttp proxy" "reset to direct"
} else { Note "winhttp proxy" "already direct" }

# ---------------------------------------------------------------- 6. restore script
if (-not $DryRun -and $regFiles.Count) {
  $lines = @("@echo off",
             "REM Put back every registry key this reset removed.",
             "REM Run as Administrator. Files are restored, certificates are not.",
             "echo Restoring registry...")
  foreach ($f in ($regFiles | Sort-Object -Unique)) { $lines += "reg import `"%~dp0$f`"" }
  $lines += @("echo Done. Copy anything you need back from the files\ folder.", "pause")
  $lines | Set-Content -Path (Join-Path $BackupTo "restore.cmd") -Encoding ASCII
}

# ---------------------------------------------------------------- 7. report
Say ""
$done | Format-Table -AutoSize
Say ""
if ($DryRun) {
  Say "DRY RUN finished - nothing was changed. Run again without -DryRun to apply."
} else {
  Say "Clean. Backup and restore.cmd are in:"
  Say "  $BackupTo"
  Say ""
  Say "Restart the PC, then run KidNest-Setup.exe."
  Say "A restart matters: the browser caches proxy settings and the old CA until it does."
}

# ---------------------------------------------------------------- 8. verify
Say ""
Say "--- state now ---"
$leftovers = 0
foreach ($p in $killPaths) {
  if (Test-Path $p) { Say "STILL THERE: $p"; $leftovers++ }
}
foreach ($k in $killKeys) {
  $ps = $k -replace '^HKLM\\', 'HKLM:\' -replace '^HKCU\\', 'HKCU:\'
  if (Test-Path $ps) { Say "STILL THERE: $k"; $leftovers++ }
}
foreach ($t in "KidNest", "KidNest Watchdog", "KidNest Resume", "KidProxy") {
  if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Say "STILL THERE: task $t"; $leftovers++ }
}
if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\KidNest") {
  Say "STILL THERE: the Add/Remove Programs entry"; $leftovers++
}
foreach ($stray in @(Get-ChildItem "C:\Users\*\Desktop\KidNest*.lnk" -EA SilentlyContinue) +
                   @(Get-ChildItem "C:\Users\*\AppData\Local\Temp\KidNest" -Directory -EA SilentlyContinue)) {
  Say "STILL THERE: $($stray.FullName)"; $leftovers++
}
# a hive that could not be opened means that account was never cleaned
$skipped = @($done | Where-Object { $_.Result -like "*locked*" })
if ($skipped) {
  Say "NOT CLEANED (profile in use - log that user off and re-run):"
  $skipped | ForEach-Object { Say "   $($_.Item)" }
  $leftovers += $skipped.Count
}
$pe = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -EA SilentlyContinue).ProxyEnable
Say ("machine ProxyEnable : " + $(if ($null -eq $pe) { "not set" } else { $pe }))
Say ("mitmproxy CA        : " + @(Get-ChildItem Cert:\LocalMachine\Root -EA SilentlyContinue |
      Where-Object { $_.Subject -like "*mitmproxy*" }).Count + " left")
if ($leftovers -eq 0) { Say "Clean." } elseif (-not $DryRun) { Say "$leftovers item(s) survived - see above." }
