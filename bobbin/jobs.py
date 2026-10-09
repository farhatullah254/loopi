"""Background pipeline: fetch -> clean -> extract -> frames -> ready.

Several projects run at once (setting `workers`, default 3). Each project has its own folder,
database row and Claude processes, so runs never share state. Only the YouTube fetch is
serialized, because parallel downloads are what trigger YouTube's HTTP 429."""
import queue
import threading
import time
import traceback

from . import db, frames, llm, review, validate, youtube
from .paths import project_dir

STAGES = ["new", "fetched", "cleaned", "extracted", "ready"]
_q: "queue.Queue[tuple[int, str]]" = queue.Queue()
_running: set[int] = set()
_lock = threading.Lock()


MAX_WORKERS = 6


def workers() -> int:
    try:
        return max(1, min(MAX_WORKERS, int(db.get_setting("workers", "3"))))
    except ValueError:
        return 3


def start_worker():
    for i in range(MAX_WORKERS):
        threading.Thread(target=_worker, args=(i,), daemon=True, name=f"bobbin-worker-{i}").start()


CAPTION_RETRY_MIN = (15, 30, 60, 120)
_timers: dict[int, threading.Timer] = {}
_caption_attempts: dict[int, int] = {}


def schedule_retry(pid: int, minutes: float, target: str = "ready"):
    old = _timers.pop(pid, None)
    if old:
        old.cancel()
    t = threading.Timer(minutes * 60, lambda: (_timers.pop(pid, None), enqueue(pid, target)))
    t.daemon = True
    _timers[pid] = t
    t.start()


def enqueue(pid: int, target: str = "ready"):
    t = _timers.pop(pid, None)
    if t:
        t.cancel()
    with _lock:
        if pid in _running:
            return False
        _running.add(pid)
    db.update_project(pid, status="queued", error=None, progress="Waiting in queue")
    _q.put((pid, target))
    return True


def is_running(pid: int) -> bool:
    return pid in _running


_active = 0
_active_cv = threading.Condition()


def _worker(idx: int):
    global _active
    while True:
        pid, target = _q.get()
        with _active_cv:
            while _active >= workers():
                _active_cv.wait(timeout=5)
            _active += 1
        try:
            _run(pid, target)
        except Exception as e:
            traceback.print_exc()
            db.update_project(pid, status="error", error=f"{type(e).__name__}: {e}")
        finally:
            with _lock:
                _running.discard(pid)
            with _active_cv:
                _active -= 1
                _active_cv.notify_all()
            _q.task_done()


def _progress(pid):
    def f(msg):
        db.update_project(pid, progress=msg)
        print(f"[{pid}] {msg}", flush=True)
    return f


def _run(pid: int, target: str):
    p = db.get_project(pid)
    if not p:
        return
    prog = _progress(pid)
    db.update_project(pid, status="running")
    pdir = project_dir(pid)
    stage = p["stage"]
    want = STAGES.index(target)

    if STAGES.index(stage) < 1 <= want:
        if youtube.FETCH_LOCK.locked():
            prog("Waiting for another video to finish downloading")
        youtube.FETCH_LOCK.acquire()
        try:
            meta, cues = _fetch(pid, p, pdir, prog)
        except youtube.CaptionsRateLimited:
            n = _caption_attempts.get(pid, 0)
            if n >= len(CAPTION_RETRY_MIN):
                _caption_attempts.pop(pid, None)
                raise RuntimeError("YouTube kept blocking caption downloads for about 4 hours. The video is "
                                   "already downloaded; press Re-fetch video later to try the captions again.")
            _caption_attempts[pid] = n + 1
            mins = CAPTION_RETRY_MIN[n]
            at = time.strftime("%H:%M", time.localtime(time.time() + mins * 60))
            db.update_project(pid, status="waiting", error=None,
                              progress=f"YouTube is limiting caption downloads. Video saved; captions retry "
                                       f"automatically at {at} (attempt {n + 2} of {len(CAPTION_RETRY_MIN) + 1}).")
            print(f"[{pid}] captions 429, retry in {mins} min", flush=True)
            schedule_retry(pid, mins, target)
            return
        finally:
            youtube.FETCH_LOCK.release()
        _caption_attempts.pop(pid, None)
        p = db.get_project(pid)
        p["_meta"] = meta
        stage = "fetched"

    meta = p.get("_meta") or _meta_from_project(p)

    if STAGES.index(stage) < 2 <= want:
        cleaned = llm.clean_captions(p["captions"], meta, prog)
        db.update_project(pid, cleaned=cleaned, stage="cleaned")
        p["cleaned"] = cleaned
        stage = "cleaned"

    if STAGES.index(stage) < 3 <= want:
        pattern = llm.extract_pattern(p["cleaned"], meta, p["density"], prog)
        if meta.get("description"):
            pattern["source"] = {"description": meta["description"][:5000]}
        pattern["validation"] = validate.run(pattern)
        db.update_project(pid, pattern=pattern, stage="extracted")
        p["pattern"] = pattern
        stage = "extracted"

    if STAGES.index(stage) < 4 <= want:
        _grab_all(pid, p, prog)
        review.run(p["pattern"], p.get("cleaned") or [], project_dir(pid), prog)
        db.update_project(pid, pattern=p["pattern"], stage="ready")

    db.update_project(pid, status="idle", progress="Done")


