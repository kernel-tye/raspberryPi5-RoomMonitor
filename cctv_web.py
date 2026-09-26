#!/usr/bin/env python3
"""Password-protected web viewer for cctv_recorder.py.

Live view, event clips, continuous segments and recorder status. Listens on the
Tailscale address only, so nobody on the house Wi-Fi can reach it.

Set / change the password:  python3 cctv_web.py --set-password
"""
import getpass
import glob
import hashlib
import hmac
import html
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

BASE = os.environ.get("CCTV_HOME") or os.path.expanduser("~/cctv")
SEG_DIR = f"{BASE}/segments"
EVENT_DIR = f"{BASE}/events"
EVENTS_LOG = f"{BASE}/events.jsonl"
HEARTBEAT = f"{BASE}/heartbeat"
SEG_LIST = f"{BASE}/segments.csv"
SENSOR_TEMP = f"{BASE}/sensor_temp"  # written every 2 s by cctv_recorder.py
SD_DEVICE = "mmcblk0"
OLD_DIR = os.environ.get("CCTV_OLD_DIR") or os.path.expanduser("~/ai_cam_records_private")
LEDGER = f"{BASE}/baidu_uploaded.txt"
RATE_LIMIT_FILE = f"{BASE}/upload_limit"  # read by cctv_baidu_sync.py before each file
RATE_LIMIT = "500KB"
PASSWORD_FILE = f"{BASE}/web_password"
SECRET_FILE = f"{BASE}/web_secret"

# Bind to a private address only (e.g. the Tailscale IP); never expose this to the internet.
_host, _port = os.environ.get("CCTV_WEB_LISTEN", "127.0.0.1:8090").rsplit(":", 1)
LISTEN = (_host, int(_port))
LIVE_URL = "http://127.0.0.1:8000/stream.mjpg"
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILS = 5
LOCKOUT_SECONDS = 600

MEDIA_DIRS = {"events": (EVENT_DIR, ".mp4"), "segments": (SEG_DIR, ".mkv")}
CONTENT_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska"}

failures = {}  # ip -> [timestamps]


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 300_000).hex()
    return f"{salt}${digest}"


def check_password(password):
    try:
        with open(PASSWORD_FILE) as f:
            stored = f.read().strip()
    except FileNotFoundError:
        return False
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def load_secret():
    if not os.path.exists(SECRET_FILE):
        write_private(SECRET_FILE, secrets.token_hex(32))
    with open(SECRET_FILE) as f:
        return bytes.fromhex(f.read().strip())


SECRET = None


