"""
KidNest Setup - the one-click installer.

Built into "KidNest Setup.exe" by PyInstaller (see .github/workflows/build-setup.yml).
Everything it installs travels inside the exe: mitmdump.exe, the addon, the PowerShell
installer. The person running it needs nothing but the exe and the link to their sheet.
"""
import ctypes
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import messagebox, ttk

APP = "KidNest"
INSTALL_DIR = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "KidNest")
PAYLOAD = ("install.ps1", "uninstall.ps1", "kidproxy.py", "sheetlog.py",
           "kidproxy.cmd", "mitmdump.exe", "reset-clean.ps1")
NOWINDOW = 0x08000000 if os.name == "nt" else 0


def resource_path(name):
    """Where PyInstaller unpacked our bundled files, or the repo layout when run from source."""
    base = getattr(sys, "_MEIPASS", None)
    if base and os.path.exists(os.path.join(base, name)):
        return os.path.join(base, name)
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, name), os.path.join(here, "..", "proxy", name)):
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return os.path.join(base or here, name)


# ---------------------------------------------------------------- windows helpers
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Ask for elevation once, then let the elevated copy do the work."""
    params = " ".join('"%s"' % a for a in sys.argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params if getattr(sys, "frozen", False)
        else '"%s" %s' % (os.path.abspath(__file__), params), None, 1)
    return rc > 32


def powershell(args, cwd=None):
    """Bytes, decoded as UTF-8 by us. The console default on a Vietnamese Windows mangles
    names like "Nguyen Duy Phuoc An" into nonsense."""
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
                       capture_output=True, cwd=cwd, creationflags=NOWINDOW, timeout=120)
    return (r.stdout or b"").decode("utf-8", "replace")


# The account name is what Windows uses internally and what -EnforceUsers needs. For a
# Microsoft account it is just the first few letters of the email ("nguye"), which is not
# what anyone recognises - the person's real name lives in the identity cache.
_PS_ACCOUNTS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$admins = @()
# S-1-5-32-544, not the word: built-in group names are translated on a localised Windows.
try { $admins = @(Get-LocalGroupMember -SID S-1-5-32-544 -EA Stop |
                  ForEach-Object { ($_.Name -split '\\')[-1] }) } catch { }
if (-not $admins) {
  try { $admins = @(Get-LocalGroupMember -Group Administrators -EA Stop |
                    ForEach-Object { ($_.Name -split '\\')[-1] }) } catch { }
}
Get-LocalUser | ForEach-Object {
  $sid = $_.SID.Value
  $display = $null
  $ic = "HKLM:\SOFTWARE\Microsoft\IdentityStore\Cache\$sid\IdentityCache\$sid"
  if (Test-Path $ic) {
    $p = Get-ItemProperty $ic -EA SilentlyContinue
    if ($p.DisplayName) { $display = $p.DisplayName } elseif ($p.UserName) { $display = $p.UserName }
  }
  if (-not $display -and $_.FullName) { $display = $_.FullName }
  [pscustomobject]@{
    name    = $_.Name
    display = $display
    admin   = [bool]($admins -contains $_.Name)
    enabled = [bool]$_.Enabled
  }
} | ConvertTo-Json -Compress -Depth 3
"""


def local_accounts():
    """[(name, display, is_admin, enabled)] for the real people on this PC."""
    import json as _json
    out = powershell(["-Command", _PS_ACCOUNTS]).strip()
    if not out:
        return []
    try:
        data = _json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    rows = []
    for d in data:
        nm = (d.get("name") or "").strip()
        if nm:
            rows.append((nm, (d.get("display") or "").strip(),
                         bool(d.get("admin")), bool(d.get("enabled"))))
    return rows


