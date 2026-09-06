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
