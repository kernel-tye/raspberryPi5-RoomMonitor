#!/usr/bin/env python3
"""Upload CCTV recordings to Baidu Netdisk (CCTV_REMOTE, default /房间监控) with BaiduPCS-Go.

Every file is wrapped in a password-protected 7z (AES-256, file names
encrypted, no compression) first, so Baidu never sees the footage.

Priority each run: logs -> door events -> person events (newest first)
-> continuous segments (oldest first, before free_space() deletes them)
-> old door-sensor recordings -> pre-encrypted archives dropped into INBOX
(pushed from the PC by push_to_pi.py). Stops after RUN_BUDGET seconds so the next
run (systemd timer) can re-prioritise new events.

After uploading, local files older than LOCAL_KEEP are deleted once the
remote archive is confirmed to exist with exactly the uploaded size. Baidu
already checks every block's MD5 during upload; its reported file MD5 is
not reliable, and downloading back is too slow (~48 KB/s from abroad) to verify that way.
"""
import json
import os
import re
import subprocess
import sys
import time

BPCS = os.environ.get("CCTV_BPCS") or os.path.expanduser("~/bin/BaiduPCS-Go")
BASE = os.environ.get("CCTV_HOME") or os.path.expanduser("~/cctv")
SEG_DIR = f"{BASE}/segments"
SEG_LIST = f"{BASE}/segments.csv"
EVENTS_LOG = f"{BASE}/events.jsonl"
HASHES = f"{BASE}/hashes.txt"
OLD_DIR = os.environ.get("CCTV_OLD_DIR") or os.path.expanduser("~/ai_cam_records_private")
INBOX = f"{BASE}/inbox"                     # *.7z already encrypted with the same password
LEDGER = f"{BASE}/baidu_uploaded.txt"      # path \t remote_dir \t archive size
DELETED_LOG = f"{BASE}/deleted.txt"
PASSWORD_FILE = f"{BASE}/archive_password"
TMP_DIR = f"{BASE}/.enc"
RATE_LIMIT_FILE = f"{BASE}/upload_limit"  # e.g. "500KB"; written by cctv_web.py, absent = no limit

REMOTE = os.environ.get("CCTV_REMOTE", "/房间监控")
RUN_BUDGET = 300
LOCAL_KEEP = 24 * 3600

made_dirs = set()
applied_limit = None


def bpcs(*args):
    r = subprocess.run([BPCS, *args], capture_output=True, text=True, timeout=3600)
    return r.stdout + r.stderr


def mkdir(remote_dir):
    if remote_dir not in made_dirs:
        bpcs("mkdir", remote_dir)  # errors if it already exists; harmless
        made_dirs.add(remote_dir)


def apply_rate_limit():
    """Checked before every file, so toggling it on the web page takes effect on the next one."""
    global applied_limit
    try:
        with open(RATE_LIMIT_FILE) as f:
            limit = f.read().strip() or "0"
    except FileNotFoundError:
        limit = "0"
    if limit != applied_limit:
        bpcs("config", "set", "-max_upload_rate", limit)
        applied_limit = limit
        print(f"upload rate limit: {limit if limit != '0' else 'none'}", flush=True)


def encrypt(local):
    with open(PASSWORD_FILE) as f:
        password = f.read().strip()
    os.makedirs(TMP_DIR, exist_ok=True)
    archive = os.path.join(TMP_DIR, os.path.basename(local) + ".7z")
    if os.path.exists(archive):
        os.remove(archive)
    subprocess.run(["7z", "a", "-t7z", "-mx=0", "-mhe=on", f"-p{password}", "-bso0", "-bsp0",
                    archive, local], check=True)
    return archive


def upload(local, remote_dir, policy="skip"):
    """Returns the uploaded archive size, or None on failure."""
    apply_rate_limit()
    mkdir(remote_dir)
    pre = local.startswith(INBOX + "/")
    archive = local if pre else encrypt(local)
    size = os.path.getsize(archive)
    try:
        out = bpcs("upload", "--policy", policy, archive, remote_dir)
    finally:
        if not pre:
            os.remove(archive)
    ok = "上传失败" not in out and ("成功" in out or "跳过" in out)
    print(f"{'OK  ' if ok else 'FAIL'} {local} -> {remote_dir}", flush=True)
    if not ok:
        print(out[-500:], flush=True)
    return size if ok else None