def _fetch(pid, p, pdir, prog):
    prog("Reading video details")
    info = youtube.fetch_info(p["url"])
    meta = youtube.meta_from_info(info)
    db.update_project(pid, video_id=meta["video_id"], title=meta["title"], channel=meta["channel"],
                      duration=meta["duration"], thumbnail=meta["thumbnail"])
    if not p.get("thumb_file"):
        thumb = youtube.download_thumbnail(meta["video_id"], pdir)
        if thumb:
            db.update_project(pid, thumb_file=thumb.name)
    prog(f"Downloading video ({meta['video_id']})")
    youtube.download_video(p["url"], pdir)
    prog("Fetching YouTube caption track")
    cues = youtube.download_captions_from_info(info, pdir)
    db.update_project(pid, captions=cues, stage="fetched", progress=f"{len(cues)} caption cues")
    return meta, cues


def _meta_from_project(p):
    return {"title": p.get("title"), "channel": p.get("channel"), "duration": p.get("duration"),
            "description": "", "chapters": []}


def ensure_video(pid, p):
    """Frames need the video. If it was deleted to save space, download it again (no Claude work redone)."""
    video = project_dir(pid) / "video.mp4"
    if not video.exists():
        with youtube.FETCH_LOCK:
            youtube.download_video(p["url"], project_dir(pid))
    return video


def _grab_all(pid, p, prog):
    pattern = p["pattern"]
    pdir = project_dir(pid)
    if not (pdir / "video.mp4").exists():
        prog("Re-downloading the video for frame grabs")
    video = ensure_video(pid, p)
    fdir = pdir / "frames"
    duration = p.get("duration")
    jobs = []
    for sec in pattern["sections"]:
        for st in sec["steps"]:
            if st.get("tier") == "none":
                continue
            for k, img in enumerate(st.get("images", [])):
                jobs.append((f"{st['id']}_{k}", img))
    for k, img in enumerate(pattern.get("final_images", [])):
        jobs.append((f"final_{k}", img))
    for i, (tag, img) in enumerate(jobs):
        if img.get("manual") or img.get("chosen"):
            continue
        prog(f"Grabbing frames {i + 1}/{len(jobs)}")
        grab_and_pick(video, fdir, tag, img, duration)
        db.update_project(pid, pattern=pattern)
    prog("Frames done")


def grab_and_pick(video, fdir, tag, img, duration=None, before=3, after=4):
    t = float(img.get("t") or 0)
    cands = frames.burst(video, t, fdir, tag, before=before, after=after, duration=duration)
    img["candidates"] = cands
    if not cands:
        img["chosen"] = None
        return
    try:
        pick = llm.pick_frame(cands, fdir, img.get("description") or "the crochet work at this moment")
        img["chosen"] = cands[pick["index"]]["path"]
        img["quality"] = pick["quality"]
        img["pick_reason"] = pick["reason"]
    except Exception as e:
        img["chosen"] = cands[min(before, len(cands) - 1)]["path"]
        img["quality"] = "unjudged"
        img["pick_reason"] = f"vision pick failed: {e}"