def make_token():
    expires = str(int(time.time()) + SESSION_SECONDS)
    sig = hmac.new(SECRET, expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def token_valid(token):
    try:
        expires, sig = token.split(".", 1)
    except ValueError:
        return False
    good = hmac.new(SECRET, expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and int(expires) > time.time()


def locked_out(ip):
    recent = [t for t in failures.get(ip, []) if time.time() - t < LOCKOUT_SECONDS]
    failures[ip] = recent
    return len(recent) >= MAX_FAILS


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def recorder_status():
    active = subprocess.run(["systemctl", "is-active", "cctv-recorder"],
                            capture_output=True, text=True).stdout.strip()
    try:
        beat_age = time.time() - os.path.getmtime(HEARTBEAT)
    except FileNotFoundError:
        beat_age = None
    disk = shutil.disk_usage(BASE)
    segs = sorted(n for n in os.listdir(SEG_DIR) if n.endswith(".mkv"))
    return {
        "active": active,
        "healthy": active == "active" and beat_age is not None and beat_age < 180,
        "beat_age": beat_age,
        "free": disk.free,
        "total": disk.total,
        "oldest": segs[0][:16].replace("_", " ") if segs else "—",
        "segments": len(segs),
    }


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def counters():
    """(time, cpu busy, cpu total, bytes received, bytes sent, SD bytes read, SD bytes written).

    Network counts physical interfaces only. Tunnels (tailscale0, wg0) and docker are left out;
    their traffic already goes through wlan0/eth0.
    """
    cpu = [int(x) for x in read("/proc/stat").split("\n", 1)[0].split()[1:]]
    idle = cpu[3] + cpu[4]
    rx = tx = 0
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, _, data = line.partition(":")
        if not name.strip().startswith(("eth", "wlan", "en", "wl")):
            continue
        fields = data.split()
        rx += int(fields[0])
        tx += int(fields[8])
    rd = wr = 0
    for line in read("/proc/diskstats").splitlines():
        fields = line.split()
        if fields[2] == SD_DEVICE:
            rd, wr = int(fields[5]) * 512, int(fields[9]) * 512  # sectors are always 512 B here
            break
    return time.time(), sum(cpu) - idle, sum(cpu), rx, tx, rd, wr


last_sample = None
sample_lock = threading.Lock()

THROTTLE_BITS = {0: "欠压", 1: "降频", 2: "限速", 3: "温度软限制"}


def throttle_status():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout
        flags = int(out.strip().split("=")[1], 16)
    except (OSError, IndexError, ValueError):
        return None, False
    now = [label for bit, label in THROTTLE_BITS.items() if flags & (1 << bit)]
    past = [label for bit, label in THROTTLE_BITS.items() if flags & (1 << (bit + 16))]
    if now:
        return "现在" + "、".join(now), True
    if past:
        return "开机后出现过" + "、".join(past), True
    return "正常", False


def baidu_status():
    uploaded = {line.split("\t", 1)[0] for line in read(LEDGER).splitlines()}
    files = [os.path.join(EVENT_DIR, n) for n in os.listdir(EVENT_DIR) if n.endswith(".mp4")]
    files += [os.path.join(SEG_DIR, os.path.basename(line.split(",")[0].strip()))
              for line in read(SEG_LIST).splitlines() if line.strip()]
    if os.path.isdir(OLD_DIR):
        files += [os.path.join(OLD_DIR, n) for n in os.listdir(OLD_DIR) if n.endswith(".mp4")]
    pending = [p for p in files if p not in uploaded and os.path.exists(p)]
    try:
        last = time.time() - os.path.getmtime(LEDGER)
    except OSError:
        last = None
    running = subprocess.run(["systemctl", "is-active", "cctv-baidu-sync.service"],
                             capture_output=True, text=True).stdout.strip() == "activating"
    return {"limited": os.path.exists(RATE_LIMIT_FILE), "pending": len(pending), "pending_bytes": sum(os.path.getsize(p) for p in pending),
            "last_upload_age": last, "running": running}


def sensor_temp():
    """IMX500 temperature, or None if the recorder hasn't updated it recently."""
    try:
        if time.time() - os.path.getmtime(SENSOR_TEMP) > 30:
            return None
        return float(read(SENSOR_TEMP).strip())
    except (OSError, ValueError):
        return None


def system_status():
    global last_sample
    with sample_lock:
        prev = last_sample
        if prev is None or time.time() - prev[0] > 30:
            prev = counters()
            time.sleep(1)
        cur = last_sample = counters()
    dt = cur[0] - prev[0]
    cpu_total = cur[2] - prev[2]

    mem = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        mem[key] = int(value.split()[0]) * 1024
    temp = read("/sys/class/thermal/thermal_zone0/temp").strip()
    fan = next((read(f).strip() for f in glob.glob("/sys/devices/platform/cooling_fan/hwmon/*/fan1_input")), "")
    throttle, throttle_bad = throttle_status()
    return {
        "temp": int(temp) / 1000 if temp else None,
        "sensor_temp": sensor_temp(),
        "fan_rpm": int(fan) if fan else None,
        "cpu": 100 * (cur[1] - prev[1]) / cpu_total if cpu_total else 0,
        "load": read("/proc/loadavg").split()[:3],
        "mem_used": mem["MemTotal"] - mem["MemAvailable"],
        "mem_total": mem["MemTotal"],
        "swap_used": mem["SwapTotal"] - mem["SwapFree"],
        "swap_total": mem["SwapTotal"],
        "up_bps": (cur[4] - prev[4]) / dt,
        "down_bps": (cur[3] - prev[3]) / dt,
        "sd_read_bps": (cur[5] - prev[5]) / dt,
        "sd_write_bps": (cur[6] - prev[6]) / dt,
        "uptime": float(read("/proc/uptime", "0").split()[0]),
        "throttle": throttle,
        "throttle_bad": throttle_bad,
        "baidu": baidu_status(),
    }


def load_events():
    events = []
    try:
        with open(EVENTS_LOG) as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return [e for e in reversed(events) if os.path.exists(e.get("clip", ""))]


def list_segments():
    segs = sorted((n for n in os.listdir(SEG_DIR) if n.endswith(".mkv")), reverse=True)
    by_day = {}
    for n in segs:
        by_day.setdefault(n[:10], []).append(n)
    return by_day


KIND_LABELS = {"person": "有人", "door": "开门"}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#667085;--line:#e4e7ec;--accent:#2563eb;
--ok:#16a34a;--bad:#dc2626;--warn:#d97706;--chip:#eef2ff;--chip-text:#3730a3;--door:#fff7ed;--door-text:#9a3412}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e6e8eb;--muted:#9aa4b2;
--line:#2a2f3a;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--chip:#1e2447;--chip-text:#c7d2fe;
--door:#3b2414;--door-text:#fdba74}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;
border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
header h1{font-size:17px;margin:0}header a{color:var(--muted);text-decoration:none;font-size:14px}
main{max-width:960px;margin:0 auto;padding:16px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px}
.live{width:100%;border-radius:8px;background:#000;aspect-ratio:4/3;object-fit:contain;display:block}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat{border:1px solid var(--line);border-radius:8px;padding:10px}
.stat b{display:block;font-size:17px}.stat span{color:var(--muted);font-size:13px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.event{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:10px 0;
border-top:1px solid var(--line);flex-wrap:wrap}.event:first-of-type{border-top:0}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:var(--chip);
color:var(--chip-text);margin-right:4px}.chip.door{background:var(--door);color:var(--door-text)}
.muted{color:var(--muted);font-size:13px}.hash{font-family:ui-monospace,monospace;font-size:11px;
color:var(--muted);word-break:break-all}
a.btn,button{display:inline-block;padding:6px 12px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--text);text-decoration:none;font-size:14px;cursor:pointer}
a.btn.primary,button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
video{width:100%;border-radius:8px;background:#000;margin-top:8px}
details{border-top:1px solid var(--line);padding:8px 0}details:first-of-type{border-top:0}
summary{cursor:pointer;font-weight:600}.segs{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.login{max-width:340px;margin:12vh auto;padding:0 16px}
.login input{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--text);font-size:16px;margin:8px 0 12px}
.login button{width:100%;padding:10px}.err{color:var(--bad);font-size:14px}
"""


SYS_JS = """
const $ = id => document.getElementById(id);
function size(n) {
  for (const u of ["B", "KB", "MB", "GB"]) {
    if (n < 1024) return (u === "B" ? n.toFixed(0) : n.toFixed(1)) + " " + u;
    n /= 1024;
  }
  return n.toFixed(1) + " TB";
}
function rate(bps) { return size(bps) + "/s"; }
function ago(s) {
  if (s == null) return "从未";
  if (s < 60) return Math.round(s) + " 秒";
  if (s < 3600) return Math.round(s / 60) + " 分钟";
  if (s < 86400) return (s / 3600).toFixed(1) + " 小时";
  return (s / 86400).toFixed(1) + " 天";
}
function set(id, text, cls) { const el = $(id); el.textContent = text; el.className = cls || ""; }
async function refresh() {
  if (document.hidden) return;
  let d;
  try { d = await (await fetch("/api/system", {cache: "no-store"})).json(); } catch (e) { return; }
  set("s-temp", d.temp == null ? "—" : d.temp.toFixed(1) + " °C",
      d.temp >= 80 ? "bad" : d.temp >= 70 ? "warn" : "");
  set("s-cam-temp", d.sensor_temp == null ? "—" : d.sensor_temp.toFixed(1) + " °C",
      d.sensor_temp >= 75 ? "bad" : d.sensor_temp >= 65 ? "warn" : "");
  set("s-fan", "CPU 温度" + (d.fan_rpm == null ? "" : " · 风扇 " + d.fan_rpm + " rpm"));
  set("s-up", "↑ " + rate(d.up_bps));
  set("s-down", "上传 · 下载 ↓ " + rate(d.down_bps));
  set("s-sd-write", "W " + rate(d.sd_write_bps));
  set("s-sd-read", "TF 卡写入 · 读取 R " + rate(d.sd_read_bps));
  set("s-cpu", d.cpu.toFixed(0) + "%", d.cpu >= 90 ? "warn" : "");
  set("s-load", "CPU · 负载 " + d.load.join(" "));
  set("s-mem", size(d.mem_used) + " / " + size(d.mem_total), d.mem_used / d.mem_total > 0.9 ? "warn" : "");
  set("s-swap", "内存 · swap " + size(d.swap_used) + " / " + size(d.swap_total));
  const b = d.baidu;
  $("s-limit").checked = b.limited;
  set("s-baidu", b.pending + " 个 · " + size(b.pending_bytes));
  set("s-baidu-sub", "百度待上传 · " + (b.running ? "上传中" : "空闲") +
      " · 上次 " + (b.last_upload_age == null ? "从未" : ago(b.last_upload_age) + "前"));
  set("s-throttle", d.throttle || "—", d.throttle_bad ? "bad" : "");
  set("s-uptime", "供电 / 降频 · 已运行 " + ago(d.uptime));
}
refresh();
setInterval(refresh, 3000);
document.addEventListener("visibilitychange", refresh);
"""


def page(title, body, header=True):
    top = ('<header><h1>房间监控</h1><a href="/logout">退出</a></header>' if header else "")
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body>{top}{body}</body></html>')


def login_page(error=""):
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return page("登录", f"""<div class="login"><h1>房间监控</h1>{err}
<form method="post" action="/login"><label>密码<input type="password" name="password"
autofocus autocomplete="current-password"></label><button class="primary">登录</button></form></div>""",
                header=False)


def dashboard():
    st = recorder_status()
    state = ('<b class="ok">录像中</b>' if st["healthy"] else f'<b class="bad">异常（{st["active"]}）</b>')
    beat = "—" if st["beat_age"] is None else f'{st["beat_age"]:.0f} 秒前'
    events = load_events()
    rows = []
    for e in events[:200]:
        name = os.path.basename(e["clip"])
        chips = "".join(f'<span class="chip {k}">{KIND_LABELS.get(k, k)}</span>' for k in e["kinds"])
        start, end = e["start"][:19].replace("T", " "), e["end"][11:19]
        url = f"/media/events/{quote(name)}"
        rows.append(f"""<div class="event"><div>{chips}<b>{start}</b> – {end}
<div class="hash">SHA256 {e["sha256"]}</div></div>
<div><a class="btn primary" href="/watch/events/{quote(name)}">播放</a>
<a class="btn" href="{url}" download>下载</a></div></div>""")
    events_html = "".join(rows) or '<p class="muted">还没有事件。</p>'

    days = []
    for day, names in list_segments().items():
        links = "".join(f'<a class="btn" href="/watch/segments/{quote(n)}">{n[11:16].replace("-", ":")}</a>'
                        for n in names)
        days.append(f'<details><summary>{day}（{len(names)} 段）</summary><div class="segs">{links}</div></details>')
    segs_html = "".join(days) or '<p class="muted">暂无。</p>'

    return page("房间监控", f"""<main>
<section><h2>实时画面</h2><img class="live" src="/live" alt="实时画面"></section>
<section><h2>状态</h2><div class="stats">
<div class="stat">{state}<span>录像服务</span></div>
<div class="stat"><b>{beat}</b><span>最近心跳</span></div>
<div class="stat"><b>{human_size(st["free"])}</b><span>剩余空间 / {human_size(st["total"])}</span></div>
<div class="stat"><b>{st["oldest"]}</b><span>最早连续录像（{st["segments"]} 段）</span></div>
</div></section>
<section><h2>树莓派</h2><div class="stats">
<div class="stat"><b id="s-temp">—</b><span id="s-fan">CPU 温度</span></div>
<div class="stat"><b id="s-cam-temp">—</b><span>摄像头温度</span></div>
<div class="stat"><b id="s-up">—</b><span id="s-down">上传</span>
<form method="post" action="/upload-limit"><label class="muted"><input type="checkbox" name="on" id="s-limit"
onchange="this.form.submit()"{" checked" if os.path.exists(RATE_LIMIT_FILE) else ""}> 百度限速 {RATE_LIMIT}/s</label></form></div>
<div class="stat"><b id="s-sd-write">—</b><span id="s-sd-read">TF 卡读写</span></div>
<div class="stat"><b id="s-cpu">—</b><span id="s-load">CPU</span></div>
<div class="stat"><b id="s-mem">—</b><span id="s-swap">内存</span></div>
<div class="stat"><b id="s-baidu">—</b><span id="s-baidu-sub">百度待上传</span></div>
<div class="stat"><b id="s-throttle">—</b><span id="s-uptime">供电 / 降频</span></div>
</div></section>
<section><h2>事件（有人 / 开门）</h2>{events_html}</section>
<section><h2>连续录像（每段 5 分钟）</h2>{segs_html}</section>
</main><script>{SYS_JS}</script>""")


def watch_page(kind, name):
    url = f"/media/{kind}/{quote(name)}"
    note = ("" if kind == "events" else
            '<p class="muted">MKV 在部分浏览器里无法直接播放，可以下载后用 VLC 打开。</p>')
    return page(name, f"""<main><section><a class="btn" href="/">← 返回</a>
<h2 style="margin-top:12px">{html.escape(name)}</h2>
<video controls autoplay playsinline src="{url}"></video>{note}
<p><a class="btn" href="{url}" download>下载</a></p></section></main>""")


class Handler(BaseHTTPRequestHandler):
    server_version = "cctv"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stdout.write(f"{self.client_address[0]} {fmt % args}\n")

    def authed(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return "session" in cookie and token_valid(cookie["session"].value)

    def send_html(self, body, code=200, headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self):
        if self.path == "/upload-limit":
            if not self.authed():
                self.redirect("/login")
                return
            length = min(int(self.headers.get("Content-Length", 0)), 4096)
            if parse_qs(self.rfile.read(length).decode(errors="replace")).get("on"):
                write_private(RATE_LIMIT_FILE, RATE_LIMIT)
            elif os.path.exists(RATE_LIMIT_FILE):
                os.remove(RATE_LIMIT_FILE)
            self.redirect("/")
            return
        if self.path != "/login":
            self.send_error(404)
            return
        ip = self.client_address[0]
        if locked_out(ip):
            self.send_html(login_page("尝试次数太多，请 10 分钟后再试。"), 429)
            return
        length = min(int(self.headers.get("Content-Length", 0)), 4096)
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        if check_password(form.get("password", [""])[0]):
            failures.pop(ip, None)
            cookie = (f"session={make_token()}; Max-Age={SESSION_SECONDS}; Path=/; "
                      f"HttpOnly; SameSite=Strict")
            self.redirect("/", {"Set-Cookie": cookie})
        else:
            failures.setdefault(ip, []).append(time.time())
            time.sleep(1)
            self.send_html(login_page("密码不对。"), 401)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            self.send_html(login_page())
            return
        if path == "/logout":
            self.redirect("/login", {"Set-Cookie": "session=; Max-Age=0; Path=/"})
            return
        if not self.authed():
            self.redirect("/login")
            return
        if path == "/":
            self.send_html(dashboard())
        elif path == "/api/system":
            data = json.dumps(system_status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/live":
            self.proxy_live()
        elif path.startswith("/watch/") or path.startswith("/media/"):
            parts = path.split("/")
            if len(parts) != 4 or parts[2] not in MEDIA_DIRS:
                self.send_error(404)
                return
            kind, name = parts[2], os.path.basename(urllib.request.unquote(parts[3]))
            folder, ext = MEDIA_DIRS[kind]
            full = os.path.join(folder, name)
            if not name.endswith(ext) or not os.path.isfile(full):
                self.send_error(404)
            elif parts[1] == "watch":
                self.send_html(watch_page(kind, name))
            else:
                self.send_file(full, CONTENT_TYPES[ext])
        else:
            self.send_error(404)

    def proxy_live(self):
        try:
            upstream = urllib.request.urlopen(LIVE_URL, timeout=10)
        except OSError:
            self.send_error(503, "camera stream unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", upstream.headers.get("Content-Type"))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while chunk := upstream.read(64 * 1024):
                self.wfile.write(chunk)
        except OSError:
            pass
        finally:
            upstream.close()

    def send_file(self, full, ctype):
        size = os.path.getsize(full)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start, end = int(a), int(b) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := f.read(min(256 * 1024, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            pass


def main():
    global SECRET
    os.makedirs(BASE, exist_ok=True)
    if "--set-password" in sys.argv:
        pw = getpass.getpass("新密码: ")
        if len(pw) < 8 or pw != getpass.getpass("再输一次: "):
            sys.exit("两次不一致，或少于 8 位")
        write_private(PASSWORD_FILE, hash_password(pw))
        print("已设置")
        return
    if not os.path.exists(PASSWORD_FILE):
        sys.exit(f"先运行 {sys.argv[0]} --set-password")
    SECRET = load_secret()
    server = ThreadingHTTPServer(LISTEN, Handler)
    server.daemon_threads = True
    print(f"listening on http://{LISTEN[0]}:{LISTEN[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