# ---------------------------------------------------------------- sheet
def sheet_id(value):
    """Full link, or a bare id, or an id with '/edit?usp=sharing' still stuck to it."""
    import re
    v = (value or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", v)
    if m:
        return m.group(1)
    m = re.match(r"^([A-Za-z0-9_-]{20,})(?:[/?#].*)?$", v)
    return m.group(1) if m else ""


def direct_opener():
    """Never go through the system proxy. A half-removed install leaves the machine
    pointing at 127.0.0.1:8080 with nothing listening, and every fetch dies with
    WinError 10061 - including this app's own sheet check."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def system_proxy():
    """The Windows proxy currently configured, or '' - used only to explain failures."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        with key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return ""
            return winreg.QueryValueEx(key, "ProxyServer")[0]
    except Exception:
        return ""


def fetch_csv(url):
    """Direct first, then through whatever proxy Windows has. Direct handles the usual
    case - a dead leftover proxy - while the fallback keeps this working on a network
    that genuinely requires one."""
    req = urllib.request.Request(url, headers={"User-Agent": "KidNest"})
    first = None
    for opener in (direct_opener(), urllib.request.build_opener()):
        try:
            with opener.open(req, timeout=25) as r:
                return r.read().decode("utf-8-sig", "replace")
        except Exception as e:
            first = first or e
    raise first


def csv_url(sid, tab):
    return ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=%s"
            % (sid, urllib.parse.quote(tab)))


def check_sheet(sid):
    """(ok, message). Counts what the two tabs hold, so a typo is caught before installing."""
    import csv as _csv
    import io as _io
    counts = {}
    for tab in ("websites", "youtube"):
        try:
            text = fetch_csv(csv_url(sid, tab))
        except Exception as e:
            hint = ("Hãy mở Chia sẻ trên Google Sheet và đặt "
                    "'Bất kỳ ai có đường liên kết' = Người xem.")
            if "10061" in str(e) or "refused" in str(e).lower():
                hint = ("Máy này không ra được Internet. Thường là do một bản cài cũ còn "
                        "để lại proxy trỏ vào cổng đã chết.\n"
                        "Hãy chạy reset-clean.ps1 (quyền quản trị), khởi động lại máy, rồi cài lại.")
            return False, "Không đọc được tab '%s'.\n%s\n\n%s" % (tab, e, hint)
        rows = [r for r in _csv.reader(_io.StringIO(text)) if r and r[0].strip()]
        counts[tab] = max(0, len(rows) - 1)
    if not counts["websites"]:
        return False, "Tab 'websites' đang trống. Cần ít nhất một tên miền ở cột A."
    return True, ("Đọc được: %d trang web, %d kênh YouTube."
                  % (counts["websites"], counts["youtube"]))


