"""
Google Sheets writer for KidProxy URL logs.

One tab per month, named MM-yyyy. Row = URL, column = day of month, cell = number of hits.
Blocked attempts go to a parallel tab "MM-yyyy chan".

Deliberately stdlib + `cryptography` only: KidProxy runs inside the standalone mitmdump.exe,
a frozen bundle where pip install is impossible. `cryptography` is a mitmproxy dependency so
it is always present; google-auth / googleapiclient are not.
"""
import base64, json, time, urllib.error, urllib.parse, urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
MAX_DAYS = 31


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def a1(tab, rng):
    """Quote a tab name so spaces and unicode are safe inside an A1 range."""
    return "'" + tab.replace("'", "''") + "'!" + rng


def col_name(idx):
    """1 -> A, 2 -> B, ... (day d lives in column d+1)"""
    s = ""
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


class SheetLog:
    def __init__(self, sheet_id, cred_path, counts, log=print):
        self.sheet_id, self.cred_path, self.log = sheet_id, cred_path, log
        self.counts = counts          # the Counts store; this class is only a transport
        self._tok = (None, 0)
        self._creds = None

    # ------------------------------------------------------------------ auth
    def _cred(self):
        if self._creds is None:
            with open(self.cred_path, encoding="utf-8") as f:
                self._creds = json.load(f)
        return self._creds

    def _token(self):
        tok, exp = self._tok
        if tok and time.time() < exp - 60:
            return tok
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        c = self._cred()
        now = int(time.time())
        claim = {"iss": c["client_email"], "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}
        signing_input = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + _b64(json.dumps(claim).encode())
        key = serialization.load_pem_private_key(c["private_key"].encode(), password=None)
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = (signing_input + b"." + _b64(sig)).decode()
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": assertion}).encode()
        r = json.loads(self._raw(TOKEN_URL, "POST", body, {"Content-Type": "application/x-www-form-urlencoded"}))
        self._tok = (r["access_token"], now + int(r.get("expires_in", 3600)))
        return self._tok[0]

    def _raw(self, url, method, body, headers):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via ourselves
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with opener.open(req, timeout=60) as resp:
            return resp.read().decode("utf-8")

    def _api(self, path, method="GET", body=None, params=None):
        url = API + "/" + self.sheet_id + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        h = {"Authorization": "Bearer " + self._token(), "Content-Type": "application/json"}
        return json.loads(self._raw(url, method, data, h) or "{}")

    # ------------------------------------------------------------------ push
    def _tabs(self):
        meta = self._api("", params={"fields": "sheets.properties.title"})
        return {s["properties"]["title"] for s in meta.get("sheets", [])}

    def _create_tab(self, tab):
        self._api(":batchUpdate", "POST", {"requests": [{"addSheet": {"properties": {
            "title": tab, "gridProperties": {"rowCount": 2000, "columnCount": MAX_DAYS + 1, "frozenRowCount": 1,
                                             "frozenColumnCount": 1}}}}]})
        header = [["URL"] + [str(d) for d in range(1, MAX_DAYS + 1)]]
        self._api("/values/" + urllib.parse.quote(a1(tab, "A1")), "PUT", {"values": header},
                  {"valueInputOption": "RAW"})

    def _sync_rows(self, tab, existing):
        if tab not in existing:
            self._create_tab(tab)
            self.counts.rows[tab] = []
            return
        got = self._api("/values/" + urllib.parse.quote(a1(tab, "A2:A"))).get("values", [])
        self.counts.rows[tab] = [r[0] for r in got if r and r[0]]

    def flush(self):
        c = self.counts
        with c.lock:
            dirty = {t: set(d) for t, d in c.dirty.items() if d}
            snapshot = {t: {u: dict(v) for u, v in c.totals.get(t, {}).items()} for t in dirty}
        if not dirty:
            return 0
        existing = self._tabs()
        pushed = 0
        for tab, days in dirty.items():
            urls = snapshot[tab]
            if tab not in c.rows or tab not in existing:
                self._sync_rows(tab, existing)
            order = c.rows.setdefault(tab, [])
            known = set(order)
            new = [u for u in urls if u not in known]
            if new:
                self._api("/values/" + urllib.parse.quote(a1(tab, "A:A")) + ":append", "POST",
                          {"values": [[u] for u in new]},
                          {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"})
                order.extend(new)
            ranges = []
            for day in sorted(days):
                col = col_name(day + 1)
                column = [[urls.get(u, {}).get(str(day), "")] for u in order]
                ranges.append({"range": a1(tab, "%s2:%s%d" % (col, col, len(order) + 1)), "values": column})
            if ranges:
                self._api("/values:batchUpdate", "POST", {"valueInputOption": "RAW", "data": ranges})
            pushed += len(urls)
            with c.lock:
                c.dirty[tab] -= days
        c.save()
        return pushed
