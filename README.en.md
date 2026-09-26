# raspberryPi5-RoomMonitor

[中文](README.md)

A Raspberry Pi 5 + IMX500 AI Camera room monitor: 24/7 recording, person detection on the camera sensor, a web viewer, and encrypted uploads to Baidu Netdisk.

- Continuous recording in 5-minute MKV segments with a burned-in timestamp. A power cut only damages the last segment.
- Person detection runs on the IMX500 sensor itself, so the Pi's CPU barely takes part. Each time a person is detected, the matching stretch (30 s before they appear to 30 s after they leave) is cut from the segments into an event clip.
- Optional door sensor: a door-open MQTT message from Home Assistant also counts as an event.
- The SHA256 of every segment and clip is appended to `hashes.txt`, so you can show a file hasn't been changed later.
- Web viewer: live view, event playback and download, and continuous footage browsed by day, plus temperatures, network rate, SD card I/O, memory and under-voltage status.
- Baidu Netdisk: every file is packed into an AES-256 7z archive (file names encrypted too) before upload, so Baidu only ever sees the encrypted archive. A local file is deleted only after the upload succeeds and the archive size on Baidu matches.

## Hardware

- Raspberry Pi 5 (runs on the 1 GB model, but tightly)
- Raspberry Pi AI Camera (Sony IMX500)
- The official 27 W power supply is recommended. An undersized supply causes under-voltage and throttling, which the web page shows.

## Files

| File | Purpose | systemd |
|---|---|---|
| `cctv_recorder.py` | Recording, person detection, event clips, MJPEG preview | `cctv-recorder.service` |
| `cctv_web.py` | Web viewer | `cctv-web.service` |
| `cctv_baidu_sync.py` | Encrypts and uploads to Baidu, verifies, then deletes local files | `cctv-baidu-sync.timer` → `.service` |
| `push_to_pi.py` | (Windows) Encrypts old videos on the PC and queues them on the Pi for upload | — |
| `encrypt_local.py` | (Windows) Encrypts local videos into the same 7z format | — |
| `cctv.env.example` | Example settings | — |

## Install

These steps assume the user is `pi`. If yours isn't, change `pi` / `/home/pi` in the `.service` files to your user name and home directory.

```bash
# 1. Dependencies (Raspberry Pi OS Bookworm)
sudo apt install imx500-all python3-picamera2 python3-opencv ffmpeg p7zip-full mosquitto-clients

# 2. Scripts and data directory
cp cctv_recorder.py cctv_web.py cctv_baidu_sync.py ~/
mkdir -p ~/cctv && cp cctv.env.example ~/cctv/cctv.env   # edit as needed

# 3. Web login password (stored as a PBKDF2 hash)
python3 ~/cctv_web.py --set-password

# 4. 7z archive password (lose it and the footage on Baidu can never be opened)
printf '%s' 'your-password' > ~/cctv/archive_password && chmod 600 ~/cctv/archive_password

# 5. systemd
sudo cp cctv-*.service cctv-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cctv-recorder cctv-web cctv-baidu-sync.timer
```

If you don't need Baidu Netdisk, leave `cctv-baidu-sync.timer` disabled. The recorder then deletes the oldest continuous segments when free space drops below 4 GB. Event clips are never deleted automatically.

### Baidu Netdisk

Uploads use [BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go), placed at `~/bin/BaiduPCS-Go`. Take BDUSS and STOKEN from your browser's `pan.baidu.com` cookies:

```bash
~/bin/BaiduPCS-Go login -bduss="..." -stoken="..."
# Outside China the default pcs.baidu.com keeps dropping and restarting uploads; use this endpoint instead
~/bin/BaiduPCS-Go config set -pcs_addr c.pcs.baidu.com -fix_pcs_addr=true
```

## Configuration

