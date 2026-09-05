"""
KidProxy - mitmproxy addon.

* Allows only websites listed in the Google Sheet (tab "websites"), for ENFORCED users.
* On youtube.com, only videos from channels listed in the sheet (tab "youtube") play;
  search/home/related lists are filtered to those channels; Shorts are blocked.
* Users that are members of the local Administrators group (or listed in EXEMPT_USERS)
  are not filtered at all. Service accounts (SYSTEM, *$) are never filtered.
* Lists are re-downloaded every REFRESH_SECONDS. On failure the last good list stays.

Run:  mitmdump -s kidproxy.py --listen-host 127.0.0.1 --listen-port 8080
Config: kidproxy.json next to this file (optional), keys: sitesUrl, channelsUrl,
        refreshSeconds, exemptUsers (list), enforceUsers (list, overrides admin check).
"""
import csv, io, json, os, re, sys, threading, time, urllib.request, subprocess, platform
from mitmproxy import http, ctx

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "sitesUrl": "https://docs.google.com/spreadsheets/d/1VtlZ1FJlUmRQ3VDx7Gzlve7LZ-oHo79P9iFbNj-9VCs/export?format=csv&gid=0",
    "channelsUrl": "https://docs.google.com/spreadsheets/d/1VtlZ1FJlUmRQ3VDx7Gzlve7LZ-oHo79P9iFbNj-9VCs/export?format=csv&gid=1729222840",
    "refreshSeconds": 900,
    "exemptUsers": [],
    "enforceUsers": [],      # if non-empty: ONLY these users are filtered
    "blockShorts": True,
    "filterFeeds": True,
    "logFile": "",
}
CFG = dict(DEFAULTS)
try:
    with open(os.path.join(HERE, "kidproxy.json"), encoding="utf-8") as f:
        CFG.update(json.load(f))
except FileNotFoundError:
    pass
except Exception as e:
    print("kidproxy: bad kidproxy.json:", e, file=sys.stderr)

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtubei.googleapis.com"}
HEADERS = {"site", "sites", "domain", "domains", "url", "urls", "channel", "channels", "kênh", "trang"}
SYSTEM_USERS = {"system", "local service", "network service"}

def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print("kidproxy:", msg)
    if CFG.get("logFile"):
        try:
            with open(CFG["logFile"], "a", encoding="utf-8") as f: f.write(line + "\n")
        except Exception: pass

# ---------------------------------------------------------------- lists
class Lists:
    domains: set = set()
    paths: list = []
    handles: set = set()
    ids: set = set()
    loaded = False
    last_ok = 0
    last_err = ""

L = Lists()
lock = threading.Lock()

