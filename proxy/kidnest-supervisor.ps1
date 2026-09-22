<#
KidNest supervisor - the ONLY thing allowed to start or stop mitmdump.

Before this, three different things could launch the proxy: Task Scheduler (through
an AtStartup and an AtLogOn trigger on the same task), the installer's explicit
Start-ScheduledTask, and the watchdog's recovery. Nothing owned the process, so
nothing could say whether what was running was healthy.

The 6MB process sitting beside the 155MB one was never the symptom of that. mitmdump
is a PyInstaller onefile binary and always runs as a bootstrap plus the server it
unpacks and re-executes. Every attempt to reap "the duplicate" was killing a working
proxy. Counting processes is meaningless here; the only honest health check is
whether the thing answers.

Task Scheduler now runs THIS, and this runs mitmdump as its own child:

  * already serving        -> leave it alone, just sweep away any stray instance
  * not serving            -> clear whatever is there and start one
  * started                -> wait until it really answers before calling it up
  * will not come up       -> back off, try again, and after enough failures pause
                              KidNest so the machine is usable rather than leaving
                              the household offline

Only one supervisor can exist: Task Scheduler's IgnoreNew plus a global mutex. And
because the proxy is its child, stopping the task takes the proxy down with it
instead of orphaning it.
#>
param(
  [int]$Port = 8080,
  [int]$PollSeconds = 30,        # how often to re-check a healthy proxy
  [int]$MaxStartSeconds = 90,    # how long a fresh start may take to answer
  [int]$FailOpenAfter = 5        # consecutive failed starts before standing down
)

$Dir  = "C:\Program Files\KidNest"
$Data = "C:\ProgramData\KidNest"
$log  = "$Data\supervisor.log"

function Note($m) {
  try {
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) {
      Move-Item $log "$log.1" -Force -ErrorAction SilentlyContinue
    }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content $log -ErrorAction SilentlyContinue
  } catch { }
}

function PortOwner {
  (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
   Select-Object -First 1).OwningProcess
}

