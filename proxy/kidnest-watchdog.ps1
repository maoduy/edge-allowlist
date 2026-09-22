<#
KidNest watchdog - runs as SYSTEM every 5 minutes.

The old watchdog just called "schtasks /Run /TN KidNest". That is a no-op precisely
when it matters: a mitmdump that is alive but STUCK still counts as Running, so
Task Scheduler ignores the request and nothing recovers. A household sat without
internet for 30+ minutes with the watchdog firing happily every 5 of them.

This one proves the proxy actually serves a request before deciding it is healthy.
A TCP connect is not enough - the kernel accepts into the backlog even when the
process behind it is wedged, which is exactly the state that caused the outage.
#>
param([int]$Port = 8080)

$Dir  = "C:\Program Files\KidNest"
$Data = "C:\ProgramData\KidNest"
$log  = "$Data\watchdog.log"

function Note($m) {
  try {
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) {
      Move-Item $log "$log.1" -Force -ErrorAction SilentlyContinue
    }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content $log -ErrorAction SilentlyContinue
  } catch { }
}

# Only one watchdog may act at a time. The scheduled run fires every 5 minutes and can
# land inside the window where something else - a manual run, an installer, a person -
# is already restarting the proxy. Both then "recover" it and you get two mitmdump
# processes, one of which wedges the port. Reproduced in CI; this is the cure.
$mutex = New-Object System.Threading.Mutex($false, "Global\KidNestWatchdog")
$held = $false
try { $held = $mutex.WaitOne(5000) } catch [System.Threading.AbandonedMutexException] { $held = $true }
if (-not $held) { exit 0 }

try {

# Paused on purpose? Then stay out of the way - kidnest-pause.ps1 disables the task.
$task = Get-ScheduledTask -TaskName "KidNest" -ErrorAction SilentlyContinue
if (-not $task) { return }
if ($task.State -eq "Disabled") { return }

# End-to-end probe: this only answers if the addon's own request hook ran.
$healthy = $false
try {
  $out = & curl.exe -s -o NUL -w "%{http_code}" --max-time 10 `
           -x "http://127.0.0.1:$Port" "http://kidnest.local/" 2>$null
  if ($out -match '^\d{3}$' -and $out -ne "000") { $healthy = $true }
} catch { $healthy = $false }

if ($healthy) {
  # Healthy, but more than one instance means something restarted it twice. The oldest
  # bound the port and is the one serving; newer ones are wedged or idle. Leave the
  # server alone, clear the rest.
  $all = @(Get-Process mitmdump -ErrorAction SilentlyContinue | Sort-Object StartTime)
  if ($all.Count -gt 1) {
    Note "healthy, but $($all.Count) mitmdump processes - removing $($all.Count - 1) duplicate(s)"
    $all[1..($all.Count - 1)] | Stop-Process -Force -ErrorAction SilentlyContinue
  }
  return
}

Note "proxy not answering on $Port - recovering"
$procs = @(Get-Process mitmdump -ErrorAction SilentlyContinue)
Note "  mitmdump processes before: $($procs.Count)"

# Stop the task, then kill every instance ourselves. Stop-ScheduledTask does not
# reliably terminate the process it launched, and a stray second instance holding
# the port is the thing that wedges a fresh start.
Stop-ScheduledTask -TaskName "KidNest" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Get-Process mitmdump -ErrorAction SilentlyContinue |
  Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Start-ScheduledTask -TaskName "KidNest" -ErrorAction SilentlyContinue

# Say whether it actually came back, so the log is evidence and not a guess.
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Seconds 2
  $out = & curl.exe -s -o NUL -w "%{http_code}" --max-time 5 `
           -x "http://127.0.0.1:$Port" "http://kidnest.local/" 2>$null
  if ($out -match '^\d{3}$' -and $out -ne "000") { $ok = $true; break }
}
Note $(if ($ok) { "  recovered after $((($i + 1) * 2)) seconds" }
        else     { "  STILL DOWN after restart - see kidproxy.log" })

# One more sweep: if anything else restarted it at the same moment, keep the instance
# that bound the port and drop the rest.
$all = @(Get-Process mitmdump -ErrorAction SilentlyContinue | Sort-Object StartTime)
if ($all.Count -gt 1) {
  Note "  $($all.Count) instances after restart - removing $($all.Count - 1)"
  $all[1..($all.Count - 1)] | Stop-Process -Force -ErrorAction SilentlyContinue
}

} finally {
  try { $mutex.ReleaseMutex() } catch { }
  $mutex.Dispose()
}
