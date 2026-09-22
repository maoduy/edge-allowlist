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
import csv, io, json, os, re, sys, threading, time, types, urllib.request, urllib.parse, subprocess, platform
from mitmproxy import http, ctx

HERE = os.path.dirname(os.path.abspath(__file__))

# mitmproxy loads this script as SEVERAL independent modules. Module-level globals are
# therefore per-load, not per-process: each copy would refresh its own list, and only the
# copy whose addon instance serves a request decides that request. Everything that must be
# singular lives on one object parked in sys.modules instead.
_shared = sys.modules.get("__kidproxy_shared__")
if _shared is None:
    _shared = types.ModuleType("__kidproxy_shared__")
    _shared.lists = None
    _shared.lock = threading.Lock()
    _shared.counts = None
    _shared.sheet = None
    _shared.usage = None
    _shared.refresher = False
    _shared.urllog = False
    _shared.start_lock = threading.Lock()
    sys.modules["__kidproxy_shared__"] = _shared
DEFAULTS = {
    # Preferred: the spreadsheet id (or its full link) plus TAB NAMES. gid numbers differ in
    # every copy of a sheet, so a shared setup cannot use them.
    "sheetId": "",
    "sitesTab": "websites",
    "channelsTab": "youtube",
    # Explicit CSV urls still win when set, for anything the pair above cannot express.
    "sitesUrl": "",
    "channelsUrl": "",
    "refreshSeconds": 900,
    "exemptUsers": [],
    "enforceUsers": [],      # if non-empty: ONLY these users are filtered
    "blockShorts": True,
    "filterFeeds": True,
    # smart mode: only page navigations must be to a listed site; resources a listed page loads
    # (scripts, images, video, embedded players) are allowed automatically via Referer/Origin.
    "smartDependencies": True,
    "dependencyTtlSeconds": 3600,
    # A minute counts toward a budget after this many browser requests, or at once on a
    # page navigation. Stops a background tab burning the day's allowance while nobody
    # is at the machine. 1 = count every minute that saw any browser traffic.
    "activityRequests": 3,
    "dataDir": "",          # empty -> C:\ProgramData\KidNest
    "logFile": "",          # empty -> kidproxy.log inside dataDir
    # URL log -> Google Sheet: one tab per month "MM-yyyy", row = URL, column = day, cell = hits.
    # Blocked attempts go to "MM-yyyy chan". Only filtered (kid) accounts are logged.
    "urlLog": {
        "enabled": False,
        "sheetId": "",
        "credentials": "kidproxy-sheets.json",   # its OWN service account, not a shared one
        "flushSeconds": 300,
        "sheetAllRequests": False,   # False = only page navigations reach the sheet
        "localFile": "",             # set a path to also keep a JSONL of EVERY request
        "localMaxMB": 20,            # rotate that JSONL at this size (one generation kept)
    },
}
CFG = dict(DEFAULTS)


def _pick_data_dir(preferred):
    """Everything written at runtime lives here - NOT in Program Files. Controlled Folder
    Access blocks unrecognised binaries from writing there, silently, which looks exactly
    like the proxy never starting. Falls back to TEMP so there is always somewhere to log."""
    for cand in (preferred,
                 r"C:\ProgramData\KidNest" if os.name == "nt" else None,
                 os.path.join(os.environ.get("TEMP") or "/tmp", "KidNest")):
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write-test")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            return cand
        except Exception:
            continue
    return HERE

try:
    # utf-8-sig, not utf-8: Windows PowerShell 5.1's Set-Content -Encoding UTF8 writes a
    # BOM, and a plain utf-8 read then fails on the very first character - the config is
    # silently discarded, sheetId comes back empty and everything is blocked.
    with open(os.path.join(HERE, "kidproxy.json"), encoding="utf-8-sig") as f:
        _user_cfg = json.load(f)
    _ul = dict(DEFAULTS["urlLog"]); _ul.update(_user_cfg.pop("urlLog", {}) or {})
    CFG.update(_user_cfg); CFG["urlLog"] = _ul
    CFG["_configLoaded"] = True
