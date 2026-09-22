<#
KidNest self-test - run it on the machine itself.

    kidnest test                 10 minutes, probes as the account running this
    kidnest test -Minutes 30     longer soak
    kidnest test -AsUser nguye -Password xxx     probe as the child's account

Two halves running at once:
  * a behaviour soak that browses at random like a person would - listed sites,
    unlisted sites, YouTube, the control page - and checks each answer against what
    the rules say should happen;
  * a health sample every minute: is the proxy answering, how many instances, which
    pid owns the port, how much memory, did it restart.

Writes a full report to the Desktop. Send that file when something looks wrong.
#>
param(
  [int]$Minutes = 10,
  [int]$Port = 8080,
  [string]$AsUser = "",
  [string]$Password = ""
)

$Dir  = "C:\Program Files\KidNest"
$Data = "C:\ProgramData\KidNest"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$report = Join-Path ([Environment]::GetFolderPath("Desktop")) "kidnest-test-$stamp.txt"
$lines = New-Object System.Collections.ArrayList
function Add-Line($t) { [void]$lines.Add($t); Write-Host $t }
function Sect($t) { Add-Line ""; Add-Line "===== $t =====" }

$script:Cred = $null
$script:Share = "C:\Users\Public\kidnest-test"
if ($AsUser) {
  $script:Cred = New-Object PSCredential($AsUser, (ConvertTo-SecureString $Password -AsPlainText -Force))
  New-Item -ItemType Directory -Force -Path $script:Share | Out-Null
  & icacls $script:Share /grant "*S-1-1-0:(OI)(CI)M" *>$null      # Everyone, so the child can write
}

function Probe($url, $useProxy = $true, $browserLike = $true) {
  $a = @("-s", "-o", "NUL", "-w", "%{http_code} %{time_total}", "--max-time", "20")
  if ($useProxy) { $a += @("-x", "http://127.0.0.1:$Port") }
  if ($browserLike) { $a += @("-H", "Sec-Fetch-Dest: document", "-H", "Sec-Fetch-Mode: navigate") }
  $a += @("-k", $url)

  if (-not $script:Cred) {
    try { $out = & curl.exe @a 2>$null } catch { $out = "000 0" }
  } else {
    # Actually run as the child. Judging that account's rules while probing from an
    # administrator's session reports failures that are not real - and hides real ones.
    $res = Join-Path $script:Share "probe.txt"
    $cmd = Join-Path $script:Share "probe.cmd"
    Remove-Item $res -Force -EA SilentlyContinue
    $quoted = ($a | ForEach-Object { if ($_ -match '[\s"]') { '"' + $_ + '"' } else { $_ } }) -join " "
    Set-Content $cmd -Encoding ASCII -Value @("@echo off", "curl.exe $quoted > `"$res`" 2>&1")
    & icacls $cmd /grant "*S-1-1-0:(RX)" *>$null
    try {
      Start-Process cmd.exe -Credential $script:Cred -ArgumentList "/c", $cmd `
        -WorkingDirectory $script:Share -Wait -WindowStyle Hidden -EA Stop
    } catch { }
    $out = if (Test-Path $res) { (Get-Content $res -Raw) } else { "000 0" }
  }
  $p = "$out".Trim() -split "\s+"
  if ($p.Count -lt 2 -or $p[0] -notmatch '^\d{3}$') { return @{ Code = "000"; Ms = 0 } }
  return @{ Code = $p[0]; Ms = [int]([double]($p[1]) * 1000) }
}

function NoProxyText($url) {
  # the sheet must be read without going through our own proxy
  try {
    $c = New-Object System.Net.WebClient
    $c.Proxy = $null
    $c.Encoding = [Text.Encoding]::UTF8
    return $c.DownloadString($url)
  } catch { return "" }
}

Add-Line "KidNest self-test  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Line "soak: $Minutes minutes, port $Port"

# ---------------------------------------------------------------- A. snapshot
Sect "machine"
Add-Line ("windows      : " + (Get-CimInstance Win32_OperatingSystem).Caption + " build " + [Environment]::OSVersion.Version)
Add-Line ("booted       : " + (Get-CimInstance Win32_OperatingSystem).LastBootUpTime)
$hb = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -Name HiberbootEnabled -EA SilentlyContinue).HiberbootEnabled
Add-Line "fast startup : $hb   (1 = on, can skip AtStartup triggers)"
Add-Line ("running as   : " + [Security.Principal.WindowsIdentity]::GetCurrent().Name)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Add-Line "elevated     : $isAdmin"

