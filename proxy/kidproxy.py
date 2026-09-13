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
import csv, io, json, os, re, sys, threading, time, urllib.request, urllib.parse, subprocess, platform
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
    # smart mode: only page navigations must be to a listed site; resources a listed page loads
    # (scripts, images, video, embedded players) are allowed automatically via Referer/Origin.
    "smartDependencies": True,
    "dependencyTtlSeconds": 3600,
    "logFile": "",
    # URL log -> Google Sheet: one tab per month "MM-yyyy", row = URL, column = day, cell = hits.
    # Blocked attempts go to "MM-yyyy chan". Only filtered (kid) accounts are logged.
    "urlLog": {
        "enabled": False,
        "sheetId": "",
        "credentials": "kidproxy-sheets.json",   # its OWN service account, not a shared one
        "flushSeconds": 300,
        "sheetAllRequests": False,   # False = only page navigations reach the sheet
        "localFile": "",             # set a path to also keep a JSONL of EVERY request
    },
}
CFG = dict(DEFAULTS)
try:
    with open(os.path.join(HERE, "kidproxy.json"), encoding="utf-8") as f:
        _user_cfg = json.load(f)
    _ul = dict(DEFAULTS["urlLog"]); _ul.update(_user_cfg.pop("urlLog", {}) or {})
    CFG.update(_user_cfg); CFG["urlLog"] = _ul
except FileNotFoundError:
    pass
except Exception as e:
    print("kidproxy: bad kidproxy.json:", e, file=sys.stderr)

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtubei.googleapis.com"}
HEADERS = {"site", "sites", "website", "websites", "domain", "domains", "url", "urls", "channel", "channels", "kênh", "trang", "trang web"}
# never allowed, even as a dependency of a listed page
AD_HOSTS = ("doubleclick.net", "googlesyndication.com", "googleadservices.com", "adnxs.com", "adsrvr.org", "rubiconproject.com",
            "pubmatic.com", "openx.net", "criteo.com", "criteo.net", "taboola.com", "outbrain.com", "amazon-adsystem.com",
            "adsafeprotected.com", "moatads.com", "3lift.com", "casalemedia.com", "yieldmo.com", "sharethrough.com")
def is_ad_host(host):
    host = (host or "").lower()
    return any(host == a or host.endswith("." + a) for a in AD_HOSTS)
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

_ID_CACHE_FILE = os.path.join(HERE, "channel-ids.json")
_id_cache = {}
_id_fail = {}            # handle -> when resolution last failed
_ID_FAIL_TTL = 6 * 3600
try:
    with open(_ID_CACHE_FILE, encoding="utf-8") as f: _id_cache = json.load(f)
except Exception:
    pass

def _save_id_cache():
    """Once per refresh. Rewriting it per handle churns a file next to the script,
    which makes mitmproxy reload the addon mid-load."""
    try:
        tmp = _ID_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(_id_cache, f)
        os.replace(tmp, _ID_CACHE_FILE)
    except Exception as e:
        log(f"channel id cache write failed: {e}")


def resolve_handle(handle):
    """@handle -> UC... channel id via YouTube's own URL resolver (cached on disk)."""
    h = handle.lower()
    if h in _id_cache: return _id_cache[h]
    if time.time() - _id_fail.get(h, 0) < _ID_FAIL_TTL: return None   # don't retry a dud every refresh
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}},
                           "url": f"https://www.youtube.com/@{handle}"}).encode()
        req = urllib.request.Request("https://www.youtube.com/youtubei/v1/navigation/resolve_url?prettyPrint=false",
                                     data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=8) as r:
            cid = (json.load(r).get("endpoint") or {}).get("browseEndpoint", {}).get("browseId")
        if cid and cid.startswith("UC"):
            _id_cache[h] = cid
            return cid
        log(f"resolve @{handle}: no channel id in response")
    except Exception as e:
        log(f"resolve @{handle} failed: {e}")
    _id_fail[h] = time.time()
    return None

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
    # Publish the site list FIRST. host_allowed() fails closed until the first publish, and
    # resolving @handles costs a network round trip each - doing that first left every site
    # blocked for a minute after every start, watchdog restart and reboot.
    _publish(d, p, h, i)
    added = False
    for hd in sorted(h):
        cid = resolve_handle(hd)
        if cid and cid not in i:
            i.add(cid); added = True
    if added:
        _save_id_cache()
        _publish(d, p, h, i)