except FileNotFoundError:
    CFG["_configError"] = "kidproxy.json not found in " + HERE
except Exception as e:
    CFG["_configError"] = "kidproxy.json is malformed: %s" % e

DATA = _pick_data_dir(CFG.get("dataDir"))

GLOBAL = "*"          # the whole-internet budget, written as a "*" row in the sheet

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

def _log_path():
    """Never rely on the config for this. If kidproxy.json is missing or malformed there
    would be no log at all - and no way to find out why everything is being blocked."""
    return CFG.get("logFile") or os.path.join(DATA, "kidproxy.log")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    if True:                                  # file first: it is the only log a SYSTEM task has
        try:
            with open(_log_path(), "a", encoding="utf-8") as f: f.write(line + "\n")
        except Exception: pass
    try:
        print("kidproxy:", msg)
    except Exception:
        pass          # no console under Task Scheduler -> stdout is an invalid handle

# ---------------------------------------------------------------- lists
class Lists:
    domains: set = set()
    paths: list = []
    handles: set = set()
    ids: set = set()
    limits: dict = {}        # domain -> minutes allowed per day (0 / missing = unlimited)
    loaded = False
    last_ok = 0
    last_err = ""

if _shared.lists is None:
    _shared.lists = Lists()
L = _shared.lists
lock = _shared.lock

def sheet_id(value):
    """Accept a full spreadsheet link or a bare id and return the id."""
    v = (value or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", v)
    if m:
        return m.group(1)
    m = re.match(r"^([A-Za-z0-9_-]{20,})(?:[/?#].*)?$", v)   # id with /edit?usp=... attached
    return m.group(1) if m else ""


def sheet_csv_url(sid, tab):
    """CSV for one tab BY NAME - survives being copied, unlike an export?gid= link."""
    return ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=%s"
            % (sid, urllib.parse.quote(tab)))


def source_urls():
    sid = sheet_id(CFG.get("sheetId"))
    sites, chans = CFG.get("sitesUrl") or "", CFG.get("channelsUrl") or ""
    if sid:
        sites = sites or sheet_csv_url(sid, CFG.get("sitesTab") or "websites")
        chans = chans or sheet_csv_url(sid, CFG.get("channelsTab") or "youtube")
    return sites, chans


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

def _parse_minutes(cell):
    """Column C of the websites tab -> minutes. '30' and '30 phút' are 30 minutes; '1h',
    '1 giờ' and '60' are an hour; '1:30' and '1h30' are 90. Blank or 0 means no limit."""
    c = (cell or "").strip().lower()
    if not c:
        return 0
    m = re.match(r"^(\d+)\s*[:h]\s*(\d+)\s*$", c)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+(?:[.,]\d+)?)", c)
    if not m:
        return 0
    n = float(m.group(1).replace(",", "."))
    unit = c[m.end():]                     # only what FOLLOWS the number is the unit:
    if re.search(r"ph[uú]t|min", unit):    # "30 phút" must not read as hours because of its h
        pass
    elif re.search(r"gi[ờo]|hour|h", unit):
        n *= 60
    return int(round(n))