function Serving {
  # Only answers if the addon's own request hook ran. A TCP connect proves nothing -
  # the kernel accepts into the backlog even when the process behind it is wedged.
  $c = & curl.exe -s -o NUL -w "%{http_code}" --max-time 8 `
         -x "http://127.0.0.1:$Port" "http://kidnest.local/" 2>$null
  return ($c -match '^\d{3}$' -and $c -ne "000")
}

# mitmdump.exe is a PyInstaller onefile build: it unpacks itself and re-executes, so a
# single healthy proxy is ALWAYS two processes - a small bootstrap (~6MB) and the real
# server (~155MB) that holds the port. Start-Process hands back the bootstrap. Treating
# "two processes" as a duplicate is what made the supervisor kill the server it had just
# started, every five seconds. One proxy = one process TREE.
function ProcMap {
  $m = @{}
  foreach ($p in Get-CimInstance Win32_Process -Filter "Name='mitmdump.exe'" -ErrorAction SilentlyContinue) {
    $m[[int]$p.ProcessId] = [int]$p.ParentProcessId
  }
  return $m
}

function TreeOf($root, $map) {
  # the root, everything descended from it, and the ancestors that are also mitmdump
  $keep = @{}
  if (-not $root) { return $keep }
  $keep[[int]$root] = $true
  $up = [int]$root
  while ($map.ContainsKey($up) -and $map.ContainsKey($map[$up])) { $up = $map[$up]; $keep[$up] = $true }
  for ($i = 0; $i -lt 8; $i++) {           # walk down, a few generations is plenty
    foreach ($pid2 in @($map.Keys)) {
      if (-not $keep.ContainsKey($pid2) -and $keep.ContainsKey($map[$pid2])) { $keep[$pid2] = $true }
    }
  }
  return $keep
}

function SweepStrays($keep) {
  $map  = ProcMap
  $mine = TreeOf $keep $map
  $extra = @($map.Keys | Where-Object { -not $mine.ContainsKey($_) })
  if ($extra.Count) {
    Note "sweeping $($extra.Count) stray mitmdump process(es) outside the tree of pid $keep"
    $extra | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
  }
}

function KillTree($root) {
  $map = ProcMap
  foreach ($p in @((TreeOf $root $map).Keys)) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
}

$mutex = New-Object System.Threading.Mutex($false, "Global\KidNestSupervisor")
$held = $false
try { $held = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $held = $true }
if (-not $held) { Note "another supervisor already has the lock - exiting"; exit 0 }

$exe = "$Dir\mitmdump.exe"
# ONE pre-quoted string, not an array. Start-Process joins an array with spaces and
# quotes nothing, so "C:\Program Files\KidNest\kidproxy.py" arrived as two separate
# arguments and mitmdump could never start.
$arg = "--listen-host 127.0.0.1 --listen-port $Port " +
       "--set confdir=`"$Data\ca`" -s `"$Dir\kidproxy.py`" -q"
$child = $null
$fails = 0
Note "supervisor up (port $Port, poll ${PollSeconds}s)"

try {
  while ($true) {

    # -------------------------------------------------- healthy: do nothing but tidy
    if (Serving) {
      if ($fails) { Note "serving again after $fails failed start(s)" }
      $fails = 0
      SweepStrays (PortOwner)
      Start-Sleep -Seconds $PollSeconds
      continue
    }

    # -------------------------------------------------- not serving: clear and start
    $alive = @(Get-Process mitmdump -ErrorAction SilentlyContinue)
    if ($alive.Count) {
      Note "not answering but $($alive.Count) process(es) alive - clearing them"
      $alive | Stop-Process -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 2
    }

    if (-not (Test-Path $exe)) {
      $fails++
      Note "mitmdump.exe is missing at $exe (attempt $fails) - antivirus may have removed it"
      Start-Sleep -Seconds 60
      if ($fails -ge $FailOpenAfter) { break }
      continue
    }

    Note "starting mitmdump"
    try {
      $so = "$Data\mitmdump-out.log"; $se = "$Data\mitmdump-err.log"
      $child = Start-Process -FilePath $exe -ArgumentList $arg -PassThru -WindowStyle Hidden `
                 -RedirectStandardOutput $so -RedirectStandardError $se -ErrorAction Stop
    } catch {
      $fails++
      Note "  could not launch: $($_.Exception.Message) (attempt $fails)"
      Start-Sleep -Seconds ([math]::Min(300, 15 * $fails))
      if ($fails -ge $FailOpenAfter) { break }
      continue
    }

    # -------------------------------------------------- wait for it to really answer
    $t0 = Get-Date
    $ready = $false
    while (((Get-Date) - $t0).TotalSeconds -lt $MaxStartSeconds) {
      Start-Sleep -Seconds 3
      if ($child.HasExited) { Note "  exited straight away, code $($child.ExitCode)"; break }
      if (Serving) { $ready = $true; break }
    }

    if ($ready) {
      $fails = 0
      $owner = PortOwner
      Note "  ready in $([int]((Get-Date) - $t0).TotalSeconds)s, bootstrap pid $($child.Id), serving pid $owner"
      SweepStrays $owner
    } else {
      $fails++
      if (-not $child.HasExited) {
        Note "  still not answering after ${MaxStartSeconds}s - stopping it (attempt $fails)"
        KillTree $child.Id
      } else {
        Note "  start failed (attempt $fails), exit code $($child.ExitCode)"
        foreach ($f in $se, $so) {
          if ((Test-Path $f) -and (Get-Item $f).Length) {
            Get-Content $f -Tail 5 | ForEach-Object { Note "    $_" }
          }
        }
      }
      if ($fails -ge $FailOpenAfter) { break }
      $back = [math]::Min(300, 15 * $fails)
      Note "  retrying in ${back}s"
      Start-Sleep -Seconds $back
    }
  }

  # ------------------------------------------------------ worst case: stand down
  # Repeated failures mean nobody in the house can browse. Leaving the machine
  # pointed at a proxy that will not start is the one outcome worse than not
  # filtering at all, so hand the machine back and say so loudly.
  Note "GIVING UP after $fails failed starts - pausing KidNest so this machine still works"
  Note "filtering is OFF until someone runs: kidnest resume"
  if (Test-Path "$Dir\kidnest-pause.ps1") {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Dir\kidnest-pause.ps1" -Port $Port
  }

} finally {
  # The proxy is our child - do not leave it running without a supervisor.
  if ($child -and -not $child.HasExited) {
    Note "supervisor stopping - taking mitmdump (tree of pid $($child.Id)) with it"
    KillTree $child.Id
  }
  try { $mutex.ReleaseMutex() } catch { }
  $mutex.Dispose()
}
