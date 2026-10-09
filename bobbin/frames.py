"""Grab video frames with the bundled ffmpeg binary."""
import subprocess
from pathlib import Path


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def grab(video: Path, t: float, out: Path, width=1280) -> Path:
    t = max(0.0, t)
    subprocess.run([_ffmpeg(), "-nostdin", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                    "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "2", str(out)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return out


def burst(video: Path, t: float, out_dir: Path, tag: str, before=3, after=4, step=1.0,
          duration: float | None = None) -> list[dict]:
    """Frames from t-before to t+after (inclusive). Returns [{t, path}]."""
    out = []
    k = -before
    while k <= after:
        tt = round(t + k * step, 2)
        if tt >= 0 and (duration is None or tt <= duration):
            p = out_dir / f"{tag}_{tt:.0f}s.jpg"
            try:
                grab(video, tt, p)
                out.append({"t": tt, "path": p.name})
            except subprocess.CalledProcessError:
                pass
        k += 1
    return out


def thumb(video: Path, out: Path) -> Path:
    return grab(video, 1.0, out, width=480)