def _sites_with_limits(text):
    sites, limits = [], {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        v = row[0].strip()
        if not v or v.startswith("#") or v.lower() in HEADERS:
            continue
        mins = _parse_minutes(row[2] if len(row) > 2 else "")
        if v.strip() in ("*", "**"):        # the whole-internet budget, not a site
            if mins:
                limits[GLOBAL] = mins
            continue
        sites.append(v)
        if mins:
            kind, dom = _norm_site(v)
            if kind == "domain":
                limits[dom] = mins
    return sites, limits


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

_ID_CACHE_FILE = os.path.join(DATA, "channel-ids.json")
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
    """@handle -> UC... channel id via YouTube's own URL resolver (cached on disk).

    Some handles (seen with @codeorg) come back as a bare urlEndpoint pointing at
    "youtube.com/<legacy-name>" instead of a browseEndpoint - the resolver knows the
    handle exists but does not hand back its id directly. Falling back to fetching the
    channel page itself and reading its externalId covers those."""
    h = handle.lower()
    if h in _id_cache: return _id_cache[h]
    if time.time() - _id_fail.get(h, 0) < _ID_FAIL_TTL: return None   # don't retry a dud every refresh
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}},
                           "url": f"https://www.youtube.com/@{handle}"}).encode()
        req = urllib.request.Request("https://www.youtube.com/youtubei/v1/navigation/resolve_url?prettyPrint=false",
                                     data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=8) as r:
            cid = (json.load(r).get("endpoint") or {}).get("browseEndpoint", {}).get("browseId")
        if cid and cid.startswith("UC"):
            _id_cache[h] = cid
            return cid
    except Exception as e:
        log(f"resolve @{handle} failed: {e}")
        _id_fail[h] = time.time()
        return None
    try:
        req = urllib.request.Request(f"https://www.youtube.com/@{handle}", headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=8) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'"externalId":"(UC[\w-]+)"', html)
        if m:
            _id_cache[h] = m.group(1)
            return m.group(1)
        log(f"resolve @{handle}: no channel id in response or page")
    except Exception as e:
        log(f"resolve @{handle} page fallback failed: {e}")
    _id_fail[h] = time.time()
    return None

def refresh():
    sites_url, chans_url = source_urls()
    if not sites_url:
        L.last_err = "no sheet configured"
        log("no sheet configured (sheetId is empty) - everything stays blocked")
        return
    try:
        sites, limits = _sites_with_limits(_fetch(sites_url))
        chans = _first_col(_fetch(chans_url)) if chans_url else []
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
    _publish(d, p, h, i, limits)
    added = False
    for hd in sorted(h):
        cid = resolve_handle(hd)
        if cid and cid not in i:
            i.add(cid); added = True
    if added:
        _save_id_cache()
        _publish(d, p, h, i, limits)


def _publish(d, p, h, i, limits=None):
    # hosts needed to keep the sheet itself, the YouTube player and bot checks (which send no referer) working
    dd = set(d)
    for extra in ("docs.google.com", "googleusercontent.com", "googlevideo.com", "ytimg.com", "ggpht.com", "gstatic.com", "googleapis.com",
                  "challenges.cloudflare.com", "recaptcha.net"):
        dd.add(extra)
    if h or i:
        dd.update({"youtube.com", "youtube-nocookie.com"})
    with lock:
        L.domains, L.paths, L.handles, L.ids = dd, list(p), set(h), set(i)
        if limits is not None:
            L.limits = dict(limits)
        L.loaded, L.last_ok, L.last_err = True, time.time(), ""
    lim = " ".join("%s=%dm" % kv for kv in sorted((limits or L.limits).items()))
    log(f"lists loaded: {len(dd)} domains, {len(p)} paths, {len(h)} handles, {len(i)} channel ids"
        + (f", limits: {lim}" if lim else ""))

# ---------------------------------------------------------------- URL log
KEEP_PARAMS = ("v", "list", "q", "search_query")
_local_log_lock = threading.Lock()
MAX_DAYS = 31