def _fetch(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via ourselves
    req = urllib.request.Request(url, headers={"User-Agent": "kidproxy/1.0", "Cache-Control": "no-cache"})
    with opener.open(req, timeout=30) as r:
        return r.read().decode("utf-8-sig", "replace")

def _first_col(text):
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row: continue
        v = row[0].strip()
        if not v or v.startswith("#") or v.lower() in HEADERS: continue
        out.append(v)
    return out

def _norm_site(s):
    s = s.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^\*\.?", "", s)
    s = re.sub(r"^www\.", "", s)
    if "/" in s.rstrip("/"):
        return ("path", s.rstrip("/"))
    return ("domain", s.strip("/"))

def _norm_channel(s):
    s = s.strip()
    s = re.sub(r"^https?://(www\.|m\.)?youtube\.com/", "", s, flags=re.I)
    s = re.sub(r"^channel/", "", s, flags=re.I)
    s = re.sub(r"[/?#].*$", "", s)
    if re.match(r"^UC[\w-]{20,}$", s): return ("id", s)
    return ("handle", s.lstrip("@").lower())

def refresh():
    try:
        sites = _first_col(_fetch(CFG["sitesUrl"]))
        chans = _first_col(_fetch(CFG["channelsUrl"]))
    except Exception as e:
        L.last_err = str(e)
        log(f"list refresh failed: {e} (keeping previous list)")
        return
    d, p, h, i = set(), [], set(), set()
    for s in sites:
        k, v = _norm_site(s)
        (d.add(v) if k == "domain" else p.append(v))
    for c in chans:
        k, v = _norm_channel(c)
        (i.add(v) if k == "id" else h.add(v))
    # hosts needed to keep the sheet itself and the YouTube player working
    for extra in ("docs.google.com", "googleusercontent.com", "googlevideo.com", "ytimg.com", "ggpht.com", "gstatic.com", "googleapis.com"):
        d.add(extra)
    if h or i:
        d.update({"youtube.com", "youtube-nocookie.com"})
    with lock:
        L.domains, L.paths, L.handles, L.ids = d, p, h, i
        L.loaded, L.last_ok, L.last_err = True, time.time(), ""
    log(f"lists loaded: {len(d)} domains, {len(p)} paths, {len(h)} handles, {len(i)} channel ids")

def _refresher():
    while True:
        refresh()
        time.sleep(max(60, int(CFG["refreshSeconds"])))

def host_allowed(host):
    host = (host or "").lower().rstrip(".")
    if host in ("localhost", "127.0.0.1", "::1"): return True
    with lock:
        if not L.loaded: return False  # fail closed until the first successful load
        for d in L.domains:
            if host == d or host.endswith("." + d): return True
    return False

def url_allowed(host, path):
    if host_allowed(host): return True
    hp = (host or "").lower().lstrip("www.") + path
    with lock:
        return any(hp.startswith(p) for p in L.paths)

def channel_allowed(handle=None, cid=None):
    with lock:
        if handle and handle.lstrip("@").lower() in L.handles: return True
        if cid and cid in L.ids: return True
    return False

# ---------------------------------------------------------------- who is connecting?
_admins = {"__t": 0, "names": set()}
_pid_user = {}

def _local_admins():
    if time.time() - _admins["__t"] < 600: return _admins["names"]
    names = set()
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["net", "localgroup", "Administrators"], capture_output=True, text=True, timeout=10).stdout
            body = out.split("---", 2)[-1] if "---" in out else out
            for line in body.splitlines():
                l = line.strip()
                if l and not l.lower().startswith("the command") and not l.startswith("Lệnh"):
                    names.add(l.lower().split("\\")[-1])
        else:
            import grp
            for g in ("sudo", "admin", "wheel"):
                try: names.update(u.lower() for u in grp.getgrnam(g).gr_mem)
                except KeyError: pass
    except Exception as e:
        log(f"admin lookup failed: {e}")
    _admins["__t"], _admins["names"] = time.time(), names
    return names

def _pid_for_port_windows(port):
    import ctypes
    from ctypes import wintypes
    class ROW(ctypes.Structure):
        _fields_ = [("state", wintypes.DWORD), ("laddr", wintypes.DWORD), ("lport", wintypes.DWORD),
                    ("raddr", wintypes.DWORD), ("rport", wintypes.DWORD), ("pid", wintypes.DWORD)]
    iphlp = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)  # AF_INET=2, TCP_TABLE_OWNER_PID_ALL=5
    buf = ctypes.create_string_buffer(size.value)
    if iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0) != 0: return None
    n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(ROW * n))[0]
    for r in rows:
        lport = ((r.lport & 0xFF) << 8) | ((r.lport >> 8) & 0xFF)
        if lport == port: return int(r.pid)
    return None

def _user_for_pid_windows(pid):
    import ctypes
    from ctypes import wintypes
    adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h: return None
    try:
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(h, 8, ctypes.byref(tok)): return None  # TOKEN_QUERY
        try:
            size = wintypes.DWORD(0)
            adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(size))  # TokenUser
            buf = ctypes.create_string_buffer(size.value)
            if not adv.GetTokenInformation(tok, 1, buf, size, ctypes.byref(size)): return None
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            name = ctypes.create_unicode_buffer(256); dom = ctypes.create_unicode_buffer(256)
            nl, dl, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            if not adv.LookupAccountSidW(None, sid, name, ctypes.byref(nl), dom, ctypes.byref(dl), ctypes.byref(use)): return None
            return name.value
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)

def _user_for_port_linux(port):
    # /proc/net/tcp: local_address ... uid
    import pwd
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(fn) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    lp = int(parts[1].split(":")[-1], 16)
                    if lp == port:
                        return pwd.getpwuid(int(parts[7])).pw_name
        except Exception:
            continue
    return None