Sect "config"
$cfg = $null
if (Test-Path "$Dir\kidproxy.json") {
  $raw = Get-Content "$Dir\kidproxy.json" -Raw
  Add-Line $raw.Trim()
  try { $cfg = $raw | ConvertFrom-Json } catch { Add-Line "!! config will not parse: $($_.Exception.Message)" }
} else { Add-Line "!! no kidproxy.json" }
$enforced = @(); if ($cfg -and $cfg.enforceUsers) { $enforced = @($cfg.enforceUsers) }

Sect "tasks"
foreach ($n in "KidNest", "KidNest Watchdog") {
  $t = Get-ScheduledTask -TaskName $n -EA SilentlyContinue
  if (-not $t) { Add-Line "$n : NOT REGISTERED"; continue }
  $i = $t | Get-ScheduledTaskInfo
  Add-Line ("{0,-18}: State={1} LastRun={2} Result=0x{3:X} NextRun={4} Missed={5}" -f `
            $n, $t.State, $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime, $i.NumberOfMissedRuns)
}

Sect "processes and port"
function ProcTable {
  $owner = (Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue | Select-Object -First 1).OwningProcess
  $ps = @(Get-Process mitmdump -EA SilentlyContinue)
  $out = @("mitmdump instances: $($ps.Count)  | port $Port owned by pid: $(if($owner){$owner}else{'NOBODY'})")
  foreach ($p in $ps) {
    $mb = [int]($p.WorkingSet64 / 1MB)
    $mark = if ($owner -and $p.Id -eq $owner) { "<- serving" } else { "" }
    $out += ("  pid {0,-6} {1,5} MB  started {2}  {3}" -f $p.Id, $mb, $p.StartTime, $mark)
  }
  return $out
}
ProcTable | ForEach-Object { Add-Line $_ }

Sect "permissions"
foreach ($pth in $Dir, $Data, "$Data\ca", "$Data\kidproxy.log") {
  if (Test-Path $pth) {
    $acl = Get-Acl $pth
    Add-Line ("{0}`n    owner: {1}" -f $pth, $acl.Owner)
    foreach ($r in $acl.Access) { Add-Line ("    {0,-40} {1}" -f $r.IdentityReference, $r.FileSystemRights) }
  } else { Add-Line "$pth : MISSING" }
}
$probe = Join-Path $Data "write-probe.tmp"
try { "x" | Set-Content $probe -EA Stop; Remove-Item $probe -Force; Add-Line "write to $Data : OK" }
catch { Add-Line "write to $Data : BLOCKED -> $($_.Exception.Message)" }
$cfa = (Get-MpPreference -EA SilentlyContinue).EnableControlledFolderAccess
Add-Line "controlled folder access: $cfa   (1 = on, blocks writes into Program Files)"