class Counts:
    """tab -> url -> {day: hits}, persisted locally. The viewer reads it directly;
    pushing it to Google Sheets is optional and layered on top."""

    def __init__(self, path, log=print):
        self.path, self.log = path, log
        self.lock = threading.Lock()
        self.totals, self.rows, self.dirty = {}, {}, {}
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
            self.totals, self.rows = st.get("totals", {}), st.get("rows", {})
        except Exception:
            pass

    def record(self, tab, url, day):
        with self.lock:
            t = self.totals.setdefault(tab, {}).setdefault(url, {})
            k = str(day)
            t[k] = t.get(k, 0) + 1
            self.dirty.setdefault(tab, set()).add(day)

    def save(self):
        try:
            with self.lock:
                blob = json.dumps({"totals": self.totals, "rows": self.rows}, ensure_ascii=False)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        except Exception as e:
            self.log(f"url log: cannot save counts: {e}")

    def months(self):
        with self.lock:
            return sorted({t[:7] for t in self.totals}, reverse=True)

    def grid(self, tab):
        """[(url, {day: n}, total)] sorted by total, busiest first."""
        with self.lock:
            rows = [(u, dict(d), sum(d.values())) for u, d in self.totals.get(tab, {}).items()]
        return sorted(rows, key=lambda r: (-r[2], r[0]))


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
    path = CFG["urlLog"].get("localFile") or os.path.join(DATA, "urls.jsonl")
    try:
        cap = int(CFG["urlLog"].get("localMaxMB", 20)) * 1024 * 1024
        with _local_log_lock:
            if cap and os.path.exists(path) and os.path.getsize(path) > cap:
                os.replace(path, path + ".1")     # keep one generation, never grow forever
            with open(path, "a", encoding="utf-8") as f:
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
        if _shared.counts is None or not (nav or ul.get("sheetAllRequests")):
            return
        tab = time.strftime("%m-%Y", now)
        _shared.counts.record(tab + " chan" if blocked else tab, url, now.tm_mday)
    except Exception as e:
        log(f"url log error: {e}")


def _url_log_flusher():
    every = max(60, int(CFG["urlLog"].get("flushSeconds", 300)))
    while True:
        time.sleep(every)
        try:
            n = _shared.sheet.flush()
            if n:
                log(f"url log: pushed {n} rows")
        except Exception as e:
            log(f"url log push failed: {e} (kept locally, will retry)")


def _start_url_log():
    ul = CFG["urlLog"]
    if not ul.get("enabled"):
        return
    with _shared.start_lock:
        if _shared.urllog:
            return
        _shared.urllog = True
    _shared.counts = Counts(os.path.join(DATA, "urllog-state.json"), log)
    threading.Thread(target=_counts_saver, daemon=True).start()
    log("url log: on - http://kidnest.local/log")

    # Optional: also mirror it into a Google Sheet. Drop a service-account key in and
    # set sheetId to turn this on later; nothing else changes.
    if not ul.get("sheetId"):
        return
    cred = ul.get("credentials") or ""
    if not os.path.isabs(cred):
        cred = os.path.join(HERE, cred)
    if not os.path.exists(cred):
        log(f"url log: sheet mirror off, no key at {cred}")
        return
    try:
        sys.path.insert(0, HERE)
        from sheetlog import SheetLog
        _shared.sheet = SheetLog(ul["sheetId"], cred, _shared.counts, log)
    except Exception as e:
        log(f"url log: sheet mirror off ({e})")
        return
    threading.Thread(target=_url_log_flusher, daemon=True).start()
    log(f"url log: mirroring to sheet {ul['sheetId']} every {ul.get('flushSeconds')}s")


def _usage_saver():
    while True:
        time.sleep(60)
        if _shared.usage is not None:
            _shared.usage.save()


def _counts_saver():
    while True:
        time.sleep(60)
        if _shared.counts is not None:
            _shared.counts.save()


# ---------------------------------------------------------------- manual refresh
CONTROL_HOSTS = {"kidnest", "kidnest.local", "kidproxy", "kidproxy.local"}
CONTROL_MIN_INTERVAL = 10
_last_manual = [0.0]

CONTROL_HTML = """<!doctype html><html lang="vi"><meta charset="utf-8"><title>KidProxy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<body style="margin:0;font-family:system-ui,sans-serif;background:#f4f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh">
<div style="background:#fff;padding:36px 44px;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:620px">
<h1 style="font-size:20px;margin:0 0 8px">{title}</h1>
<p style="color:#555;margin:0 0 18px">{sub}</p>
<pre style="background:#f7f8fa;border-radius:8px;padding:14px;font-size:13px;color:#333;margin:0 0 18px;white-space:pre-wrap">{stats}</pre>
<a href="http://kidnest.local/update" style="display:inline-block;background:#1a73e8;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px">Cập nhật lại</a>
</div></body></html>"""