def client_user(flow_or_ctx):
    """Return the OS user name owning the client socket, or None."""
    try:
        peer = flow_or_ctx.client_conn.peername if hasattr(flow_or_ctx, "client_conn") else flow_or_ctx.client.peername
        port = peer[1]
        if platform.system() == "Windows":
            pid = _pid_for_port_windows(port)
            if pid is None: return None
            now = time.time()
            u = _pid_user.get(pid)
            if u and now - u[1] < 60: return u[0]
            name = _user_for_pid_windows(pid)
            _pid_user[pid] = (name, now)
            return name
        return _user_for_port_linux(port)
    except Exception as e:
        log(f"user lookup failed: {e}")
        return None

def enforced(flow_or_ctx):
    user = client_user(flow_or_ctx)
    if user is None:
        return True                      # unknown -> fail closed
    u = user.lower()
    if u in SYSTEM_USERS or u.endswith("$"): return False
    if CFG["enforceUsers"]:
        return u in {x.lower() for x in CFG["enforceUsers"]}
    if u in {x.lower() for x in CFG["exemptUsers"]}: return False
    if u in _local_admins(): return False
    return True

# ---------------------------------------------------------------- YouTube filtering
BLOCK_HTML = """<!doctype html><html lang="vi"><meta charset="utf-8"><title>Bị chặn</title>
<body style="margin:0;font-family:system-ui,sans-serif;background:#f4f5f7;display:flex;align-items:center;justify-content:center;height:100vh">
<div style="background:#fff;padding:40px 48px;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:560px;text-align:center">
<h1 style="font-size:22px;margin:0 0 12px">{title}</h1><p style="color:#555">{sub}</p></div></body></html>"""

def blocked_page(title, sub):
    return http.Response.make(403, BLOCK_HTML.format(title=title, sub=sub).encode("utf-8"),
                              {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})

RENDERERS = ("videoRenderer", "compactVideoRenderer", "gridVideoRenderer", "richItemRenderer",
             "reelItemRenderer", "playlistVideoRenderer", "lockupViewModel", "videoWithContextRenderer")
DROP_ALWAYS = ("reelShelfRenderer", "shortsLockupViewModel", "richShelfRenderer")

def _find_channel(obj, depth=0):
    """First (handle, channelId) found in a renderer's browse endpoints."""
    if depth > 12: return (None, None)
    if isinstance(obj, dict):
        be = obj.get("browseEndpoint")
        if isinstance(be, dict):
            base = be.get("canonicalBaseUrl") or ""
            bid = be.get("browseId") or ""
            m = re.match(r"^/@([^/?#]+)", base)
            if m or bid.startswith("UC"):
                return (m.group(1) if m else None, bid if bid.startswith("UC") else None)
        for v in obj.values():
            r = _find_channel(v, depth + 1)
            if r != (None, None): return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_channel(v, depth + 1)
            if r != (None, None): return r
    return (None, None)

def _item_allowed(item):
    h, cid = _find_channel(item)
    if h is None and cid is None: return False
    return channel_allowed(h, cid)

def filter_json(obj, stats, depth=0):
    if depth > 60: return obj
    if isinstance(obj, list):
        out = []
        for el in obj:
            if isinstance(el, dict) and len(el) == 1:
                key = next(iter(el))
                if key in DROP_ALWAYS and CFG["blockShorts"]:
                    stats["dropped"] += 1; continue
                if key in RENDERERS:
                    if _item_allowed(el): out.append(filter_json(el, stats, depth + 1))
                    else: stats["dropped"] += 1
                    continue
            out.append(filter_json(el, stats, depth + 1))
        return out
    if isinstance(obj, dict):
        return {k: filter_json(v, stats, depth + 1) for k, v in obj.items()}
    return obj

def _player_channel(pr):
    vd = pr.get("videoDetails") or {}
    mf = ((pr.get("microformat") or {}).get("playerMicroformatRenderer")) or {}
    handle = None
    m = re.search(r"youtube\.com/@([^/?#\"]+)", mf.get("ownerProfileUrl") or "")
    if m: handle = m.group(1)
    return handle, vd.get("channelId")