# ---------------------------------------------------------------- gui
class Setup(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s - Cài đặt" % APP)
        self.resizable(False, False)
        self.q = queue.Queue()
        self.accounts = []
        self.vars = {}
        self.labels = {}
        self._build()
        self.after(120, self._load_accounts)
        self.after(150, self._drain)
        self.after(300, self._warn_stale_proxy)

    def _build(self):
        pad = dict(padx=14, pady=(10, 0))
        tk.Label(self, text="KidNest", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w", **pad)
        tk.Label(self, fg="#555", justify="left",
                 text="Chỉ cho phép những trang web trong Google Sheet của bạn.\n"
                      "Cài một lần, sau đó quản lý hoàn toàn bằng Sheet.").grid(
            row=1, column=0, sticky="w", padx=14)

        box = ttk.LabelFrame(self, text=" 1. Google Sheet điều khiển ")
        box.grid(row=2, column=0, sticky="ew", **pad)
        self.sheet = tk.Entry(box, width=62)
        self.sheet.grid(row=0, column=0, padx=10, pady=10)
        self.sheet.insert(0, "")
        ttk.Button(box, text="Kiểm tra", command=self._check).grid(row=0, column=1, padx=(0, 10))
        self.sheet_msg = tk.Label(box, text="Dán đường liên kết Google Sheet vào đây.",
                                  fg="#666", anchor="w", justify="left", wraplength=470)
        self.sheet_msg.grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 10))

        box2 = ttk.LabelFrame(self, text=" 2. Áp dụng cho tài khoản nào ")
        box2.grid(row=3, column=0, sticky="ew", **pad)
        self.users = tk.Frame(box2)
        self.users.grid(row=0, column=0, sticky="w", padx=10, pady=10)
        tk.Label(box2, fg="#666", justify="left", wraplength=470,
                 text="Tài khoản không chọn sẽ dùng mạng bình thường.").grid(
            row=1, column=0, sticky="w", padx=10, pady=(0, 10))

        self.log = tk.Text(self, height=9, width=74, state="disabled",
                           bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9), bd=0)
        self.log.grid(row=4, column=0, padx=14, pady=(12, 0))

        bar = tk.Frame(self)
        bar.grid(row=5, column=0, sticky="ew", padx=14, pady=12)
        self.status = tk.Label(bar, text="", fg="#555")
        self.status.pack(side="left")
        self.btn = ttk.Button(bar, text="Cài đặt", command=self._install)
        self.btn.pack(side="right")
        if os.path.exists(INSTALL_DIR):
            ttk.Button(bar, text="Gỡ cài đặt", command=self._uninstall).pack(side="right", padx=(0, 8))

    # -------------------------------------------------- helpers
    def say(self, line):
        self.q.put(line)

    def _drain(self):
        while True:
            try:
                line = self.q.get_nowait()
            except queue.Empty:
                break
            self.log.configure(state="normal")
            self.log.insert("end", line.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(150, self._drain)

    def _load_accounts(self):
        try:
            self.accounts = [a for a in local_accounts() if a[3]]
        except Exception as e:
            self.say("Không liệt kê được tài khoản: %s" % e)
            self.accounts = []
        skip = {"defaultaccount", "guest", "wdagutilityaccount", "administrator"}
        for name, display, admin, _ in self.accounts:
            if name.lower() in skip:
                continue
            self.labels[name] = display or name
            shown = display or name
            if display and display.lower() != name.lower():
                shown = "%s   (%s)" % (display, name)     # real name, then the Windows one
            v = tk.BooleanVar(value=not admin)            # default: filter the non-admins
            self.vars[name] = v
            tk.Checkbutton(self.users, variable=v, anchor="w", width=52,
                           text="%s%s" % (shown, "   \u2014 quản trị viên" if admin else "")
                           ).pack(anchor="w")
        if not self.vars:
            tk.Label(self.users, fg="#a00",
                     text="Không tìm thấy tài khoản nào.").pack(anchor="w")

    def _warn_stale_proxy(self):
        """A leftover proxy with nothing behind it is why 'the internet is down'."""
        px = system_proxy()
        if not px:
            return
        import socket
        host, _, port = px.replace("http://", "").partition(":")
        alive = False
        try:
            with socket.create_connection((host or "127.0.0.1", int(port or 80)), timeout=1.5):
                alive = True
        except Exception:
            alive = False
        if not alive:
            self.say("CẢNH BÁO: máy đang đặt proxy %s nhưng không có gì chạy ở đó." % px)
            self.say("Đó là lý do không vào được mạng. Hãy chạy reset-clean.ps1 rồi khởi động lại.")

    def _check(self):
        sid = sheet_id(self.sheet.get())
        if not sid:
            self.sheet_msg.configure(text="Đường liên kết không hợp lệ.", fg="#a00")
            return None
        self.sheet_msg.configure(text="Đang kiểm tra...", fg="#666")
        self.update_idletasks()
        ok, msg = check_sheet(sid)
        self.sheet_msg.configure(text=msg, fg="#176b3a" if ok else "#a00")
        return sid if ok else None

    # -------------------------------------------------- install
    def _install(self):
        sid = self._check()
        if not sid:
            return
        chosen = [n for n, v in self.vars.items() if v.get()]
        if not chosen:
            messagebox.showwarning(APP, "Hãy chọn ít nhất một tài khoản để áp dụng.")
            return
        if not messagebox.askokcancel(
                APP, "Sẽ áp dụng cho:\n\n  %s\n\nTrình duyệt sẽ khởi động lại sau khi cài. Tiếp tục?"
                     % "\n  ".join(self.labels.get(n, n) for n in chosen)):
            return
        self.btn.configure(state="disabled")
        self.status.configure(text="Đang cài đặt...")
        threading.Thread(target=self._run, args=(sid, chosen), daemon=True).start()

    def _uninstall(self):
        if not messagebox.askokcancel(
                APP, "Gỡ KidNest và khôi phục mọi thiết lập của máy?\n\n"
                     "Mọi thứ sẽ được sao lưu ra Desktop kèm restore.cmd trước khi xoá."):
            return
        self.btn.configure(state="disabled")
        self.status.configure(text="Đang gỡ...")
        threading.Thread(target=self._run_uninstall, daemon=True).start()

    def _run_uninstall(self):
        script = os.path.join(INSTALL_DIR, "reset-clean.ps1")
        if not os.path.exists(script):
            script = resource_path("reset-clean.ps1")     # fall back to our own copy
        try:
            self.say("> reset-clean.ps1")
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                creationflags=NOWINDOW)
            for line in proc.stdout:
                self.say(line)
            rc = proc.wait()
        except Exception as e:
            self.say("LỖI: %s" % e)
            rc = -1
        self.after(0, self._done_uninstall, rc)

    def _done_uninstall(self, rc):
        self.btn.configure(state="normal")
        if rc == 0:
            self.status.configure(text="Đã gỡ.", fg="#176b3a")
            messagebox.showinfo(APP, "Đã gỡ KidNest và khôi phục thiết lập.\n\n"
                                     "Bản sao lưu nằm trên Desktop. Hãy khởi động lại máy.")
        else:
            self.status.configure(text="Gỡ thất bại (mã %s)." % rc, fg="#a00")

    def _run(self, sid, chosen):
        try:
            script = resource_path("install.ps1")
            missing = [f for f in PAYLOAD if not os.path.exists(resource_path(f))]
            if "install.ps1" in missing:
                raise RuntimeError("Thiếu tệp cài đặt trong gói.")
            if missing:
                self.say("(không kèm: %s - sẽ tải khi cần)" % ", ".join(missing))
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                   "-SheetId", sid, "-EnforceUsers", ",".join(chosen)]
            self.say("> install.ps1 -SheetId %s -EnforceUsers %s" % (sid, ",".join(chosen)))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, creationflags=NOWINDOW,
                                    cwd=os.path.dirname(script))
            for line in proc.stdout:
                self.say(line)
            rc = proc.wait()
        except Exception as e:
            self.say("LỖI: %s" % e)
            rc = -1
        self.after(0, self._done, rc)

    def _done(self, rc):
        self.btn.configure(state="normal")
        if rc == 0:
            self.status.configure(text="Xong.", fg="#176b3a")
            self.btn.configure(text="Đóng", command=self.destroy)
            messagebox.showinfo(
                APP, "Đã cài xong.\n\nHãy khởi động lại trình duyệt.\n\n"
                     "Xem nhật ký: http://kidnest.local/log\n"
                     "Cập nhật danh sách ngay: gõ  kidnest update")
        else:
            self.status.configure(text="Thất bại (mã %s)." % rc, fg="#a00")
            messagebox.showerror(APP, "Cài đặt không thành công. Xem chi tiết trong khung đen.")


def main():
    if os.name != "nt":
        print("KidNest Setup runs on Windows.")
        return 1
    if not is_admin():
        if relaunch_as_admin():
            return 0
        ctypes.windll.user32.MessageBoxW(
            None, "KidNest cần quyền quản trị viên để cài đặt.", APP, 0x10)
        return 1
    Setup().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