def _usage_lines():
    """[(user, domain, used, cap)] - every budget, for every account that used one."""
    with lock:
        caps = dict(L.limits)
    if not caps or _shared.usage is None:
        return []
    today = _shared.usage.today()
    rows = []
    for who in sorted(today) or [""]:
        for d, c in sorted(caps.items()):
            rows.append((who, d, today.get(who, {}).get(d, 0), c))
    return rows


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
    for who, d, used, cap in _usage_lines():
        lines.append("%-10s %-16s: %d/%d phút hôm nay"
                     % (who, "TẤT CẢ" if d == GLOBAL else d, used, cap))
    return "\n".join(lines)



LOG_HTML = """<!doctype html><html lang="vi"><meta charset="utf-8"><title>KidProxy - nhật ký</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{margin:0;font-family:system-ui,sans-serif;background:#f4f5f7;color:#222;padding:24px}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 8px}}
 .sub{{color:#666;font-size:13px;margin:0 0 16px}}
 .m a{{display:inline-block;margin:0 6px 6px 0;padding:5px 11px;border-radius:7px;background:#fff;
      border:1px solid #dcdfe4;color:#1a73e8;text-decoration:none;font-size:13px}}
 .m a.on{{background:#1a73e8;color:#fff;border-color:#1a73e8}}
 .wrap{{overflow-x:auto;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
 table{{border-collapse:collapse;font-size:13px;width:100%}}
 th,td{{padding:6px 9px;border-bottom:1px solid #eef0f2;text-align:right;white-space:nowrap}}
 th:first-child,td:first-child{{text-align:left;position:sticky;left:0;background:#fff;
      max-width:520px;overflow:hidden;text-overflow:ellipsis}}
 thead th{{background:#fafbfc;position:sticky;top:0;font-weight:600;color:#555}}
 td.z{{color:#dfe2e6}} .tot{{font-weight:600}} .today{{background:#fff7e6}}
 .none{{color:#777;font-size:13px;padding:14px}}
 .bar{{margin:0 0 18px}} .bar a{{color:#1a73e8;font-size:13px;margin-right:14px}}
</style>
<body>
<h1>Nhật ký truy cập</h1>
<p class="sub">Mỗi dòng là một trang, mỗi cột là một ngày trong tháng.</p>
<div class="m">{months}</div>
<div class="bar"><a href="http://kidnest.local/log.csv?m={month}">Tải CSV tháng này</a>
<a href="http://kidnest.local/update">Cập nhật danh sách</a></div>
{tables}
</body></html>"""


def _esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _log_table(tab, today):
    rows = _shared.counts.grid(tab) if _shared.counts else []
    if not rows:
        return '<div class="wrap"><div class="none">Chưa có dữ liệu.</div></div>'
    days = sorted({int(d) for _, dd, _ in rows for d in dd})
    head = "".join('<th class="%s">%d</th>' % ("today" if d == today else "", d) for d in days)
    body = []
    for url, dd, total in rows:
        cells = "".join('<td class="%s">%s</td>' % (
            ("today " if d == today else "") + ("" if dd.get(str(d)) else "z"),
            dd.get(str(d), "\u00b7")) for d in days)
        body.append("<tr><th>%s</th>%s<td class='tot'>%d</td></tr>" % (_esc(url), cells, total))
    return ('<div class="wrap"><table><thead><tr><th>URL</th>%s<th>Tổng</th></tr></thead>'
            "<tbody>%s</tbody></table></div>" % (head, "".join(body)))