Sect "proxy routing per account"
New-PSDrive -PSProvider Registry -Name HKU -Root HKEY_USERS -EA SilentlyContinue | Out-Null
$hk = (Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -EA SilentlyContinue).ProxyMode
Add-Line "HKLM Edge ProxyMode : $(if($hk){$hk}else{'(none - good, admins untouched)'})"
foreach ($pr in Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" -EA SilentlyContinue |
                Where-Object { $_.PSChildName -match '^S-1-5-21-' }) {
  $sid = $pr.PSChildName
  $pp = (Get-ItemProperty $pr.PSPath -Name ProfileImagePath -EA SilentlyContinue).ProfileImagePath
  $nm = if ($pp) { Split-Path $pp -Leaf } else { $sid }
  $ld = $false
  if (-not (Test-Path "HKU:\$sid")) {
    $dat = Join-Path $pp "NTUSER.DAT"
    if (Test-Path $dat) { & reg.exe load "HKU\$sid" $dat *>$null; $ld = ($LASTEXITCODE -eq 0) }
  }
  $m = (Get-ItemProperty "HKU:\$sid\Software\Policies\Microsoft\Edge" -EA SilentlyContinue).ProxyMode
  $e = (Get-ItemProperty "HKU:\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -EA SilentlyContinue).ProxyEnable
  Add-Line ("  {0,-16} EdgeProxyMode={1,-14} ProxyEnable={2}  enforced={3}" -f `
            $nm, $(if($m){$m}else{'-'}), $(if($null -ne $e){$e}else{'-'}), ($enforced -contains $nm))
  if ($ld) { [gc]::Collect(); & reg.exe unload "HKU\$sid" *>$null }
}

Sect "certificate"
$c = @(Get-ChildItem Cert:\LocalMachine\Root -EA SilentlyContinue | Where-Object { $_.Subject -like "*mitmproxy*" })
Add-Line "mitmproxy CA in LocalMachine\Root: $($c.Count)"
foreach ($x in $c) { Add-Line "  $($x.Thumbprint)  expires $($x.NotAfter)" }

# ---------------------------------------------------------------- what should be allowed
Sect "rules in force"
$allowed = @(); $limits = @{}
if ($cfg -and $cfg.sheetId) {
  $csv = NoProxyText ("https://docs.google.com/spreadsheets/d/" + $cfg.sheetId + "/gviz/tq?tqx=out:csv&sheet=websites")
  foreach ($row in ($csv -split "`n")) {
    $cells = $row -split '","'
    if ($cells.Count -lt 1) { continue }
    $d = $cells[0].Trim('"', ' ', "`r")
    if (-not $d -or $d -eq "Trang web" -or $d -eq "*") { continue }
    $allowed += $d.ToLower()
  }
}
Add-Line "listed domains read from the sheet: $($allowed.Count)"
if ($allowed.Count -eq 0) { Add-Line "!! could not read the sheet - allow/block expectations will be skipped" }

$unlisted = @("example.com", "minecraft.net", "roblox.com", "tiktok.com", "twitch.tv", "reddit.com") |
            Where-Object { $u = $_; -not ($allowed | Where-Object { $u -eq $_ -or $u.EndsWith("." + $_) }) }
Add-Line "unlisted probes: $($unlisted -join ', ')"

# ---------------------------------------------------------------- B. the soak
Sect "soak"
$me = ([Security.Principal.WindowsIdentity]::GetCurrent().Name -split '\\')[-1]
$probeUser = if ($AsUser) { $AsUser } else { $me }
$amEnforced = $enforced -contains $probeUser
Add-Line "probing as '$probeUser' - enforced by KidNest: $amEnforced"
if ($AsUser) { Add-Line "requests really are issued from that account, not this session." }
if (-not $amEnforced) {
  Add-Line "NOTE: this account is not filtered, so blocked sites are EXPECTED to load."
  Add-Line "      Run with -AsUser <child> -Password <pw> to test what the child sees."
}

$results = New-Object System.Collections.ArrayList
$health  = New-Object System.Collections.ArrayList
$end = (Get-Date).AddMinutes($Minutes)
$nextHealth = Get-Date
$restarts = 0
$logLine0 = 0
if (Test-Path "$Data\kidproxy.log") { $logLine0 = @(Select-String -Path "$Data\kidproxy.log" -Pattern "started;" -EA SilentlyContinue).Count }

$pickAllowed = if ($allowed.Count) { $allowed } else { @("google.com") }

while ((Get-Date) -lt $end) {
  # -------- health sample once a minute
  if ((Get-Date) -ge $nextHealth) {
    $owner = (Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue | Select-Object -First 1).OwningProcess
    $ps = @(Get-Process mitmdump -EA SilentlyContinue)
    $mb = if ($ps.Count) { [int](($ps | Measure-Object WorkingSet64 -Sum).Sum / 1MB) } else { 0 }
    $ctl = Probe "http://kidnest.local/" $true $false
    $now = @(Select-String -Path "$Data\kidproxy.log" -Pattern "started;" -EA SilentlyContinue).Count
    if ($now -gt $logLine0) { $restarts += ($now - $logLine0); $logLine0 = $now }
    $h = ("{0}  answering={1,-4} instances={2} portPid={3,-7} mem={4}MB restartsSoFar={5}" -f `
          (Get-Date -Format "HH:mm:ss"), ($ctl.Code -ne "000"), $ps.Count, $(if($owner){$owner}else{'-'}), $mb, $restarts)
    [void]$health.Add($h); Add-Line "  $h"
    $nextHealth = (Get-Date).AddMinutes(1)
  }

  # -------- one random, human-ish action
  $roll = Get-Random -Minimum 0 -Maximum 100
  $browserLike = (Get-Random -Minimum 0 -Maximum 10) -lt 8      # mostly browser, sometimes an app
  if ($roll -lt 45) {
    $d = $pickAllowed | Get-Random
    $r = Probe "https://$d/" $true $browserLike
    $ok = ($r.Code -match '^[23]')
    [void]$results.Add(@{ Kind = "listed"; Target = $d; Expect = "allow"; Code = $r.Code; Ms = $r.Ms; Ok = $ok })
  } elseif ($roll -lt 75) {
    $d = $unlisted | Get-Random
    $r = Probe "https://$d/" $true $browserLike
    $ok = if ($amEnforced) { $r.Code -eq "403" } else { $r.Code -match '^[23]' }
    [void]$results.Add(@{ Kind = "unlisted"; Target = $d; Expect = $(if($amEnforced){"403"}else{"allow"}); Code = $r.Code; Ms = $r.Ms; Ok = $ok })
  } elseif ($roll -lt 90) {
    $r = Probe "https://www.youtube.com/" $true $browserLike
    $ok = ($r.Code -match '^[23]')
    [void]$results.Add(@{ Kind = "youtube"; Target = "youtube.com"; Expect = "allow"; Code = $r.Code; Ms = $r.Ms; Ok = $ok })
  } else {
    $r = Probe "http://kidnest.local/" $true $false
    $ok = ($r.Code -eq "200")
    [void]$results.Add(@{ Kind = "control"; Target = "kidnest.local"; Expect = "200"; Code = $r.Code; Ms = $r.Ms; Ok = $ok })
  }
  Start-Sleep -Milliseconds (Get-Random -Minimum 700 -Maximum 3000)
}

# ---------------------------------------------------------------- C. verdict
Sect "results"
$total = $results.Count
$bad = @($results | Where-Object { -not $_.Ok })
Add-Line "requests: $total   unexpected: $($bad.Count)"
foreach ($k in @("listed", "unlisted", "youtube", "control")) {
  $g = @($results | Where-Object { $_.Kind -eq $k })
  if (-not $g.Count) { continue }
  $f = @($g | Where-Object { -not $_.Ok }).Count
  $avg = [int](($g | Measure-Object Ms -Average).Average)
  Add-Line ("  {0,-9} n={1,-4} unexpected={2,-4} avg={3}ms" -f $k, $g.Count, $f, $avg)
}
if ($bad.Count) {
  Add-Line ""
  Add-Line "first 20 unexpected answers:"
  foreach ($b in ($bad | Select-Object -First 20)) {
    Add-Line ("  {0,-9} {1,-24} expected {2,-6} got {3}" -f $b.Kind, $b.Target, $b.Expect, $b.Code)
  }
}

Sect "health over time"
$health | ForEach-Object { Add-Line "  $_" }
$down = @($health | Where-Object { $_ -match "answering=False" }).Count
$dupes = @($health | Where-Object { $_ -match "instances=[2-9]" }).Count

Sect "state after the soak"
ProcTable | ForEach-Object { Add-Line $_ }
if (Test-Path "$Data\watchdog.log") {
  Add-Line ""
  Add-Line "watchdog.log (last 20):"
  Get-Content "$Data\watchdog.log" -Tail 20 | ForEach-Object { Add-Line "  $_" }
}
Add-Line ""
Add-Line "kidproxy.log (last 30):"
Get-Content "$Data\kidproxy.log" -Tail 30 -EA SilentlyContinue | ForEach-Object { Add-Line "  $_" }

Sect "VERDICT"
$problems = @()
if ($down)        { $problems += "proxy was not answering in $down of $($health.Count) samples" }
if ($dupes)       { $problems += "more than one mitmdump seen in $dupes samples" }
if ($restarts)    { $problems += "proxy restarted $restarts time(s) during the soak" }
if ($bad.Count -gt [math]::Max(2, $total * 0.05)) { $problems += "$($bad.Count)/$total requests did not match the rules" }
if ($problems.Count) {
  Add-Line "VERDICT: PROBLEMS FOUND"
  $problems | ForEach-Object { Add-Line "  - $_" }
} else {
  Add-Line "VERDICT: HEALTHY - $total requests, no restarts, one instance throughout."
}

$lines | Set-Content -Path $report -Encoding UTF8
Write-Host ""
Write-Host "Report: $report"
