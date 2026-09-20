<#
KidNest diagnostics - run as Administrator:
    powershell -ExecutionPolicy Bypass -File .\diagnose.ps1
Writes kidnest-diagnose.txt to your Desktop. Send that file over.
#>
$Dir  = "C:\Program Files\KidNest"
if (-not (Test-Path $Dir)) { $Dir = "C:\Program Files\KidProxy" }   # older install
$Port = 8080
$out  = Join-Path ([Environment]::GetFolderPath("Desktop")) "kidnest-diagnose.txt"
$r = @()
function S($t) { $script:r += ""; $script:r += "===== $t ====="; }

S "when"; $r += (Get-Date).ToString("u"); $r += "uptime since: " + (Get-CimInstance Win32_OperatingSystem).LastBootUpTime

S "scheduled tasks"
foreach ($n in "KidNest", "KidNest Watchdog", "KidProxy", "KidProxy Watchdog") {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if (-not $t) { $r += "$n : NOT REGISTERED"; continue }
  $i = $t | Get-ScheduledTaskInfo
  $r += "$n : State=$($t.State) LastRun=$($i.LastRunTime) Result=0x$('{0:X}' -f $i.LastTaskResult) NextRun=$($i.NextRunTime) Missed=$($i.NumberOfMissedRuns)"
  $r += "   triggers: " + (($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ", ")
}

S "process + port"
$p = Get-Process mitmdump -ErrorAction SilentlyContinue
$r += if ($p) { "mitmdump RUNNING pid=$($p.Id) started=$($p.StartTime) mem=$([int]($p.WorkingSet64/1MB))MB" } else { "mitmdump NOT RUNNING" }
$c = Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
$r += "127.0.0.1:${Port} reachable: $c"

S "files"
foreach ($f in "mitmdump.exe","kidproxy.py","sheetlog.py","kidproxy.json","kidproxy.log","urllog-state.json","ca\mitmproxy-ca-cert.cer") {
  $fp = Join-Path $Dir $f
  $r += if (Test-Path $fp) { "{0,-34} {1,10} bytes  {2}" -f $f, (Get-Item $fp).Length, (Get-Item $fp).LastWriteTime } else { "$f : MISSING" }
}

S "kidproxy.json"
if (Test-Path "$Dir\kidproxy.json") { $r += (Get-Content "$Dir\kidproxy.json" -Raw) }

S "kidproxy.log (last 60 lines)"
if (Test-Path "$Dir\kidproxy.log") {
  $len = (Get-Item "$Dir\kidproxy.log").Length
  if ($len -eq 0) { $r += "*** LOG IS EMPTY - the addon never logged anything ***" }
  $r += Get-Content "$Dir\kidproxy.log" -Tail 60
} else { $r += "*** NO LOG FILE ***" }

S "can anything actually WRITE there?"
# The addon swallows write errors, so a blocked write looks exactly like "never started".
$probe = Join-Path $Dir "write-test.tmp"
try {
  "probe" | Set-Content -Path $probe -Encoding ASCII -ErrorAction Stop
  $r += "write to $Dir : OK"
  Remove-Item $probe -Force -ErrorAction SilentlyContinue
} catch {
  $r += "write to $Dir : BLOCKED -> $($_.Exception.Message)"
}
$cfa = (Get-MpPreference -ErrorAction SilentlyContinue).EnableControlledFolderAccess
$r += "controlled folder access : $cfa   (1 = on, blocks apps writing to Program Files)"
$r += "CFA allowed apps         : " + ((Get-MpPreference -EA SilentlyContinue).ControlledFolderAccessAllowedApplications -join ", ")
$r += "ASR rules                : " + ((Get-MpPreference -EA SilentlyContinue).AttackSurfaceReductionRules_Ids -join ", ")

S "why it is not running"
$r += "task registered : " + [bool](Get-ScheduledTask -TaskName KidNest -EA SilentlyContinue)
$r += "fast startup    : " + (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -Name HiberbootEnabled -EA SilentlyContinue).HiberbootEnabled + "  (1 = on, can skip AtStartup triggers)"
$r += "defender blocks : "
$r += (Get-MpThreatDetection -EA SilentlyContinue | Where-Object { $_.Resources -like "*KidNest*" -or $_.Resources -like "*mitmdump*" } |
       Select-Object -First 5 | ForEach-Object { "  $($_.InitialDetectionTime)  $($_.Resources)" })
try {
  $r += "manual start    : launching mitmdump.exe for 8s..."
  $o = "$env:TEMP\kn-o.txt"; $e2 = "$env:TEMP\kn-e.txt"
  $arg = "--listen-host 127.0.0.1 --listen-port $Port --set confdir=`"$Dir\ca`" -s `"$Dir\kidproxy.py`" -q"
  $ph = Start-Process -FilePath "$Dir\mitmdump.exe" -ArgumentList $arg -PassThru -RedirectStandardOutput $o -RedirectStandardError $e2 -WindowStyle Hidden -EA Stop
  Start-Sleep -Seconds 8
  if (-not $ph.HasExited) { $ph.Kill(); $r += "  -> runs fine by hand; the scheduled task is the problem" }
  else { $r += "  -> exited with $($ph.ExitCode):"; $r += (Get-Content $e2, $o -EA SilentlyContinue | Where-Object { $_ } | Select-Object -First 15 | ForEach-Object { "     $_" }) }
} catch { $r += "  -> could not launch: $($_.Exception.Message)" }

S "proxy policy"
$r += "system ProxyEnable = " + (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -EA SilentlyContinue).ProxyEnable
$r += "system ProxyServer = " + (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyServer -EA SilentlyContinue).ProxyServer
$r += "Edge ProxyServer   = " + (Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -Name ProxyServer -EA SilentlyContinue).ProxyServer

S "CA trusted"
$r += (Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "*mitmproxy*" } |
       ForEach-Object { "$($_.Subject)  expires $($_.NotAfter)" })

S "can this PC reach the sheet directly"
try {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 25 -Uri "https://docs.google.com/spreadsheets/d/1VtlZ1FJlUmRQ3VDx7Gzlve7LZ-oHo79P9iFbNj-9VCs/export?format=csv&gid=0"
  $r += "sheet fetch OK in $($sw.ElapsedMilliseconds)ms, $($resp.Content.Length) bytes"
} catch { $r += "sheet fetch FAILED: $($_.Exception.Message)" }

S "task scheduler events (last 20)"
try {
  $r += Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'} -MaxEvents 400 -EA SilentlyContinue |
        Where-Object { $_.Message -like "*KidNest*" -or $_.Message -like "*KidProxy*" } | Select-Object -First 20 |
        ForEach-Object { "{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}" -f $_.TimeCreated, $_.Id, ($_.Message -split "`n")[0] }
} catch { $r += "could not read the TaskScheduler log: $($_.Exception.Message)" }

$r | Set-Content -Path $out -Encoding UTF8
Write-Host "Written to $out"
Write-Host ""
$r | Select-Object -First 40