def log_page(query):
    now = time.localtime()
    cur = time.strftime("%m-%Y", now)
    want = (urllib.parse.parse_qs(query).get("m") or [cur])[0]
    have = _shared.counts.months() if _shared.counts else []
    if cur not in have:
        have = [cur] + have
    if want not in have:
        want = have[0]
    months = "".join('<a class="%s" href="http://kidnest.local/log?m=%s">%s</a>'
                     % ("on" if m == want else "", m, m) for m in have)
    today = now.tm_mday if want == cur else -1
    budget = ""
    rows = _usage_lines() if want == cur else []
    if rows:
        budget = ("<h2>Thời gian hôm nay</h2><div class='wrap'><table><thead><tr>"
                  "<th>Tài khoản</th><th>Trang</th><th>Đã dùng</th><th>Giới hạn</th>"
                  "<th>Còn lại</th></tr></thead><tbody>"
                  + "".join("<tr><th>%s</th><td style='text-align:left'>%s</td>"
                            "<td>%d</td><td>%d</td><td class='tot'>%d</td></tr>"
                            % (_esc(w), _esc("TẤT CẢ" if d == GLOBAL else d), u, c, max(0, c - u))
                            for w, d, u, c in rows)
                  + "</tbody></table></div>")
    tables = (budget + "<h2>Đã vào</h2>" + _log_table(want, today) +
              "<h2>Bị chặn</h2>" + _log_table(want + " chan", today))
    body = LOG_HTML.format(months=months, month=want, tables=tables).encode("utf-8")
    return http.Response.make(200, body, {"Content-Type": "text/html; charset=utf-8",
                                          "Cache-Control": "no-store"})


def log_csv(query):
    want = (urllib.parse.parse_qs(query).get("m") or [time.strftime("%m-%Y")])[0]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Loại", "URL"] + [str(d) for d in range(1, MAX_DAYS + 1)] + ["Tổng"])
    for label, tab in (("Đã vào", want), ("Bị chặn", want + " chan")):
        for url, dd, total in (_shared.counts.grid(tab) if _shared.counts else []):
            w.writerow([label, url] + [dd.get(str(d), "") for d in range(1, MAX_DAYS + 1)] + [total])
    return http.Response.make(200, out.getvalue().encode("utf-8-sig"),
                              {"Content-Type": "text/csv; charset=utf-8",
                               "Content-Disposition": 'attachment; filename="kidproxy-%s.csv"' % want,
                               "Cache-Control": "no-store"})


def control_response(path, accept=""):
    p = path.split("?")[0].rstrip("/") or "/"
    q = path.split("?", 1)[1] if "?" in path else ""
    if p == "/log":
        return log_page(q)
    if p in ("/log.csv", "/logcsv"):
        return log_csv(q)
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
            if _shared.sheet is not None:
                try:
                    _shared.sheet.flush()
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
        title, sub = "KidProxy", "/update để tải lại danh sách, /log để xem nhật ký truy cập."
    stats = _control_stats()
    if "text/html" in (accept or "").lower():
        body = CONTROL_HTML.format(title=title, sub=sub, stats=stats).encode("utf-8")
        ct = "text/html; charset=utf-8"
    else:
        body = ("%s\n%s\n\n%s\n" % (title, sub, stats)).encode("utf-8")
        ct = "text/plain; charset=utf-8"
    return http.Response.make(200, body, {"Content-Type": ct, "Cache-Control": "no-store"})



