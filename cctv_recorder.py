#!/usr/bin/env python3
"""24/7 CCTV recorder for the IMX500 AI camera (Raspberry Pi 5).

- Records continuously into 5-minute MKV segments with a burned-in timestamp
  (MKV stays readable if power is cut mid-segment).
- Runs person detection on the IMX500 sensor. Each period with a person in view
  is cut from the segments (PRE_ROLL before .. POST_ROLL after) into events/.
- Door-open MQTT messages (CCTV_MQTT_TOPIC, payload START, e.g. from Home Assistant) are
  events too.
- Serves the MJPEG preview on 127.0.0.1:8000/stream.mjpg for Home Assistant.
- Appends the SHA256 of every closed segment and every clip to hashes.txt.
- Deletes the oldest continuous segments when free space runs low. Event clips
  are never deleted automatically.
"""
import collections
import hashlib
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
from picamera2 import MappedArray, Picamera2
from picamera2.devices import IMX500
from picamera2.encoders import LibavH264Encoder, MJPEGEncoder
from picamera2.outputs import FfmpegOutput, FileOutput

BASE = os.environ.get("CCTV_HOME") or os.path.expanduser("~/cctv")
SEG_DIR = f"{BASE}/segments"
EVENT_DIR = f"{BASE}/events"
SEG_LIST = f"{BASE}/segments.csv"
HASHES = f"{BASE}/hashes.txt"
EVENTS_LOG = f"{BASE}/events.jsonl"
HEARTBEAT = f"{BASE}/heartbeat"
SENSOR_TEMP = f"{BASE}/sensor_temp"  # IMX500 temperature in °C, read by cctv_web.py

MODEL = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"
PERSON_CLASS = 0          # COCO "person"
SCORE_THRESHOLD = 0.55
HITS_NEEDED = 3           # person in >= 3 of the last 5 inferences
HITS_WINDOW = 5

MAIN_SIZE = (1280, 960)   # sensor is 4:3, keep full field of view
LORES_SIZE = (640, 480)
FPS = 15
QP = 27                   # constant quantiser: a still room costs almost nothing
SEGMENT_SECONDS = 300

PRE_ROLL = 30
POST_ROLL = 30
DOOR_HOLD = 60            # a door-open keeps the event alive this long
MAX_EVENT = 600           # split long events (e.g. someone sitting at the desk)
MIN_FREE_BYTES = 4 * 1024**3
KEEP_RECENT_SEGMENTS = 12  # never delete the newest hour

MQTT_TOPIC = os.environ.get("CCTV_MQTT_TOPIC", "cctv/door")
MJPEG_ADDR = ("127.0.0.1", 8000)

log = logging.getLogger("cctv")