def _publish(d, p, h, i):
    # hosts needed to keep the sheet itself, the YouTube player and bot checks (which send no referer) working
    dd = set(d)
    for extra in ("docs.google.com", "googleusercontent.com", "googlevideo.com", "ytimg.com", "ggpht.com", "gstatic.com", "googleapis.com",
                  "challenges.cloudflare.com", "recaptcha.net"):
        dd.add(extra)
    if h or i:
        dd.update({"youtube.com", "youtube-nocookie.com"})
    with lock:
        L.domains, L.paths, L.handles, L.ids = dd, list(p), set(h), set(i)
        L.loaded, L.last_ok, L.last_err = True, time.time(), ""
    log(f"lists loaded: {len(dd)} domains, {len(p)} paths, {len(h)} handles, {len(i)} channel ids")

# ---------------------------------------------------------------- URL log
KEEP_PARAMS = ("v", "list", "q", "search_query")
SL = None            # SheetLog, when enabled
_local_log_lock = threading.Lock()


def _log_url(req):
    """Row key: host + path, keeping only the query params that identify a page."""
    base = req.host.lower() + (req.path.split("?")[0].rstrip("/") or "/")
    kept = []
    if "?" in req.path:
        for k, v in urllib.parse.parse_qsl(req.path.split("?", 1)[1]):
            if k in KEEP_PARAMS and v:
                kept.append((k, v[:80]))
    if kept:
        base += "?" + urllib.parse.urlencode(kept)
    return base[:400]


def _local_append(rec):
    path = CFG["urlLog"].get("localFile")
    if not path:
        return
    try:
        with _local_log_lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def record_url(flow, blocked, user=None, nav=None):
    ul = CFG["urlLog"]
    if not ul.get("enabled") and not ul.get("localFile"):
        return
    try:
        req = flow.request
        if nav is None:
            dest = req.headers.get("sec-fetch-dest", "").lower()
            nav = dest in NAV_DESTS or not dest
        url = _log_url(req)
        now = time.localtime()
        _local_append({"t": time.strftime("%Y-%m-%d %H:%M:%S", now), "user": user, "url": url,
                       "blocked": bool(blocked), "nav": bool(nav)})
        if SL is None or not (nav or ul.get("sheetAllRequests")):
            return
        tab = time.strftime("%m-%Y", now)
        SL.record(tab + " chan" if blocked else tab, url, now.tm_mday)
    except Exception as e:
        log(f"url log error: {e}")


def _url_log_flusher():
    every = max(60, int(CFG["urlLog"].get("flushSeconds", 300)))
    while True:
        time.sleep(every)
        try:
            n = SL.flush()
            if n:
                log(f"url log: pushed {n} rows")
        except Exception as e:
            log(f"url log push failed: {e} (kept locally, will retry)")


def _start_url_log():
    global SL
    ul = CFG["urlLog"]
    if not ul.get("enabled"):
        return
    if not ul.get("sheetId"):
        log("url log: enabled but sheetId is empty - disabled")
        return
    cred = ul.get("credentials") or ""
    if not os.path.isabs(cred):
        cred = os.path.join(HERE, cred)
    if not os.path.exists(cred):
        log(f"url log: credentials not found at {cred} - disabled")
        return
    try:
        sys.path.insert(0, HERE)
        from sheetlog import SheetLog
        SL = SheetLog(ul["sheetId"], cred, os.path.join(HERE, "urllog-state.json"), log)
    except Exception as e:
        log(f"url log: cannot start ({e}) - disabled")
        return
    threading.Thread(target=_url_log_flusher, daemon=True).start()
    log(f"url log: on, sheet {ul['sheetId']}, flush every {ul.get('flushSeconds')}s")


# ---------------------------------------------------------------- manual refresh
CONTROL_HOSTS = {"kidproxy", "kidproxy.local"}
CONTROL_MIN_INTERVAL = 10
_last_manual = [0.0]

CONTROL_HTML = """<!doctype html><html lang="vi"><meta charset="utf-8"><title>KidProxy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<body style="margin:0;font-family:system-ui,sans-serif;background:#f4f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh">
<div style="background:#fff;padding:36px 44px;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:620px">
<h1 style="font-size:20px;margin:0 0 8px">{title}</h1>
<p style="color:#555;margin:0 0 18px">{sub}</p>
<pre style="background:#f7f8fa;border-radius:8px;padding:14px;font-size:13px;color:#333;margin:0 0 18px;white-space:pre-wrap">{stats}</pre>
<a href="http://kidproxy.local/update" style="display:inline-block;background:#1a73e8;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px">Cập nhật lại</a>
</div></body></html>"""