All settings go in `~/cctv/cctv.env` as environment variables. Every one is optional; the defaults are in [`cctv.env.example`](cctv.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `CCTV_HOME` | `~/cctv` | Data directory |
| `CCTV_WEB_LISTEN` | `127.0.0.1:8090` | Address the web viewer binds to. Use a private address such as your Tailscale IP. **Never expose it to the internet.** |
| `CCTV_MQTT_TOPIC` | `cctv/door` | Door-sensor MQTT topic on the local mosquitto; a `START` message counts as a door opening |
| `CCTV_BPCS` | `~/bin/BaiduPCS-Go` | Path to the BaiduPCS-Go binary |
| `CCTV_REMOTE` | `/房间监控` | Netdisk folder uploads go to |
| `CCTV_OLD_DIR` | `~/ai_cam_records_private` | Optional folder of older door-sensor recordings, uploaded last |

Recording settings (resolution, frame rate, detection threshold, clip padding and so on) are constants at the top of `cctv_recorder.py`.

## How it works

### Recorder `cctv_recorder.py`

- 1280×960 at 15 fps, H.264 (the Pi 5 has no hardware encoder, so it uses libx264 at qp 27). The bitrate is very low while nobody is moving in the room and about 1.3 Mbps with activity. Continuous footage comes to about 5 GB per day.
- Person detection uses `ssd_mobilenetv2_fpnlite_320x320_pp`, COCO class person, with a score ≥ 0.55 and a person found in at least 3 of the last 5 frames.
- Event clips go to `events/*.mp4`, at most 10 minutes each, and are logged in `events.jsonl`.
- Serves a live MJPEG stream on `127.0.0.1:8000/stream.mjpg` for the web viewer and Home Assistant.
- Writes `heartbeat` every minute. If there are no frames for 30 s or ffmpeg exits, the recorder exits and systemd restarts it.

### Web viewer `cctv_web.py`

- Login: the password is stored as a PBKDF2 hash and the session cookie is HMAC-signed, valid for 7 days. 5 wrong attempts from one IP lock it out for 10 minutes.
- The Pi panel refreshes every 3 seconds: CPU and camera temperature, fan speed, network rate, SD card I/O, CPU, memory and swap, number of files waiting to upload, and under-voltage/throttling status.
- The "Baidu limit 500KB/s" checkbox writes or deletes `~/cctv/upload_limit`, which the sync reads before each file.

### Baidu sync `cctv_baidu_sync.py`

- Upload order: logs → door events → person events (newest first) → continuous segments (oldest first) → old door-sensor recordings → inbox `~/cctv/inbox/*.7z`.
- Each run lasts at most 5 minutes, and the next one starts 30 s after it ends. Priorities are recomputed every run, so new events always go up first.
- A local file is deleted only when both hold: it is at least 24 hours old, and the `.7z` size on Baidu exactly matches what was uploaded. Inbox archives are only a staging copy, so they are deleted as soon as the size matches.
  Baidu checks the MD5 of every block during upload, but the whole-file MD5 it reports isn't reliable, and downloading everything back to compare would be too slow. The same file packed with the same password always gives the same 7z size, so "upload succeeded + size matches" is the check.
- Netdisk layout: `events/`, `segments/YYYY-MM-DD/`, `logs/`, `旧门磁录像/`.
- To open an archive: 7-Zip on a computer, ZArchiver on Android, iZip on iOS.

### PC → Pi: `push_to_pi.py` (Windows)

Hands old videos on the PC to the Pi to queue for upload. For each file it encrypts it locally and tests the archive, copies it to the Pi's `inbox/*.7z.part` with `pscp`, renames it to `.7z` once the SHA256 matches on both sides, then deletes the local original. The Pi's inbox holds at most 14 GB at a time and always keeps at least 6 GB free, so the recorder never has to delete footage because of it. You can stop the script and rerun it at any time.

```powershell
$env:CCTV_PI_HOST="pi@100.x.y.z"; $env:CCTV_PI_HOSTKEY="SHA256:..."
$env:CCTV_SSH_PASSWORD="..."; $env:CCTV_ZIP_PASSWORD="..."
python push_to_pi.py <folder>
```

Needs PuTTY (`plink`, `pscp`) and 7-Zip.

## Notes

- Only one process can use the camera at a time.
- A 1 GB Pi 5 already dips into swap, so don't add memory-hungry programs.
- Only reach the web viewer over your LAN or Tailscale.
- Follow your local laws on video recording and privacy.

## License

[MIT](LICENSE)
