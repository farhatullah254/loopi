"""Disk usage for Loopi's data folder and the two safe clean-ups the Storage card offers."""
import shutil

from . import db
from .paths import DATA_DIR, LIBRARY_DIR, PROJECTS_DIR


def _size(paths) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _partials():
    return [f for d in PROJECTS_DIR.iterdir() if d.is_dir() for f in d.glob("video.*") if f.name != "video.mp4"]


def _finished_videos():
    done = {p["id"] for p in db.list_projects() if p["stage"] == "ready" and p.get("pdf_count")
            and p["status"] not in ("running", "queued", "waiting")}
    return [PROJECTS_DIR / str(pid) / "video.mp4" for pid in done if (PROJECTS_DIR / str(pid) / "video.mp4").exists()]


def usage() -> dict:
    du = shutil.disk_usage(DATA_DIR)
    videos = list(PROJECTS_DIR.glob("*/video.mp4"))
    return {
        "disk_free": du.free, "disk_total": du.total,
        "videos": _size(videos), "videos_count": len(videos),
        "finished_videos": _size(_finished_videos()), "finished_videos_count": len(_finished_videos()),
        "partials": _size(_partials()), "partials_count": len(_partials()),
        "frames": _size(PROJECTS_DIR.glob("*/frames/*")),
        "library": _size(LIBRARY_DIR.glob("*.pdf")),
    }


def clean(what: str) -> dict:
    if what == "partials":
        files = _partials()
    elif what == "finished_videos":
        files = _finished_videos()
    else:
        raise ValueError("what must be partials or finished_videos")
    freed = _size(files)
    for f in files:
        f.unlink(missing_ok=True)
    return {"deleted": len(files), "freed": freed}