def _control_stats():
    with lock:
        n_d, n_p, n_i, ok, err = len(L.domains), len(L.paths), len(L.ids), L.last_ok, L.last_err
    lines = ["Trang web được phép : %d tên miền%s" % (n_d, " + %d đường dẫn" % n_p if n_p else ""),
             "Kênh YouTube        : %d" % n_i,
             "Cập nhật lần cuối   : %s" % (
                 time.strftime("%H:%M:%S", time.localtime(ok)) + " (%ds trước)" % int(time.time() - ok)
                 if ok else "chưa lần nào")]
    if err:
        lines.append("Lỗi                 : %s" % err)
    return "\n".join(lines)


def control_response(path, accept=""):
    p = path.split("?")[0].rstrip("/") or "/"
    if p in ("/update", "/refresh"):
        now = time.time()
        waited = now - _last_manual[0]
        if waited < CONTROL_MIN_INTERVAL:
            title = "Vừa cập nhật xong"
            sub = "Đợi %d giây rồi bấm lại." % (CONTROL_MIN_INTERVAL - int(waited))
        else:
            _last_manual[0] = now
            with lock:
                before = set(L.domains)
            refresh()
            if SL is not None:
                try:
                    SL.flush()
                except Exception as e:
                    log(f"url log push failed: {e}")
            with lock:
                added, err = sorted(L.domains - before), L.last_err
            if err:
                title, sub = "Không tải được danh sách", err
            else:
                title = "Đã cập nhật danh sách"
                sub = ("Mới thêm: " + ", ".join(added)) if added else "Không có thay đổi mới."
    else:
        title, sub = "KidProxy", "Mở http://kidproxy.local/update để tải lại danh sách ngay."
    stats = _control_stats()
    if "text/html" in (accept or "").lower():
        body = CONTROL_HTML.format(title=title, sub=sub, stats=stats).encode("utf-8")
        ct = "text/html; charset=utf-8"
    else:
        body = ("%s\n%s\n\n%s\n" % (title, sub, stats)).encode("utf-8")
        ct = "text/plain; charset=utf-8"
    return http.Response.make(200, body, {"Content-Type": ct, "Cache-Control": "no-store"})



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
    hp = re.sub(r"^www\.", "", (host or "").lower()) + path
    with lock:
        return any(hp.startswith(p) for p in L.paths)

def channel_allowed(handle=None, cid=None):
    with lock:
        if handle and handle.lstrip("@").lower() in L.handles: return True
        if cid and cid in L.ids: return True
    return False

# hosts that were embedded (iframe) by a listed page; their own sub-resources are trusted for a while
_dyn = {}
def _dyn_allow(host):
    _dyn[host.lower()] = time.time() + int(CFG["dependencyTtlSeconds"])
def _dyn_ok(host):
    exp = _dyn.get((host or "").lower())
    return bool(exp and exp > time.time())

def _hdr_host(value):
    try:
        return (urllib.parse.urlsplit(value).hostname or "").lower()
    except Exception:
        return ""

NAV_DESTS = {"document"}
FRAME_DESTS = {"iframe", "frame", "embed", "object", "fencedframe"}
def smart_allowed(flow):
    """Smart mode. Only listed sites may be opened as a page (checked by the caller).
    - frames: allowed when embedded by a listed page (or by a frame that was itself allowed), and the
      frame's host is then trusted for a while so its own sub-resources load;
    - any other browser sub-resource (script, style, image, font, xhr, media, websocket...) is allowed,
      because it can only originate from a page that was itself allowed;
    - requests without Sec-Fetch-Dest (non-browser apps) and ad networks are never allowed this way."""
    if not CFG["smartDependencies"] or is_ad_host(flow.request.host): return False
    h = flow.request.headers
    dest = h.get("sec-fetch-dest", "").lower()
    if not dest or dest in NAV_DESTS: return False
    if dest in FRAME_DESTS:
        ref = _hdr_host(h.get("referer", "")) or _hdr_host(h.get("origin", ""))
        if ref and (host_allowed(ref) or _dyn_ok(ref)):
            _dyn_allow(flow.request.host)
            return True
        return False
    return True

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

_conn_cache = {}          # client connection id -> (user, enforced)


def _conn_key(o):
    c = getattr(o, "client_conn", None) or getattr(o, "client", None)
    return getattr(c, "id", None)