def remote_size(local, remote_dir):
    name = os.path.basename(local)
    if not local.startswith(INBOX + "/"):
        name += ".7z"
    out = bpcs("meta", f"{remote_dir}/{name}")
    m = re.search(r"文件大小\s+(\d+)", out)
    return int(m.group(1)) if m else None


def load_ledger():
    """{path: (remote_dir, size)}; size is None for entries from before sizes were logged."""
    ledger = {}
    try:
        with open(LEDGER) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if parts[0]:
                    ledger[parts[0]] = (parts[1], int(parts[2])) if len(parts) == 3 else (None, None)
    except FileNotFoundError:
        pass
    return ledger


def mark_done(path, remote_dir, size):
    with open(LEDGER, "a") as f:
        f.write(f"{path}\t{remote_dir}\t{size}\n")


def queue():
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
    events = [e for e in events if os.path.exists(e["clip"])]
    events.sort(key=lambda e: ("door" not in e["kinds"], -os.path.getmtime(e["clip"])))
    items = [(e["clip"], f"{REMOTE}/events") for e in events]

    try:
        with open(SEG_LIST) as f:
            names = sorted(line.split(",")[0].strip() for line in f if line.strip())
    except FileNotFoundError:
        names = []
    for n in names:
        p = os.path.join(SEG_DIR, os.path.basename(n))
        if os.path.exists(p):
            items.append((p, f"{REMOTE}/segments/{os.path.basename(p)[:10]}"))

    if os.path.isdir(OLD_DIR):
        for n in sorted(os.listdir(OLD_DIR)):
            if n.endswith(".mp4"):
                items.append((os.path.join(OLD_DIR, n), f"{REMOTE}/旧门磁录像"))

    if os.path.isdir(INBOX):
        for n in sorted(os.listdir(INBOX)):
            if n.endswith(".7z"):
                items.append((os.path.join(INBOX, n), f"{REMOTE}/旧门磁录像"))
    return items


def delete_verified(ledger, remote_dirs):
    """Delete local copies older than LOCAL_KEEP whose remote archive checks out.
    INBOX archives are only a staging copy, so they go as soon as the remote matches."""
    for path, (remote_dir, size) in ledger.items():
        if not os.path.exists(path):
            continue
        if not path.startswith(INBOX + "/") and time.time() - os.path.getmtime(path) < LOCAL_KEEP:
            continue
        remote_dir = remote_dir or remote_dirs.get(path)
        if remote_dir is None:
            continue
        if size is None:  # old ledger entry: 7z output size is deterministic, so rebuild it
            archive = encrypt(path)
            size = os.path.getsize(archive)
            os.remove(archive)
        got = remote_size(path, remote_dir)
        if got != size:
            print(f"KEEP {path}: remote size {got} != {size}", flush=True)
            continue
        os.remove(path)
        with open(DELETED_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\t{path}\t{remote_dir}\t{size}\n")
        print(f"DEL  {path} (verified {size} bytes on Baidu)", flush=True)


def main():
    start = time.time()
    for log in (EVENTS_LOG, HASHES):
        if os.path.exists(log):
            upload(log, f"{REMOTE}/logs", policy="overwrite")

    ledger = load_ledger()
    items = queue()
    pending = [(p, d) for p, d in items if p not in ledger]
    print(f"{len(pending)} files pending", flush=True)
    failures = 0
    for local, remote_dir in pending:
        if time.time() - start > RUN_BUDGET:
            break
        size = upload(local, remote_dir)
        if size is not None:
            mark_done(local, remote_dir, size)
        else:
            failures += 1
            if failures >= 3:
                break

    delete_verified(load_ledger(), dict(items))
    if failures >= 3:
        sys.exit("3 upload failures this run, retrying next run")


if __name__ == "__main__":
    main()
