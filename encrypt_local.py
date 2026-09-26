#!/usr/bin/env python3
"""Encrypt local recordings into password-protected 7z archives for manual upload.

Same format as the Pi's Baidu sync: AES-256, file names encrypted, no compression.
Each archive is tested with the password after it is written.

Usage:
    python encrypt_local.py [SRC_DIR] [OUT_DIR]
Password comes from the CCTV_ZIP_PASSWORD environment variable, or is prompted for.
"""
import getpass
import os
import subprocess
import sys

SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
EXTENSIONS = (".mp4", ".mkv", ".txt", ".jsonl")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    src =sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "recordings")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "recordings_encrypted")
    password = os.environ.get("CCTV_ZIP_PASSWORD") or getpass.getpass("7z 密码: ")
    os.makedirs(out, exist_ok=True)

    names = sorted(n for n in os.listdir(src) if n.endswith(EXTENSIONS))
    done = failed = 0
    for i, name in enumerate(names, 1):
        archive = os.path.join(out, name + ".7z")
        if os.path.exists(archive):
            os.remove(archive)  # redo partial / old archives
        subprocess.run([SEVEN_ZIP, "a", "-t7z", "-mx=0", "-mhe=on", f"-p{password}",
                        "-bso0", "-bsp0", archive, os.path.join(src, name)], check=True)
        test = subprocess.run([SEVEN_ZIP, "t", f"-p{password}", "-bso0", "-bsp0", archive])
        if test.returncode == 0:
            done += 1
            print(f"[{i}/{len(names)}] OK   {name}.7z", flush=True)
        else:
            failed += 1
            print(f"[{i}/{len(names)}] FAIL {name}.7z", flush=True)
    print(f"\n完成 {done} 个，失败 {failed} 个，输出目录: {out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
