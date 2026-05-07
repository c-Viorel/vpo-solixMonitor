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
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
THUMB_INTERVAL = 10       # seconds between thumbnails
THUMB_W, THUMB_H = 160, 90
SPRITE_COLS = 10


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


def generate_pending_sprites(cam: str) -> int:
    """Generate sprites for all segments that don't have one yet. Returns count generated."""
    cam_dir = Path(RECORDINGS_DIR) / cam
    if not cam_dir.exists():
        return 0

    generated = 0
    for mp4 in sorted(cam_dir.glob("*.mp4")):
        sprite = mp4.with_suffix(".sprite.jpg")
        meta = mp4.with_suffix(".sprite.json")
        if sprite.exists() and meta.exists():
            continue
        # Don't process the currently-being-written segment (< 60s old, < 1MB)
        try:
            stat = mp4.stat()
            if stat.st_size < 500_000:
                continue
        except OSError:
            continue
        logger.info("Generating sprite for %s", mp4.name)
        if _ffmpeg_sprite(mp4, sprite, meta):
            generated += 1
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
    """Generate sprites for both cameras."""
    for cam in ("camera1lo", "camera2lo"):
        n = generate_pending_sprites(cam)
        if n:
            logger.info("Generated %d sprites for %s", n, cam)
