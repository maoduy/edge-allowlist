<#
KidNest watchdog - runs as SYSTEM every 5 minutes.

Its only job now is to make sure the SUPERVISOR is running. The supervisor owns
mitmdump: it starts it, waits until it really answers, sweeps strays, and takes it
down with itself. Nothing else may touch the proxy.

That division matters. This script used to restart mitmdump itself, racing the
installer and Task Scheduler doing the same - which is what left two processes
fighting over port 8080, one of them wedged at 6MB. A watchdog that only asks "is the
one owner alive?" cannot cause that.

It also used to fire "schtasks /Run /TN KidNest" blind, which Task Scheduler ignores
when it believes the task is already running - so it did nothing in exactly the
situation it existed for.
#>
param([int]$Port = 8080)

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

$task = Get-ScheduledTask -TaskName "KidNest" -ErrorAction SilentlyContinue
if (-not $task) { Note "the KidNest task is not registered"; exit 0 }

# Paused on purpose, or stood down after repeated failures? Leave it alone - a person
# decides when it comes back, with "kidnest resume".
if ($task.State -eq "Disabled") { exit 0 }

# Supervisor alive: whether the proxy is healthy is its business, and it checks every
# 30 seconds. Stepping in here is what caused duplicates.
if ($task.State -eq "Running") { exit 0 }

Note "supervisor is not running (task state: $($task.State)) - starting it"
Start-ScheduledTask -TaskName "KidNest" -ErrorAction SilentlyContinue

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 2
  $c = & curl.exe -s -o NUL -w "%{http_code}" --max-time 5 `
         -x "http://127.0.0.1:$Port" "http://kidnest.local/" 2>$null
  if ($c -match '^\d{3}$' -and $c -ne "000") { $ok = $true; break }
}
Note $(if ($ok) { "  proxy answering again after $((($i + 1) * 2))s" }
        else     { "  still not answering - see supervisor.log" })
