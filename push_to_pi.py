#!/usr/bin/env python3
"""Encrypt old recordings on the PC and hand them to the Pi's Baidu sync queue.

For each file in SRC_DIR (oldest name first):
  1. 7z it (same format/password as the Pi) and test the archive
  2. copy it to the Pi as ~/cctv/inbox/<name>.7z.part, compare SHA256, rename to .7z
  3. delete the local original and the temporary archive

cctv_baidu_sync.py then uploads inbox/*.7z as-is and deletes each one once
Baidu reports the same size. The inbox is kept under INBOX_LIMIT so the
recorder's free_space() never has to delete footage to make room.

Usage:
    python push_to_pi.py SRC_DIR
Environment:
    CCTV_PI_HOST       user@host of the Pi
    CCTV_PI_HOSTKEY    its SSH host key fingerprint (SHA256:...), checked by plink
    CCTV_SSH_PASSWORD  Pi login password
    CCTV_ZIP_PASSWORD  7z password (must match ~/cctv/archive_password on the Pi)
"""
import hashlib
import os
import subprocess
import sys
import time

SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"
PLINK = r"C:\Program Files\PuTTY\plink.exe"
PSCP = r"C:\Program Files\PuTTY\pscp.exe"
HOST = os.environ["CCTV_PI_HOST"]
HOSTKEY = os.environ["CCTV_PI_HOSTKEY"]
INBOX = "cctv/inbox"                   # relative to the Pi user's home
LEDGER = "cctv/baidu_uploaded.txt"
INBOX_LIMIT = 14 * 1024**3
MIN_FREE = 6 * 1024**3  # recorder starts deleting segments below 4 GB
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, ".push_tmp")


def ssh(cmd):
    r = subprocess.run([PLINK, "-ssh", "-batch", "-hostkey", HOSTKEY, "-pw", SSH_PW, HOST, cmd],
                       capture_output=True, text=True, encoding="utf-8", timeout=600)
    if r.returncode:
        raise RuntimeError(f"ssh failed: {cmd}\n{r.stderr}")
    return r.stdout.strip()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def wait_for_room(size):
    while True:
        used, free = map(int, ssh(f"du -sb {INBOX} | cut -f1; df -B1 --output=avail {INBOX} | tail -1").split())
        if used + size <= INBOX_LIMIT and free - size >= MIN_FREE:
            return
        print(f"  Pi inbox {used / 1024**3:.1f} GB, free {free / 1024**3:.1f} GB - waiting", flush=True)
        time.sleep(600)


def already_on_pi(archive_name):
    return ssh(f"test -e {INBOX}/'{archive_name}' || grep -qF '{INBOX}/{archive_name}' {LEDGER}"
               f" && echo yes || echo no") == "yes"


def push(src, name):
    archive_name = name + ".7z"
    if already_on_pi(archive_name):
        os.remove(src)
        return "SKIP (already on Pi)"
    archive = os.path.join(TMP, archive_name)
    if os.path.exists(archive):
        os.remove(archive)
    subprocess.run([SEVEN_ZIP, "a", "-t7z", "-mx=0", "-mhe=on", f"-p{ZIP_PW}", "-bso0", "-bsp0",
                    archive, src], check=True)
    subprocess.run([SEVEN_ZIP, "t", f"-p{ZIP_PW}", "-bso0", "-bsp0", archive], check=True)
    size = os.path.getsize(archive)
    digest = sha256(archive)

    wait_for_room(size)
    part = f"{INBOX}/{archive_name}.part"
    subprocess.run([PSCP, "-batch", "-q", "-hostkey", HOSTKEY, "-pw", SSH_PW, archive, f"{HOST}:{part}"],
                   check=True)
    got = ssh(f"sha256sum '{part}' | cut -d' ' -f1")
    if got != digest:
        ssh(f"rm -f '{part}'")
        raise RuntimeError(f"{name}: SHA256 mismatch after copy ({got} != {digest})")
    ssh(f"mv '{part}' '{INBOX}/{archive_name}'")
    os.remove(archive)
    os.remove(src)
    return f"OK   {size / 1024**2:.0f} MB"


def main():
    global ZIP_PW, SSH_PW
    sys.stdout.reconfigure(encoding="utf-8")
    src_dir = sys.argv[1]
    ZIP_PW = os.environ["CCTV_ZIP_PASSWORD"]
    SSH_PW = os.environ["CCTV_SSH_PASSWORD"]
    os.makedirs(TMP, exist_ok=True)
    ssh(f"mkdir -p {INBOX}")

    names = sorted(os.listdir(src_dir))
    for i, name in enumerate(names, 1):
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        print(f"[{i}/{len(names)}] {name}: {push(src, name)}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
