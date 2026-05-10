"""
Thumbnail sprite generator for DVR recordings.

For each .mp4 recording segment that doesn't have a sprite yet,
runs ffmpeg to extract frames (1 per 10s) and tiles them into a sprite sheet.
Also writes a JSON metadata file with timestamp info.

Sprite layout: 160x90px thumbnails, 10 per row.
JSON: { "interval": 10, "cols": 10, "count": N, "width": 160, "height": 90 }
"""
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
THUMB_INTERVAL = 10       # seconds between thumbnails
THUMB_W, THUMB_H = 160, 90
SPRITE_COLS = 10

# YOLOv5n — tiny COCO model, downloaded once to /data (persists across restarts)
_DATA_DIR = os.environ.get("DATA_DIR", "/data")
_YOLO_MODEL_PATH = os.path.join(_DATA_DIR, "yolov5n.onnx")
_YOLO_MODEL_URL = "https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx"
_YOLO_NET = None   # lazy-loaded, cached in memory per worker

_SEG_PATTERN = re.compile(r'^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})-\d+\.mp4$')


def _parse_segment_start(fname: str) -> str | None:
    """Parse a segment filename → ISO timestamp string matching recordings_list output."""
    m = _SEG_PATTERN.match(fname)
    if not m:
        return None
    return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}Z"