def seg_start_time(name):
    return time.mktime(time.strptime(os.path.basename(name)[:19], "%Y-%m-%d_%H-%M-%S"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record_hash(path):
    digest = sha256_file(path)
    with open(HASHES, "a") as f:
        f.write(f"{digest}  {path}  hashed={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
    return digest


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def all_segments():
    """Every segment on disk, oldest first; the last one is still being written."""
    return sorted(os.path.join(SEG_DIR, n) for n in os.listdir(SEG_DIR) if n.endswith(".mkv"))


def closed_segments():
    """Segments ffmpeg has finished writing (listed in segments.csv), oldest first."""
    try:
        with open(SEG_LIST) as f:
            names = [line.split(",")[0].strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []
    paths = [os.path.join(SEG_DIR, os.path.basename(n)) for n in names]
    return sorted(p for p in paths if os.path.exists(p))


def segment_spans():
    """(path, start, end) for closed segments; end is the next segment's start."""
    closed = set(closed_segments())
    segs = all_segments()
    return [(p, seg_start_time(p), seg_start_time(n)) for p, n in zip(segs, segs[1:]) if p in closed]


class EventManager:
    """Merges person / door triggers into events and cuts a clip for each one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current = None
        self.pending = []

    def trigger(self, kind, t, hold=0.0, score=None):
        with self.lock:
            cur = self.current
            if cur and (t - cur["last"] > POST_ROLL or t - cur["start"] > MAX_EVENT):
                self._finish()
                cur = None
            if cur is None:
                cur = self.current = {"start": t, "last": t, "kinds": set(), "max_score": 0.0}
                log.info("event start (%s)", kind)
            cur["last"] = max(cur["last"], t + hold)
            cur["kinds"].add(kind)
            if score is not None:
                cur["max_score"] = max(cur["max_score"], score)

    def tick(self):
        with self.lock:
            if self.current and time.time() - self.current["last"] > POST_ROLL:
                self._finish()
            ready = [e for e in self.pending if self._segments_ready(e)]
            for e in ready:
                self.pending.remove(e)
        for e in ready:
            try:
                self._cut(e)
            except Exception:
                log.exception("cutting clip failed")

    def protected_after(self):
        """Segments starting after this time may still be needed for a clip."""
        with self.lock:
            starts = [e["start"] for e in self.pending]
            if self.current:
                starts.append(self.current["start"])
        return min(starts) - PRE_ROLL - SEGMENT_SECONDS if starts else None

    def _finish(self):
        e = self.current
        self.current = None
        e["from"] = e["start"] - PRE_ROLL
        e["to"] = e["last"] + POST_ROLL
        log.info("event end: %.0fs %s", e["last"] - e["start"], sorted(e["kinds"]))
        self.pending.append(e)

    @staticmethod
    def _segments_ready(e):
        # ffmpeg closes a segment before opening the next, so once a segment
        # starts after the clip's end, everything the clip needs is on disk.
        return any(seg_start_time(p) > e["to"] for p in all_segments())

    def _cut(self, e):
        spans = [(p, s) for p, s, end in segment_spans() if s < e["to"] and end > e["from"]]
        if not spans:
            log.warning("no segments for event %s", e)
            return
        label = "+".join(sorted(e["kinds"]))
        name = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(e["start"])) + f"_{label}.mp4"
        out = os.path.join(EVENT_DIR, name)
        listing = out + ".txt"
        with open(listing, "w") as f:
            for i, (p, s) in enumerate(spans):
                f.write(f"file '{p}'\n")
                if i == 0 and e["from"] > s:
                    f.write(f"inpoint {e['from'] - s:.2f}\n")
                if i == len(spans) - 1:
                    f.write(f"outpoint {e['to'] - s:.2f}\n")
        segs = [p for p, _ in spans]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", listing, "-c", "copy", "-movflags", "+faststart", out], check=True)
        os.remove(listing)
        digest = record_hash(out)
        append_jsonl(EVENTS_LOG, {
            "clip": out, "sha256": digest, "kinds": sorted(e["kinds"]),
            "start": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(e["start"])),
            "end": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(e["last"])),
            "max_person_score": round(e["max_score"], 3),
            "segments": [os.path.basename(p) for p in segs],
        })
        log.info("clip saved: %s", out)


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        super().__init__()
        self.frame = None
        self.condition = threading.Condition()

    def writable(self):
        return True

    def write(self, buf):
        with self.condition:
            self.frame = bytes(buf)
            self.condition.notify_all()
        return len(buf)


preview = StreamingOutput()


class StreamingHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(301)
            self.send_header("Location", "/stream.mjpg")
            self.end_headers()
            return
        if self.path != "/stream.mjpg":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        try:
            while True:
                with preview.condition:
                    preview.condition.wait()
                    frame = preview.frame
                self.wfile.write(b"--FRAME\r\n")
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except Exception:
            pass


class Detector:
    def __init__(self, imx500, events):
        self.imx500 = imx500
        self.events = events
        self.hits = collections.deque(maxlen=HITS_WINDOW)
        self.last_frame = time.time()
        self.person = False
        self.sensor_temp = None

    def __call__(self, request):
        now = time.time()
        self.last_frame = now
        metadata = request.get_metadata()
        self.sensor_temp = metadata.get("SensorTemperature")
        self._detect(metadata, now)
        self._overlay(request, now)

    def _detect(self, metadata, now):
        outputs = self.imx500.get_outputs(metadata, add_batch=True)
        if outputs is None:
            return
        scores, classes = outputs[1][0], outputs[2][0]
        best = max((float(s) for s, c in zip(scores, classes)
                    if int(c) == PERSON_CLASS and s >= SCORE_THRESHOLD), default=None)
        self.hits.append(best is not None)
        self.person = sum(self.hits) >= HITS_NEEDED
        if self.person:
            self.events.trigger("person", now, score=best)

    def _overlay(self, request, now):
        text = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now))
        if self.person:
            text += "  PERSON"
        for stream, scale, y in (("main", 1.0, 40), ("lores", 0.6, 24)):
            with MappedArray(request, stream) as m:
                # YUV420: draw on the luma plane only (black outline, white text)
                cv2.putText(m.array, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, 0, 5)
                cv2.putText(m.array, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, 255, 2)


def mqtt_loop(events):
    while True:
        proc = subprocess.Popen(["mosquitto_sub", "-h", "127.0.0.1", "-t", MQTT_TOPIC],
                                stdout=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if line.strip() == "START":
                log.info("door opened (mqtt)")
                events.trigger("door", time.time(), hold=DOOR_HOLD)
        proc.wait()
        log.warning("mosquitto_sub exited rc=%s, restarting", proc.returncode)
        time.sleep(5)


def hash_new_segments(seen):
    for p in closed_segments():
        if p not in seen:
            record_hash(p)
            seen.add(p)


def free_space(events):
    if shutil.disk_usage(BASE).free >= MIN_FREE_BYTES:
        return
    protected = events.protected_after()
    segs = sorted(closed_segments())[:-KEEP_RECENT_SEGMENTS]
    for p in segs:
        if shutil.disk_usage(BASE).free >= MIN_FREE_BYTES:
            break
        if protected is not None and seg_start_time(p) >= protected:
            break
        log.info("low disk, deleting %s", p)
        os.remove(p)


def write_sensor_temp(temp):
    if temp is None:
        return
    tmp = SENSOR_TEMP + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{temp:.1f}\n")
    os.replace(tmp, SENSOR_TEMP)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    os.makedirs(SEG_DIR, exist_ok=True)
    os.makedirs(EVENT_DIR, exist_ok=True)

    events = EventManager()
    imx500 = IMX500(MODEL)
    picam2 = Picamera2(imx500.camera_num)
    config = picam2.create_video_configuration(
        main={"size": MAIN_SIZE, "format": "YUV420"},
        lores={"size": LORES_SIZE, "format": "YUV420"},
        controls={"FrameRate": FPS},
        buffer_count=6,
    )
    picam2.configure(config)
    detector = Detector(imx500, events)
    picam2.pre_callback = detector

    h264 = LibavH264Encoder(qp=QP, iperiod=FPS, framerate=FPS)
    segment_args = (f"-f segment -segment_time {SEGMENT_SECONDS} -segment_atclocktime 1 "
                    f"-reset_timestamps 1 -strftime 1 -segment_format matroska "
                    f"-segment_list {SEG_LIST} -segment_list_type csv "
                    f"{SEG_DIR}/%Y-%m-%d_%H-%M-%S.mkv")
    segments_out = FfmpegOutput(segment_args)
    picam2.start_encoder(h264, segments_out, name="main")
    picam2.start_encoder(MJPEGEncoder(), FileOutput(preview), name="lores")
    picam2.start()
    log.info("recording started")

    threading.Thread(target=mqtt_loop, args=(events,), daemon=True).start()
    server = ThreadingHTTPServer(MJPEG_ADDR, StreamingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    seen = set(closed_segments())
    last_housekeeping = 0.0
    try:
        while True:
            time.sleep(2)
            if time.time() - detector.last_frame > 30:
                raise RuntimeError("no camera frames for 30s")
            if segments_out.ffmpeg is not None and segments_out.ffmpeg.poll() is not None:
                raise RuntimeError(f"ffmpeg exited rc={segments_out.ffmpeg.returncode}")
            events.tick()
            write_sensor_temp(detector.sensor_temp)
            if time.time() - last_housekeeping > 60:
                last_housekeeping = time.time()
                hash_new_segments(seen)
                free_space(events)
                with open(HEARTBEAT, "w") as f:
                    f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z\n"))
    finally:
        # Let systemd restart us; flush what we have first.
        try:
            picam2.stop_encoder()
            picam2.stop()
        except Exception:
            log.exception("stopping camera")


if __name__ == "__main__":
    main()
