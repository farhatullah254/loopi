"""Fetch metadata, the YouTube caption track and the video file with yt-dlp."""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .paths import ROOT

YTDLP = str(ROOT / ".venv" / "bin" / "yt-dlp")

VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_id(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


FETCH_LOCK = threading.Lock()
MIN_FREE_GB = 5
BACKOFF = (20, 60, 150)


def _common() -> list[str]:
    """Flags on every call: a JS runtime for YouTube's challenge, polite pacing, built-in retries."""
    args = ["--retries", "5", "--extractor-retries", "3", "--sleep-subtitles", "2", "--sleep-requests", "1"]
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    if shutil.which("deno") is None and Path(node).exists():
        args += ["--js-runtimes", f"node:{node}"]
    return args


def _run(args, cwd=None, backoff=True) -> subprocess.CompletedProcess:
    """Run yt-dlp; on HTTP 429 wait and retry with growing delays before giving up."""
    args = [args[0]] + _common() + list(args[1:])
    last = ""
    for attempt in range(len(BACKOFF) + 1 if backoff else 1):
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if p.returncode == 0:
            return p
        out = (p.stderr or p.stdout or "").strip()
        last = " | ".join(l for l in out.splitlines()[-6:] if "WARNING" not in l) or out[-400:]
        transient_403 = "HTTP Error 403" in out and attempt < 2
        if transient_403 and backoff:
            print("yt-dlp got 403 from YouTube's video server; retrying with fresh links in 10s", flush=True)
            time.sleep(10)
            continue
        if "429" not in out or not backoff or attempt == len(BACKOFF):
            break
        wait = BACKOFF[attempt]
        print(f"yt-dlp rate-limited by YouTube (429); retrying in {wait}s", flush=True)
        time.sleep(wait)
    if "429" in last:
        raise RuntimeError("YouTube is rate-limiting this machine (HTTP 429) even after retries. "
                           "Wait 10-15 minutes and press Re-fetch video. Details: " + last)
    raise RuntimeError("yt-dlp failed: " + last)


class CaptionsRateLimited(RuntimeError):
    """YouTube's caption endpoint answered 429; the rest of YouTube may still work."""


def fetch_info(url: str) -> dict:
    p = _run([YTDLP, "--dump-single-json", "--no-playlist", "--skip-download", url])
    return json.loads(p.stdout)


def meta_from_info(info: dict) -> dict:
    return {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "description": info.get("description") or "",
        "chapters": info.get("chapters") or [],
        "has_manual_subs": bool(info.get("subtitles")),
        "has_auto_subs": bool(info.get("automatic_captions")),
    }


def metadata(url: str) -> dict:
    return meta_from_info(fetch_info(url))


def _pick_track(info: dict):
    """One English track: the creator's own if present, else YouTube's original-language ASR."""
    manual = info.get("subtitles") or {}
    for key in ["en", "en-US", "en-GB"] + sorted(k for k in manual if k.startswith("en")):
        if key in manual:
            return "manual", key, manual[key]
    auto = info.get("automatic_captions") or {}
    for key in ("en-orig", "en"):
        if key in auto:
            return "auto", key, auto[key]
    return None, None, None


def download_captions_from_info(info: dict, dest: Path) -> list[dict]:
    """Fetch exactly one caption file (json3) with a browser fingerprint. One request, not dozens."""
    cached = dest / "captions.json3"
    if cached.exists() and cached.stat().st_size > 100:
        cues = _parse_json3(json.loads(cached.read_text()))
        if cues:
            return cues
    kind, key, formats = _pick_track(info)
    if not formats:
        raise RuntimeError("No English caption track found on this video (neither manual nor auto-generated).")
    url = next((f["url"] for f in formats if f.get("ext") == "json3"), None)
    if not url:
        raise RuntimeError(f"Caption track {key} has no json3 format")
    from curl_cffi import requests as cr
    last = None
    for wait in (0, 8, 25):
        if wait:
            time.sleep(wait)
        r = cr.get(url, impersonate="chrome", timeout=40)
        last = r.status_code
        if r.status_code == 200 and r.content:
            cached.write_bytes(r.content)
            cues = _parse_json3(json.loads(r.content))
            if cues:
                return cues
            raise RuntimeError(f"Caption track {key} ({kind}) is empty")
        if r.status_code != 429:
            break
    if last == 429:
        signed = _captions_signed_in(info, dest, kind, key)
        if signed:
            return signed
        raise CaptionsRateLimited("YouTube is temporarily blocking caption downloads from this machine (HTTP 429).")
    raise RuntimeError(f"Caption download failed with HTTP {last}")


def _captions_signed_in(info: dict, dest: Path, kind: str, key: str):
    """One signed-in request for exactly this caption track. None if no sign-in is saved."""
    from . import ytauth
    cookies = ytauth.cookies_file()
    if not cookies:
        return None
    for f in dest.glob("captions_auth*"):
        f.unlink()
    url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={info.get('id')}"
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    print(f"captions blocked as guest; one signed-in request for track {key}", flush=True)
    try:
        _run([YTDLP, "--cookies", cookies, "--no-playlist", "--skip-download", flag, "--sub-langs", key,
              "--sub-format", "json3", "-o", str(dest / "captions_auth"), url], backoff=False)
    except RuntimeError as e:
        if "429" in str(e):
            raise CaptionsRateLimited("YouTube is blocking caption downloads even when signed in (HTTP 429).")
        if "Sign in" in str(e) or "cookies" in str(e).lower():
            raise RuntimeError("The saved YouTube sign-in was rejected or has expired. Open Settings, sign in "
                               "again, then press Try now. Details: " + str(e)[:300])
        raise
    files = sorted(dest.glob("captions_auth*.json3"))
    if not files:
        return None
    (dest / "captions.json3").write_bytes(files[0].read_bytes())
    return _parse_json3(json.loads(files[0].read_text())) or None


def download_captions(url: str, dest: Path) -> list[dict]:
    return download_captions_from_info(fetch_info(url), dest)


def _parse_json3(data: dict) -> list[dict]:
    cues = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000.0
        dur = ev.get("dDurationMs", 0) / 1000.0
        words = []
        for s in segs:
            w = s.get("utf8", "").strip()
            if w:
                words.append({"t": round(start + s.get("tOffsetMs", 0) / 1000.0, 2), "w": w})
        cues.append({"start": round(start, 2), "end": round(start + dur, 2), "text": text, "words": words})
    # auto tracks emit duplicate rolling lines; drop exact consecutive repeats
    out = []
    for c in cues:
        if out and out[-1]["text"] == c["text"]:
            out[-1]["end"] = c["end"]
            continue
        out.append(c)
    return out


def download_video(url: str, dest: Path, max_height=720) -> Path:
    target = dest / "video.mp4"
    if target.exists() and target.stat().st_size > 0:
        return target
    free_gb = shutil.disk_usage(dest).free / 1e9
    if free_gb < MIN_FREE_GB:
        raise RuntimeError(f"Not enough disk space to download the video: {free_gb:.1f} GB free, Loopi needs at "
                           f"least {MIN_FREE_GB} GB. Free space in Settings → Storage, then press Re-fetch video.")
    fmt = (f"bv*[height<={max_height}][ext=mp4]/bv*[height<={max_height}]/"
           f"b[height<={max_height}][ext=mp4]/b")
    try:
        _run([YTDLP, "--no-playlist", "-f", fmt, "--ffmpeg-location", _ffmpeg(),
              "--merge-output-format", "mp4", "-o", str(dest / "video.%(ext)s"), url])
    except Exception:
        for f in dest.glob("video.*"):
            if f.name != "video.mp4":
                f.unlink(missing_ok=True)
        raise
    cands = sorted(dest.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not cands:
        raise RuntimeError("Video download produced no file")
    if cands[0] != target:
        cands[0].rename(target)
    return target


def download_thumbnail(url_or_id: str, dest: Path) -> Path | None:
    """Save the best available YouTube thumbnail as thumb.jpg. Returns None if nothing fetched."""
    import urllib.request
    vid = video_id(url_or_id) or url_or_id
    target = dest / "thumb.jpg"
    for name in ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg"):
        try:
            with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/{name}", timeout=20) as r:
                data = r.read()
            if len(data) > 5000:
                target.write_bytes(data)
                return target
        except Exception:
            continue
    return None
