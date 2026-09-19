<#
KidProxy diagnostics - run as Administrator:
    powershell -ExecutionPolicy Bypass -File .\diagnose.ps1
Writes kidproxy-diagnose.txt to your Desktop. Send that file over.
#>
$Dir  = "C:\Program Files\KidProxy"
$Port = 8080
$out  = Join-Path ([Environment]::GetFolderPath("Desktop")) "kidproxy-diagnose.txt"
$r = @()
function S($t) { $script:r += ""; $script:r += "===== $t ====="; }

S "when"; $r += (Get-Date).ToString("u"); $r += "uptime since: " + (Get-CimInstance Win32_OperatingSystem).LastBootUpTime

S "scheduled tasks"
foreach ($n in "KidProxy", "KidProxy Watchdog") {
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

S "task scheduler events for KidProxy (last 20)"
try {
  $r += Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'} -MaxEvents 400 -EA SilentlyContinue |
        Where-Object { $_.Message -like "*KidProxy*" } | Select-Object -First 20 |
        ForEach-Object { "{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}" -f $_.TimeCreated, $_.Id, ($_.Message -split "`n")[0] }
} catch { $r += "could not read the TaskScheduler log: $($_.Exception.Message)" }

$r | Set-Content -Path $out -Encoding UTF8
Write-Host "Written to $out"
Write-Host ""
$r | Select-Object -First 40
