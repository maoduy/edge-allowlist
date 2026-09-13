# KidProxy: local filtering proxy driven by the Google Sheet

A mitmproxy addon that runs as a SYSTEM service on the kid's Windows PC.

- Websites: only domains in the sheet's `websites` tab can be **opened as a page**. Smart mode
  (`smartDependencies`, on by default) then allows whatever a listed page loads: scripts, images,
  video streams, embedded players (frames are allowed only when embedded by a listed page). Ad
  networks are refused always. So the sheet holds only the ~25 core sites, no CDN/helper hosts.
  Non-browser apps get the strict rule: host must be listed.
- YouTube: youtube.com traffic is decrypted by the proxy. A video plays only if its channel is in the
  `youtube` tab; other videos get "Kênh này chưa được duyệt". Home, search, related lists and channel
  pages are filtered to listed channels. Shorts are blocked.
- Lists are re-downloaded from the sheet every 15 minutes. If the download fails, the last list stays.
  Before the first successful download everything is blocked (fail closed).
- Local **Administrators are never filtered**, nor are Windows service accounts. Everyone else is.
  `-ExemptUsers a,b` adds never-filtered accounts; `-EnforceUsers kid` filters only that account.

## Install (Windows 10 Home is fine)

Copy `install.ps1` + `kidproxy.py` to the PC, open PowerShell **as Administrator** in that folder:

```
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

It downloads mitmproxy (~30 MB), installs to `C:\Program Files\KidProxy`, registers the SYSTEM task,
trusts the proxy certificate, and sets the proxy + lock policies for Edge, Chrome and Windows.
Then restart the browser. Check `C:\Program Files\KidProxy\kidproxy.log` for `lists loaded`.

`uninstall.ps1` reverses all of it.

## Why the kid cannot bypass it

- Proxy is set by HKLM policy (Edge, Chrome, Windows). Standard users cannot write HKLM.
- Proxy settings page is locked; extensions, DevTools, InPrivate/Incognito, guest mode are off.
- The proxy runs as SYSTEM from an admin-only folder; a watchdog restarts it every 5 minutes.
- Proxy refuses anything not on the list, so a VPN or "no proxy" trick inside the browser is not
  possible. Other browsers (Firefox) use the Windows proxy by default; remove them anyway.

## Config

`C:\Program Files\KidProxy\kidproxy.json` (written by the installer): `exemptUsers`, `enforceUsers`,
`logFile`; optional `sitesUrl`, `channelsUrl`, `refreshSeconds`, `blockShorts`, `filterFeeds`.

## Test locally (Linux, Edge installed)

```
python3 -m venv .venv-proxy && .venv-proxy/bin/pip install mitmproxy
.venv-proxy/bin/mitmdump -s proxy/kidproxy.py --listen-port 8081 --set confdir=/tmp/ca   # with kidproxy.json enforceUsers=[you]
node test/proxy.js
```

## URL log

Every page the filtered accounts open is counted locally. Read it in the browser on that PC:

* `http://kidproxy.local/log` - row = URL, column = day of month, cell = hits, with a month
  picker and a separate table for blocked attempts. No account, no key, nothing to set up.
* `http://kidproxy.local/log.csv` - the same month as CSV, for Excel or Sheets.

The installer turns this on by default. Config lives under `urlLog` in `kidproxy.json`:

| key | default | meaning |
|---|---|---|
| `enabled` | `true` (set by the installer) | count URLs and serve `/log` |
| `localFile` | `""` | path to a JSONL of **every** request, sub-resources included |
| `localMaxMB` | `20` | rotate that JSONL at this size (one generation kept) |
| `sheetAllRequests` | `false` | `false` = only page navigations are counted |
| `sheetId` | `""` | set it, and the counts are mirrored to a Google Sheet too |
| `credentials` | `kidproxy-sheets.json` | service-account key for that mirror |
| `flushSeconds` | `300` | how often the mirror is pushed (1-3 API calls) |

The row key is `host + path` plus only the params that identify a page (`v`, `list`, `q`,
`search_query`), so `youtube.com/watch?v=...` stays distinguishable without the URL
cardinality exploding. Counts persist in `urllog-state.json` as totals, so a failed push or
a reboot never loses or double-counts a hit.

### Optional: mirror into Google Sheets

Only needed to read the log **away from that PC**. One tab per month `MM-yyyy`, same shape,
blocked attempts in `MM-yyyy chan`.

```powershell
.\install.ps1 -LogSheetId <spreadsheet id>          # expects .\kidproxy-sheets.json
```

Create a service account in its **own** GCP project with no IAM roles, enable the Sheets API,
and share the spreadsheet with its address as Editor - sharing is what grants access, not a
project role. The key file ends up on the kid's PC, so anything else that account can reach
is reachable from there.

JWT signing uses `cryptography`, which ships inside `mitmdump.exe`. `google-auth` and
`googleapiclient` are deliberately unused: the standalone binary cannot import anything that
is not already bundled.

### Note for anyone editing kidproxy.py

mitmproxy loads the script as **several independent modules**. Module-level globals are
per-load, not per-process, and the addon instance that serves a request may not be the one
whose thread did the work. Anything singular (the allowlist, the URL counts, the Sheets
transport, the background threads) must live on `_shared`, the object parked in
`sys.modules["__kidproxy_shared__"]`. Guarding a thread start with a module-level flag
silently leaves other copies with an empty allowlist, which fails closed and blocks
everything.