def _html_channel(body):
    m1 = re.search(r'"ownerProfileUrl":"https?://(?:www\.)?youtube\.com/@([^"/?#]+)"', body)
    m2 = re.search(r'"videoDetails":\{[^{}]*?"channelId":"(UC[\w-]+)"', body)
    return (m1.group(1) if m1 else None, m2.group(1) if m2 else None)

def _rewrite_initial_data(body, stats):
    """Filter the embedded ytInitialData JSON in a YouTube HTML page."""
    m = re.search(r"(var ytInitialData = )(\{.*?\})(;</script>)", body, flags=re.S)
    if not m: return body
    try:
        data = json.loads(m.group(2))
    except Exception:
        return body
    data = filter_json(data, stats)
    return body[:m.start(2)] + json.dumps(data, separators=(",", ":")) + body[m.end(2):]

# ---------------------------------------------------------------- mitmproxy hooks
class KidProxy:
    def load(self, loader):
        threading.Thread(target=_refresher, daemon=True).start()
        log(f"started; enforceUsers={CFG['enforceUsers'] or 'all non-admins'} exempt={CFG['exemptUsers']}")

    def http_connect(self, flow: http.HTTPFlow):
        host = flow.request.host
        if not enforced(flow): return
        if not host_allowed(host):
            flow.response = http.Response.make(403, b"blocked by kidproxy: " + host.encode())
            log(f"BLOCK site {host} ({client_user(flow)})")

    def tls_clienthello(self, data):
        host = (data.context.server.address or ("",))[0].lower()
        if host not in YT_HOSTS or not enforced(data.context):
            data.ignore_connection = True   # pass through untouched

    def request(self, flow: http.HTTPFlow):
        if not enforced(flow): return
        host, path = flow.request.host.lower(), flow.request.path
        if host not in YT_HOSTS:
            # plain-http request (no CONNECT)
            if not url_allowed(host, path):
                flow.response = blocked_page("Trang này không nằm trong danh sách được phép", host)
            return
        if CFG["blockShorts"] and path.startswith("/shorts"):
            flow.response = blocked_page("YouTube Shorts đã bị tắt", "")

    def response(self, flow: http.HTTPFlow):
        host, path = flow.request.host.lower(), flow.request.path
        if host not in YT_HOSTS or flow.response is None or not enforced(flow): return
        ct = flow.response.headers.get("content-type", "")
        p = path.split("?")[0]
        try:
            if p.startswith("/youtubei/v1/player") and "json" in ct:
                pr = json.loads(flow.response.get_text(strict=False) or "{}")
                h, cid = _player_channel(pr)
                if not channel_allowed(h, cid):
                    pr["playabilityStatus"] = {"status": "UNPLAYABLE", "reason": "Kênh này chưa được duyệt"}
                    pr.pop("streamingData", None)
                    flow.response.set_text(json.dumps(pr))
                    log(f"BLOCK video channel=@{h}/{cid}")
            elif p == "/watch" and "html" in ct:
                body = flow.response.get_text(strict=False) or ""
                h, cid = _html_channel(body)
                if not channel_allowed(h, cid):
                    flow.response = blocked_page("Kênh YouTube này chưa được duyệt", f"@{h}" if h else (cid or ""))
                    log(f"BLOCK watch channel=@{h}/{cid}")
                elif CFG["filterFeeds"]:
                    st = {"dropped": 0}
                    flow.response.set_text(_rewrite_initial_data(body, st))
            elif CFG["filterFeeds"] and "json" in ct and p.startswith(("/youtubei/v1/search", "/youtubei/v1/browse", "/youtubei/v1/next", "/youtubei/v1/guide")):
                data = json.loads(flow.response.get_text(strict=False) or "{}")
                st = {"dropped": 0}
                flow.response.set_text(json.dumps(filter_json(data, st), separators=(",", ":")))
            elif CFG["filterFeeds"] and "html" in ct and (p == "/" or p.startswith(("/results", "/feed", "/@", "/channel/", "/c/", "/user/"))):
                body = flow.response.get_text(strict=False) or ""
                st = {"dropped": 0}
                flow.response.set_text(_rewrite_initial_data(body, st))
        except Exception as e:
            log(f"filter error on {p}: {e}")

addons = [KidProxy()]