def _verdict(user):
    if user is None:
        return True                      # unknown -> fail closed
    u = user.lower()
    if u in SYSTEM_USERS or u.endswith("$"): return False
    if CFG["enforceUsers"]:
        return u in {x.lower() for x in CFG["enforceUsers"]}
    if u in {x.lower() for x in CFG["exemptUsers"]}: return False
    if u in _local_admins(): return False
    return True


def conn_info(flow_or_ctx):
    """(user, enforced) for this client connection. The OS lookup walks the whole TCP
    table, so it must happen once per connection, not once per request."""
    key = _conn_key(flow_or_ctx)
    if key is not None:
        hit = _conn_cache.get(key)
        if hit is not None: return hit
    info = (None, True)
    user = client_user(flow_or_ctx)
    info = (user, _verdict(user))
    if key is not None:
        if len(_conn_cache) > 4096: _conn_cache.clear()
        _conn_cache[key] = info
    return info


def enforced(flow_or_ctx):
    return conn_info(flow_or_ctx)[1]

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

def _find_channels(obj, out, depth=0):
    """Collect (handle, channelId) pairs from every browse endpoint inside a renderer."""
    if depth > 14: return
    if isinstance(obj, dict):
        if "reelWatchEndpoint" in obj: out.append(("__shorts__", None))
        be = obj.get("browseEndpoint")
        if isinstance(be, dict):
            base = be.get("canonicalBaseUrl") or ""
            bid = be.get("browseId") or ""
            m = re.match(r"^/@([^/?#]+)", base)
            if m or bid.startswith("UC"):
                out.append((m.group(1) if m else None, bid if bid.startswith("UC") else None))
        for v in obj.values(): _find_channels(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj: _find_channels(v, out, depth + 1)

def _item_allowed(item):
    found = []
    _find_channels(item, found)
    if any(h == "__shorts__" for h, _ in found) and CFG["blockShorts"]: return False
    found = [(h, c) for h, c in found if h != "__shorts__"]
    if not found: return False
    return any(channel_allowed(h, c) for h, c in found)

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
                if key == "guideEntryRenderer" and CFG["blockShorts"]:
                    if json.dumps(el).find('"FEshorts"') >= 0 or '"reelWatchEndpoint"' in json.dumps(el):
                        stats["dropped"] += 1; continue
            out.append(filter_json(el, stats, depth + 1))
        return out
    if isinstance(obj, dict):
        return {k: filter_json(v, stats, depth + 1) for k, v in obj.items()}
    return obj

def scrub_player_responses(obj, stats, depth=0):
    """Find every player response (any endpoint: player, get_watch, reel_item_watch, ...) and neuter it
    when the channel is not allowed."""
    if depth > 30: return
    if isinstance(obj, dict):
        if "playabilityStatus" in obj and ("videoDetails" in obj or "microformat" in obj):
            h, cid = _player_channel(obj)
            if not channel_allowed(h, cid):
                obj["playabilityStatus"] = {"status": "UNPLAYABLE", "reason": "Kênh này chưa được duyệt",
                    "errorScreen": {"playerErrorMessageRenderer": {
                        "reason": {"simpleText": "Kênh này chưa được duyệt"},
                        "subreason": {"simpleText": "Chỉ xem được video từ các kênh trong danh sách cho phép."}}}}
                for k in ("streamingData", "captions", "storyboards", "playerConfig", "adPlacements", "adSlots", "playerAds"):
                    obj.pop(k, None)
                stats["blocked"] += 1
                log(f"BLOCK video channel=@{h}/{cid}")
            return
        for v in obj.values(): scrub_player_responses(v, stats, depth + 1)
    elif isinstance(obj, list):
        for v in obj: scrub_player_responses(v, stats, depth + 1)

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

_start_lock = threading.Lock()


# ---------------------------------------------------------------- mitmproxy hooks
class KidProxy:
    def load(self, loader):
        # mitmproxy re-executes the script as a SEPARATE module when it sees a fresh mtime,
        # so a module-level flag is useless here - os.environ is shared by the whole process.
        # Without this we get one refresher thread and one sheet flusher per load, and the
        # flushers overwrite each other's counts.
        with _start_lock:
            if os.environ.get("KIDPROXY_STARTED") == str(os.getpid()): return
            os.environ["KIDPROXY_STARTED"] = str(os.getpid())
        threading.Thread(target=_refresher, daemon=True).start()
        _start_url_log()
        log(f"started; enforceUsers={CFG['enforceUsers'] or 'all non-admins'} exempt={CFG['exemptUsers']}")

    def http_connect(self, flow: http.HTTPFlow):
        host = flow.request.host
        if host.lower() in CONTROL_HOSTS:
            flow.response = http.Response.make(403, b"kidproxy: dung http://kidproxy.local/update")
            return
        if not enforced(flow): return
        if CFG["smartDependencies"]: return          # decided per request after TLS interception
        if not host_allowed(host):
            flow.response = http.Response.make(403, b"blocked by kidproxy: " + host.encode())
            log(f"BLOCK site {host} ({conn_info(flow)[0]})")

    def tls_clienthello(self, data):
        host = (data.context.server.address or ("",))[0].lower()
        if not enforced(data.context):
            data.ignore_connection = True   # admins: pass through untouched
            return
        if CFG["smartDependencies"]: return            # intercept everything for filtered users
        if host not in YT_HOSTS:
            data.ignore_connection = True

    def client_disconnected(self, client):
        _conn_cache.pop(getattr(client, "id", None), None)

    def responseheaders(self, flow: http.HTTPFlow):
        # only YouTube bodies are ever rewritten; stream everything else instead of
        # buffering it whole (matters for video segments and big assets)
        if flow.response is not None and flow.request.host.lower() not in YT_HOSTS:
            flow.response.stream = True

    def request(self, flow: http.HTTPFlow):
        if flow.request.host.lower() in CONTROL_HOSTS:
            flow.response = control_response(flow.request.path, flow.request.headers.get("accept", ""))
            return
        user, filtered = conn_info(flow)
        if not filtered: return
        host, path = flow.request.host.lower(), flow.request.path
        dest = flow.request.headers.get("sec-fetch-dest", "").lower()
        nav = dest in NAV_DESTS or not dest
        blocked = False
        if host not in YT_HOSTS:
            if not (url_allowed(host, path) or smart_allowed(flow)):
                blocked = True
                if nav:
                    flow.response = blocked_page("Trang này không nằm trong danh sách được phép", host)
                else:
                    flow.response = http.Response.make(403, b"blocked by kidproxy")
                log(f"BLOCK site {host} dest={dest or '-'} ref={_hdr_host(flow.request.headers.get('referer',''))} ({user})")
        elif CFG["blockShorts"] and (path.startswith("/shorts") or path.startswith("/youtubei/v1/reel/")):
            blocked = True
            flow.response = blocked_page("YouTube Shorts đã bị tắt", "") if path.startswith("/shorts") else http.Response.make(403, b"shorts blocked")
        # a /watch page is only really "allowed" once the channel check in response() passes
        if not (host in YT_HOSTS and path.split("?")[0] == "/watch" and nav and not blocked):
            record_url(flow, blocked, user, nav)

    def response(self, flow: http.HTTPFlow):
        host, path = flow.request.host.lower(), flow.request.path
        if host not in YT_HOSTS or flow.response is None or not enforced(flow): return
        ct = flow.response.headers.get("content-type", "")
        p = path.split("?")[0]
        try:
            if "json" in ct and p.startswith("/youtubei/"):
                data = json.loads(flow.response.get_text(strict=False) or "null")
                st = {"dropped": 0, "blocked": 0}
                scrub_player_responses(data, st)
                if CFG["filterFeeds"]: data = filter_json(data, st)
                if st["dropped"] or st["blocked"]:
                    flow.response.set_text(json.dumps(data, separators=(",", ":")))
            elif p == "/watch" and "html" in ct:
                body = flow.response.get_text(strict=False) or ""
                h, cid = _html_channel(body)
                if not channel_allowed(h, cid):
                    flow.response = blocked_page("Kênh YouTube này chưa được duyệt", f"@{h}" if h else (cid or ""))
                    log(f"BLOCK watch channel=@{h}/{cid}")
                    record_url(flow, True, conn_info(flow)[0], True)
                else:
                    record_url(flow, False, conn_info(flow)[0], True)
                    if CFG["filterFeeds"]:
                        st = {"dropped": 0, "blocked": 0}
                        flow.response.set_text(_rewrite_initial_data(body, st))
            elif CFG["filterFeeds"] and "html" in ct and (p == "/" or p.startswith(("/results", "/feed", "/@", "/channel/", "/c/", "/user/", "/playlist"))):
                body = flow.response.get_text(strict=False) or ""
                st = {"dropped": 0, "blocked": 0}
                flow.response.set_text(_rewrite_initial_data(body, st))
        except Exception as e:
            log(f"filter error on {p}: {e}")

addons = [KidProxy()]