def _refresher():
    # At boot the network is often not up yet. refresh() keeps the previous list on failure,
    # but before the FIRST success there is no list and host_allowed() fails closed - so retry
    # quickly until we have one instead of waiting out a full refresh interval.
    delay = 5
    while True:
        try:
            refresh()
        except Exception as e:
            log(f"refresh crashed: {e}")
        with lock:
            loaded = L.loaded
        if loaded:
            delay = 5
            time.sleep(max(60, int(CFG["refreshSeconds"])))
        else:
            log(f"no list yet, retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 120)


class Usage:
    """Which minutes of the day each account spent on each limited site, plus a "*" total
    across everything. A minute counts once however many requests it held, and it is
    counted PER ACCOUNT - one child running out does not spend another's allowance.

    A minute only counts once it looks like real use: a page navigation, or enough
    requests to rule out a background tab polling while nobody is at the machine."""

    VERSION = 2

    def __init__(self, path, log=print):
        self.path, self.log = path, log
        self.lock = threading.Lock()
        self.days = {}          # day -> user -> domain -> set(minute-of-day)
        self._pending = {}      # (day, user, domain, minute) -> requests seen so far
        try:
            with open(path, encoding="utf-8-sig") as f:
                blob = json.load(f)
            if blob.get("v") == self.VERSION:       # v1 had no per-user dimension
                for day, users in (blob.get("days") or {}).items():
                    self.days[day] = {u: {k: set(v) for k, v in doms.items()}
                                      for u, doms in users.items()}
        except Exception:
            pass

    # ------------------------------------------------------------------ reads
    def used(self, day, user, domain):
        with self.lock:
            return len(self.days.get(day, {}).get(user, {}).get(domain, ()))

    def has(self, day, user, domain, minute):
        with self.lock:
            return minute in self.days.get(day, {}).get(user, {}).get(domain, ())

    def today(self):
        """{user: {domain: minutes}} for today."""
        day = time.strftime("%Y-%m-%d")
        with self.lock:
            return {u: {k: len(v) for k, v in doms.items()}
                    for u, doms in self.days.get(day, {}).items()}

    # ------------------------------------------------------------------ writes
    def touch(self, day, user, domain, minute):
        with self.lock:
            self.days.setdefault(day, {}).setdefault(user, {}).setdefault(domain, set()).add(minute)

    def note(self, day, user, domain, minute, nav, threshold):
        """Count this minute once it is clearly someone using the machine."""
        if self.has(day, user, domain, minute):
            return
        key = (day, user, domain, minute)
        with self.lock:
            seen = self._pending.get(key, 0) + 1
            self._pending[key] = seen
        if nav or seen >= max(1, threshold):
            self.touch(day, user, domain, minute)
            with self.lock:
                self._pending.pop(key, None)

    def save(self):
        keep = {time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))}
        try:
            with self.lock:
                for stale in [d for d in self.days if d not in keep]:
                    del self.days[stale]
                for k in [k for k in self._pending if k[0] not in keep]:
                    del self._pending[k]
                blob = json.dumps({"v": self.VERSION, "days": {
                    d: {u: {k: sorted(v) for k, v in doms.items()} for u, doms in users.items()}
                    for d, users in self.days.items()}})
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        except Exception as e:
            self.log(f"usage: cannot save: {e}")


def matched_domain(host):
    """Which allowlist domain this host falls under, if any."""
    host = (host or "").lower().rstrip(".")
    with lock:
        for d in L.domains:
            if host == d or host.endswith("." + d):
                return d
    return None


def limited_site(flow):
    """(domain, minutes) of the limited site this request belongs to. The referer wins, so
    the video stream on googlevideo.com counts against youtube.com, not against itself."""
    h = flow.request.headers
    ref = _hdr_host(h.get("referer", "")) or _hdr_host(h.get("origin", ""))
    for cand in (ref, flow.request.host):
        if not cand:
            continue
        d = matched_domain(cand)
        if d:
            with lock:
                cap = L.limits.get(d, 0)
            if cap:
                return d, cap
    return None, 0


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
            # by SID, because "Administrators" is translated on a localised Windows
            ps = (r"Get-LocalGroupMember -SID S-1-5-32-544 | "
                  r"ForEach-Object { ($_.Name -split '\\')[-1] }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=20).stdout
            if not out.strip():
                out = subprocess.run(["net", "localgroup", "Administrators"],
                                     capture_output=True, text=True, timeout=10).stdout
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
    iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
    iphlp.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                          wintypes.BOOL, wintypes.ULONG,
                                          ctypes.c_int, wintypes.ULONG]
    iphlp.GetExtendedTcpTable.restype = wintypes.DWORD
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
    # Private WinDLL instances, not ctypes.windll: that one is shared process-wide, so
    # setting argtypes on it would also rewrite the signatures mitmproxy itself uses.
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Undeclared functions are assumed to take and return C ints. On 64-bit Windows a
    # HANDLE and a PSID are 64 bits, so handles came back truncated and passing a SID
    # raised "int too long to convert" - the lookup returned None, every connection
    # looked like an unknown user, and the addon failed closed on all of them.
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                      ctypes.POINTER(wintypes.DWORD)]
    adv.LookupAccountSidW.restype = wintypes.BOOL
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        log(f"user lookup: OpenProcess({pid}) failed, err={ctypes.get_last_error()}")
        return None
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
            if pid is None:
                log(f"user lookup: no process owns local port {port}")
                return None
            now = time.time()
            u = _pid_user.get(pid)
            if u and now - u[1] < 60: return u[0]
            name = _user_for_pid_windows(pid)
            if name is None:
                log(f"user lookup: pid {pid} found but no account name")
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