def _ffmpeg_sprite(mp4_path: Path, sprite_path: Path, meta_path: Path) -> bool:
    """Generate a sprite sheet from an mp4 file. Returns True on success."""
    with tempfile.TemporaryDirectory() as tmp:
        # Extract one frame every THUMB_INTERVAL seconds
        extract_cmd = [
            "ffmpeg", "-y",
            "-i", str(mp4_path),
            "-vf", f"fps=1/{THUMB_INTERVAL},scale={THUMB_W}:{THUMB_H}",
            "-q:v", "5",
            os.path.join(tmp, "thumb_%04d.jpg"),
        ]
        result = subprocess.run(extract_cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            logger.warning("ffmpeg extract failed for %s: %s", mp4_path.name,
                           result.stderr[-300:].decode(errors="replace"))
            return False

        thumbs = sorted(Path(tmp).glob("thumb_*.jpg"))
        if not thumbs:
            logger.warning("No thumbnails extracted from %s", mp4_path.name)
            return False

        count = len(thumbs)
        rows = (count + SPRITE_COLS - 1) // SPRITE_COLS

        # Tile into sprite using ffmpeg
        tile_inputs = []
        for t in thumbs:
            tile_inputs += ["-i", str(t)]

        filter_str = (
            f"[0]" + "".join(f"[{i}]" for i in range(1, count))
            if count == 1
            else "".join(f"[{i}:v]" for i in range(count))
            + f"xstack=inputs={count}:layout="
            + "|".join(
                f"{(i % SPRITE_COLS) * THUMB_W}_{(i // SPRITE_COLS) * THUMB_H}"
                for i in range(count)
            )
            + f":fill=black[out]"
        )

        if count == 1:
            # Single thumbnail — just copy
            import shutil
            shutil.copy(str(thumbs[0]), str(sprite_path))
        else:
            # Use ffmpeg xstack or convert approach
            tile_cmd = [
                "ffmpeg", "-y",
                "-i", str(mp4_path),
                "-vf", (
                    f"fps=1/{THUMB_INTERVAL},scale={THUMB_W}:{THUMB_H},"
                    f"tile={SPRITE_COLS}x{rows}"
                ),
                "-frames:v", "1",
                "-q:v", "5",
                str(sprite_path),
            ]
            result2 = subprocess.run(tile_cmd, capture_output=True, timeout=300)
            if result2.returncode != 0:
                logger.warning("ffmpeg tile failed for %s: %s", mp4_path.name,
                               result2.stderr[-300:].decode(errors="replace"))
                return False

        meta = {
            "interval": THUMB_INTERVAL,
            "cols": SPRITE_COLS,
            "rows": rows,
            "count": count,
            "width": THUMB_W,
            "height": THUMB_H,
            "sprite_w": SPRITE_COLS * THUMB_W,
            "sprite_h": rows * THUMB_H,
        }
        meta_path.write_text(json.dumps(meta))
        logger.info("Sprite generated: %s (%d thumbs)", sprite_path.name, count)
        return True


def _get_yolo_net():
    """Lazy-load YOLOv5n ONNX model, downloading it on first use."""
    global _YOLO_NET
    if _YOLO_NET is not None:
        return _YOLO_NET
    try:
        import cv2
    except ImportError:
        return None

    if not os.path.exists(_YOLO_MODEL_PATH):
        logger.info("Downloading YOLOv5n model (~7MB) to %s …", _YOLO_MODEL_PATH)
        tmp = _YOLO_MODEL_PATH + ".tmp"
        try:
            urllib.request.urlretrieve(_YOLO_MODEL_URL, tmp)
            os.rename(tmp, _YOLO_MODEL_PATH)
            logger.info("YOLOv5n model ready")
        except Exception as exc:
            logger.warning("Failed to download YOLOv5n: %s", exc)
            if os.path.exists(tmp):
                os.remove(tmp)
            return None

    try:
        net = cv2.dnn.readNetFromONNX(_YOLO_MODEL_PATH)
        _YOLO_NET = net
        logger.info("YOLOv5n loaded via OpenCV DNN")
        return net
    except Exception as exc:
        logger.warning("Failed to load YOLOv5n: %s", exc)
        return None


def _detect_person_yolo(frame, net) -> bool:
    """
    Return True if a person (COCO class 0) is detected in the frame.
    Uses tiled approach: checks full frame + horizontal/vertical crops
    to handle small persons in wide-angle outdoor cameras.
    """
    import cv2
    import numpy as np

    INPUT_SIZE = 640
    CONF_THRESHOLD = 0.15   # lower threshold needed for small/distant persons
    PERSON_CLASS = 0

    h, w = frame.shape[:2]

    # Build tiles: full frame + 3 horizontal strips + 2 vertical halves
    tiles = [cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE))]
    for x_start in [0, w // 3, 2 * w // 3]:
        tile_w = min(w // 2, w - x_start)
        crop = frame[:, x_start: x_start + tile_w]
        tiles.append(cv2.resize(crop, (INPUT_SIZE, INPUT_SIZE)))
    for y_start in [0, h // 2]:
        crop = frame[y_start: y_start + h // 2, :]
        tiles.append(cv2.resize(crop, (INPUT_SIZE, INPUT_SIZE)))

    for tile in tiles:
        blob = cv2.dnn.blobFromImage(
            tile, 1 / 255.0, (INPUT_SIZE, INPUT_SIZE), swapRB=True, crop=False
        )
        net.setInput(blob)
        outputs = net.forward()   # shape: [1, 25200, 85]
        for det in outputs[0]:
            obj_conf = float(det[4])
            if obj_conf < 0.1:
                continue
            person_conf = obj_conf * float(det[5 + PERSON_CLASS])
            if person_conf >= CONF_THRESHOLD:
                return True
    return False


def detect_motion_in_segment(cam: str, mp4_path: Path, segment_start_iso: str) -> int:
    """
    Analyze a recording segment for motion and persons using OpenCV.

    Samples one frame every 5 seconds, computes frame difference to detect motion,
    then runs YOLOv5n person detection on frames that show motion.

    Stores results in DB and marks segment as analyzed.
    Returns number of motion events saved.
    """
    try:
        import cv2  # opencv-python-headless
    except ImportError:
        logger.debug("opencv not installed — skipping motion detection")
        return 0

    from db import has_motion_analyzed, save_motion_events
    if has_motion_analyzed(cam, segment_start_iso):
        return 0

    cap = cv2.VideoCapture(str(mp4_path))
    if not cap.isOpened():
        return 0

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if fps > 0 else 300

    SAMPLE_SEC = 5          # sample every N seconds
    MOTION_THRESH = 0.005   # fraction of pixels that must change (0.5%)

    yolo_net = _get_yolo_net()  # may be None if download failed

    events = []
    prev_gray = None
    t = SAMPLE_SEC

    while t < duration_sec:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret:
            t += SAMPLE_SEC
            continue

        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            _, thresh = cv2.threshold(diff, 20, 255, cv2.THRESH_BINARY)
            motion_ratio = cv2.countNonZero(thresh) / thresh.size

            if motion_ratio >= MOTION_THRESH:
                is_person = False
                if yolo_net is not None:
                    try:
                        # Pass full-resolution frame — tiling handles small persons
                        is_person = _detect_person_yolo(frame, yolo_net)
                    except Exception as exc:
                        logger.debug("YOLO detection error: %s", exc)

                events.append({
                    "offset_sec": round(t, 1),
                    "motion_type": "person" if is_person else "motion",
                })

        prev_gray = gray
        t += SAMPLE_SEC

    cap.release()
    save_motion_events(cam, segment_start_iso, events)
    if events:
        logger.info("Motion: %d events in %s (%d person, %d motion)",
                    len(events), mp4_path.name,
                    sum(1 for e in events if e["motion_type"] == "person"),
                    sum(1 for e in events if e["motion_type"] == "motion"))
    return len(events)


def generate_pending_sprites(cam: str) -> int:
    """Generate sprites for all segments that don't have one yet. Returns count generated."""
    cam_dir = Path(RECORDINGS_DIR) / cam
    if not cam_dir.exists():
        return 0

    generated = 0
    for mp4 in sorted(cam_dir.glob("*.mp4")):
        sprite = mp4.with_suffix(".sprite.jpg")
        meta = mp4.with_suffix(".sprite.json")
        # Don't process the currently-being-written segment (< 500 KB)
        try:
            stat = mp4.stat()
            if stat.st_size < 500_000:
                continue
        except OSError:
            continue

        segment_start_iso = _parse_segment_start(mp4.name)

        if not (sprite.exists() and meta.exists()):
            logger.info("Generating sprite for %s", mp4.name)
            if _ffmpeg_sprite(mp4, sprite, meta):
                generated += 1

        # Run motion detection for segments not yet analyzed
        if segment_start_iso:
            detect_motion_in_segment(cam, mp4, segment_start_iso)

    return generated


def cleanup_old_segments(retention_hours: int) -> int:
    """Delete recording segments (and their sprites) older than retention_hours. Returns count deleted."""
    import time
    cutoff = time.time() - retention_hours * 3600
    deleted = 0
    rec_dir = Path(RECORDINGS_DIR)
    if not rec_dir.exists():
        return 0
    for cam_dir in rec_dir.iterdir():
        if not cam_dir.is_dir():
            continue
        for mp4 in cam_dir.glob("*.mp4"):
            try:
                if mp4.stat().st_mtime < cutoff:
                    mp4.unlink()
                    deleted += 1
                    for ext in (".sprite.jpg", ".sprite.json"):
                        companion = mp4.with_name(mp4.stem + ext)
                        if companion.exists():
                            companion.unlink()
            except OSError:
                pass
    if deleted:
        logger.info("Cleanup: deleted %d old recording segments (retention=%dh)", deleted, retention_hours)
    return deleted


def run_all():
    """Generate sprites for both cameras based on current quality setting."""
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.dirname(__file__))
        from db import get_setting
        quality = get_setting("dvr_record_quality", "sd")
    except Exception:
        quality = "sd"
    cameras = ("camera1", "camera2") if quality == "hd" else ("camera1lo", "camera2lo")
    for cam in cameras:
        n = generate_pending_sprites(cam)
        if n:
            logger.info("Generated %d sprites for %s", n, cam)
