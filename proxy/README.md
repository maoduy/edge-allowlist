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

## URL log to Google Sheets

Every page the filtered accounts visit is written to a Google Sheet:
one tab per month named `MM-yyyy`, **row = URL, column = day of month, cell = number of hits**.
Blocked attempts go to a parallel tab `MM-yyyy chan`.

Enable it at install time:

```powershell
.\install.ps1 -LogSheetId 1VtlZ1FJ...9VCs -LogCredentials .\kidproxy-sheets.json
```

`kidproxy.json` keys (under `urlLog`):

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | master switch |
| `sheetId` | `""` | target spreadsheet |
| `credentials` | `kidproxy-sheets.json` | service-account key, relative to the install dir |
| `flushSeconds` | `300` | how often the buffer is pushed (1–3 API calls per push) |
| `sheetAllRequests` | `false` | `false` = only page navigations reach the sheet |
| `localFile` | `""` | path to a JSONL of **every** request, including sub-resources |

Notes:

* Counts are held locally in `urllog-state.json` and pushed as totals, so a failed push
  or a reboot never loses or double-counts a hit.
* The row key is `host + path` plus only the params that identify a page (`v`, `list`,
  `q`, `search_query`), so `youtube.com/watch?v=...` stays distinguishable without the
  URL cardinality exploding.
* `http://kidproxy.local/update` pushes the log immediately as well as re-reading the lists.
* Use a **dedicated** service account. The key sits on the kid's PC; anything else that
  account can reach is reachable from there too.
* Sheets allows 10M cells per workbook. At ~2k URLs/month that is years of headroom,
  but archive to a new workbook yearly if the kid is a heavy browser.
* Google's JWT signing uses `cryptography`, which ships inside `mitmdump.exe`.
  `google-auth`/`googleapiclient` are deliberately not used: the standalone binary
  cannot import anything that is not already bundled.