# ---------------------------------------------------------------- mitmproxy hooks
class KidProxy:
    def load(self, loader):
        with _shared.start_lock:
            first = not _shared.refresher
            _shared.refresher = True
        if first:                       # shared L, so one refresher serves every loaded copy
            log("data dir: " + DATA)
            log("config: " + (CFG.get("_configError") or
                              ("loaded, sheet=" + (sheet_id(CFG.get("sheetId")) or "NONE SET"))))
            log("enforceUsers=%s exempt=%s" % (CFG.get("enforceUsers"), CFG.get("exemptUsers")))
            threading.Thread(target=_refresher, daemon=True).start()
            _shared.usage = Usage(os.path.join(DATA, "usage-state.json"), log)
            threading.Thread(target=_usage_saver, daemon=True).start()
        _start_url_log()
        log(f"started; enforceUsers={CFG['enforceUsers'] or 'all non-admins'} exempt={CFG['exemptUsers']}")

    def http_connect(self, flow: http.HTTPFlow):
        host = flow.request.host
        if host.lower() in CONTROL_HOSTS:
            flow.response = http.Response.make(403, b"kidproxy: dung http://kidnest.local/update")
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
        if not blocked:
            blocked = self._meter(flow, nav, user)
        # a /watch page is only really "allowed" once the channel check in response() passes
        if not (host in YT_HOSTS and path.split("?")[0] == "/watch" and nav and not blocked):
            record_url(flow, blocked, user, nav)

    def _meter(self, flow, nav, user):
        """Daily time budgets: one per site from column C, plus the "*" row covering all
        web use. Both are counted per account, so one child cannot spend another's."""
        u = _shared.usage
        if u is None:
            return False
        # Only browser traffic is metered. Windows Update and background apps send no
        # Sec-Fetch-Dest, and should not burn a child's allowance.
        if not flow.request.headers.get("sec-fetch-dest", ""):
            return False
        who = (user or "?").lower()
        now = time.localtime()
        day, minute = time.strftime("%Y-%m-%d", now), now.tm_hour * 60 + now.tm_min
        site, site_cap = limited_site(flow)
        with lock:
            total_cap = L.limits.get(GLOBAL, 0)
        threshold = int(CFG.get("activityRequests", 3))

        for target, cap, label in ((site, site_cap, site),
                                   (GLOBAL, total_cap, None)):
            if not target or not cap:
                continue
            used = u.used(day, who, target)
            if used >= cap and not u.has(day, who, target, minute):
                if label:
                    title = "Hết giờ cho %s hôm nay" % label
                else:
                    title = "Hết giờ vào mạng hôm nay"
                if nav:
                    flow.response = blocked_page(
                        title, "Đã dùng %d/%d phút. Mai được dùng tiếp." % (used, cap))
                else:
                    flow.response = http.Response.make(403, b"kidnest: daily time limit reached")
                log(f"LIMIT {target} {used}/{cap}m ({who})")
                return True
            u.note(day, who, target, minute, nav, threshold)
        return False

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
