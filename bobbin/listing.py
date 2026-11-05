"""Three Etsy listing photos of the finished object, 4:3 at 2400x1800.

Only the choice of frames uses Claude (which frames show the whole finished piece, and where it sits
in the frame). Every pixel change is plain code: crop, a cautious white balance read only from
neutral pixels, a levels stretch applied equally to all channels so yarn colour keeps its hue, a
Lanczos resize and an unsharp mask. Nothing is generated or retouched.

The pattern pipeline keeps a 720p copy of the video, which is too soft for 2400x1800, so the
finished-object moments are downloaded again at the best resolution YouTube has (up to 2160p).
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

from . import llm, youtube
from .paths import project_dir

OUT_W, OUT_H = 2400, 1800
WANT = 3
WINDOW = 12         # seconds either side of each finished-object timestamp
OUTRO = 45          # the last seconds of the video usually show the finished piece too
INTRO = (2, 25)     # the finished piece is often shown before the tutorial starts
FPS = 2
SHORTLIST = 20
MIN_CROP_W = 1800   # never crop narrower than this many source pixels unless the frame itself is smaller


def _windows(project: dict) -> list[tuple[float, float]]:
    dur = float(project.get("duration") or 0) or None
    pattern = project.get("pattern") or {}
    ts = [float(i["t"]) for i in pattern.get("final_images", []) if i.get("t") is not None]
    if not ts and dur:
        ts = [max(0.0, dur - 30), max(0.0, dur - 12)]
    wins = [(max(0.0, t - WINDOW), t + WINDOW) for t in ts]
    if not dur or dur > 120:
        wins.append(INTRO)
    if dur and dur > 120:
        wins.append((dur - OUTRO, dur))
    if dur:
        wins = [(a, min(b, dur)) for a, b in wins if a < dur]
    wins.sort()
    merged: list[list[float]] = []
    for a, b in wins:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(round(a, 1), round(b, 1)) for a, b in merged]


def _download_sections(url: str, wins, dest: Path) -> list[tuple[Path, float]]:
    """Video-only download of just these windows at up to 2160p. Returns [(file, window_start)]."""
    fmt = "bv*[height<=2160][ext=mp4]/bv*[height<=2160]/b[height<=2160]"
    args = [youtube.YTDLP, "--no-playlist", "-f", fmt, "--ffmpeg-location", youtube._ffmpeg(),
            "-o", str(dest / "src_%(section_start)s.%(ext)s")]
    for a, b in wins:
        args += ["--download-sections", f"*{a}-{b}"]
    with youtube.FETCH_LOCK:
        youtube._run(args + [url])
    out = []
    for f in dest.glob("src_*"):
        if f.suffix in (".part", ".ytdl"):
            continue
        try:
            out.append((f, float(f.stem.split("_", 1)[1])))
        except ValueError:
            continue
    return sorted(out, key=lambda x: x[1])


def _sample(src: Path, start: float, out_dir: Path, tag: str, seek: tuple[float, float] | None = None) -> list[dict]:
    """Every 1/FPS seconds of `src` as full-resolution JPEGs. `seek` cuts a window from a full video."""
    cmd = [youtube._ffmpeg(), "-nostdin", "-y", "-loglevel", "error"]
    if seek:
        cmd += ["-ss", f"{seek[0]:.2f}", "-t", f"{seek[1] - seek[0]:.2f}"]
    cmd += ["-i", str(src), "-vf", f"fps={FPS}", "-q:v", "1", str(out_dir / f"{tag}_%04d.jpg")]
    subprocess.run(cmd, check=True, capture_output=True, stdin=subprocess.DEVNULL)
    frames = sorted(out_dir.glob(f"{tag}_*.jpg"))
    return [{"path": f.name, "t": round(start + i / FPS, 1)} for i, f in enumerate(frames)]


def _measure(path: Path) -> dict:
    im = Image.open(path).convert("L")
    im.thumbnail((960, 960))
    sharp = ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).var[0]
    hist = im.histogram()
    n = sum(hist) or 1
    clipped = (sum(hist[:5]) + sum(hist[251:])) / n
    mean = ImageStat.Stat(im).mean[0]
    exposure = 1 - min(1.0, abs(mean - 125) / 125)
    sig = list(im.resize((16, 9)).getdata())
    return {"score": sharp * (0.5 + 0.5 * exposure) * (1 - min(clipped, 0.5)), "sig": sig}


def _shortlist(cands: list[dict], cdir: Path) -> list[dict]:
    """Sharpest, best-exposed frames, near-identical neighbours dropped, taken in turn from each clip
    so one part of the video (often the subtitled intro, whose crisp text inflates the score) cannot
    crowd out the rest."""
    for c in cands:
        c.update(_measure(cdir / c["path"]))
    groups: dict[str, list[dict]] = {}
    for c in sorted(cands, key=lambda c: -c["score"]):
        groups.setdefault(c["path"].split("_", 1)[0], []).append(c)
    queues = list(groups.values())
    kept = []
    while queues and len(kept) < SHORTLIST:
        for q in list(queues):
            while q:
                c = q.pop(0)
                if all(sum(abs(a - b) for a, b in zip(c["sig"], k["sig"])) / len(c["sig"]) > 7 for k in kept):
                    kept.append(c)
                    break
            if not q:
                queues.remove(q)
            if len(kept) >= SHORTLIST:
                break
    for c in cands:
        c.pop("sig", None)
    return kept


def _box(crop, W: int, H: int, band=None) -> tuple[int, int, int, int]:
    """Claude's fractional box around the object, widened to exactly 4:3 and kept inside the frame
    and inside the text-free band, so burned-in subtitles and titles never enter the photo."""
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in crop)
        if x1 - x0 < 0.05 or y1 - y0 < 0.05:
            raise ValueError
    except (TypeError, ValueError):
        x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
    try:
        b0, b1 = (min(1.0, max(0.0, float(v))) for v in band)
        if b1 - b0 < 0.3:
            raise ValueError
    except (TypeError, ValueError):
        b0, b1 = 0.0, 1.0
    top_lim, bot_lim = b0 * H, b1 * H
    cx, cy = (x0 + x1) / 2 * W, (y0 + y1) / 2 * H
    w, h = (x1 - x0) * W, (y1 - y0) * H
    maxw = min(W, (bot_lim - top_lim) * 4 / 3)
    w = max(w, h * 4 / 3, min(maxw, MIN_CROP_W))
    w = min(w, maxw)
    h = w * 3 / 4
    left = min(max(0.0, cx - w / 2), W - w)
    top = min(max(top_lim, cy - h / 2), bot_lim - h)
    return round(left), round(top), round(left + w), round(top + h)


def _white_balance(im: Image.Image) -> Image.Image:
    """Neutralise a colour cast using only near-grey pixels, at most 6% per channel. Saturated pixels
    (the yarn) never vote, so a red amigurumi does not get pushed toward cyan."""
    small = im.copy()
    small.thumbnail((320, 320))
    px = [p for p in small.getdata() if max(p) - min(p) < 16 and 70 < sum(p) / 3 < 235]
    if len(px) < 0.05 * small.width * small.height:
        return im
    avg = [sum(p[i] for p in px) / len(px) for i in range(3)]
    grey = sum(avg) / 3
    gains = [min(1.06, max(0.94, grey / a)) if a else 1.0 for a in avg]
    if all(abs(g - 1) < 0.01 for g in gains):
        return im
    return Image.merge("RGB", [ch.point(lambda v, g=g: min(255, round(v * g))) for ch, g in zip(im.split(), gains)])


def _levels(im: Image.Image) -> Image.Image:
    """Stretch the tonal range from luminance percentiles, the same map on R, G and B so hue is kept."""
    lum = im.convert("L").histogram()
    n = sum(lum)
    lo = hi = None
    acc = 0
    for v, c in enumerate(lum):
        acc += c
        if lo is None and acc >= n * 0.003:
            lo = v
        if acc >= n * 0.997:
            hi = v
            break
    lo, hi = min(lo or 0, 18), max(hi or 255, 232)
    if lo <= 2 and hi >= 253:
        return im
    scale = 255 / (hi - lo)
    lut = [min(255, max(0, round((v - lo) * scale))) for v in range(256)]
    return im.point(lut * 3)


def _finish(src: Path, crop, band, out: Path, raw_out: Path) -> dict:
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    box = _box(crop, im.width, im.height, band)
    cut = im.crop(box)
    factor = OUT_W / cut.width
    unedited = cut.resize((OUT_W, OUT_H), Image.LANCZOS)
    unedited.save(raw_out, "JPEG", quality=95, subsampling=0)
    fixed = _levels(_white_balance(cut)).resize((OUT_W, OUT_H), Image.LANCZOS)
    radius, percent = (2.0, 70) if factor > 1.3 else (1.3, 55)
    fixed = fixed.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=2))
    fixed.save(out, "JPEG", quality=95, subsampling=0)
    return {"crop_px": list(box), "source_w": cut.width, "upscale": round(factor, 2)}


def _preview(cdir: Path, pdir: Path, names: list[str]) -> None:
    pdir.mkdir(exist_ok=True)
    for n in names:
        im = Image.open(cdir / n)
        im.thumbnail((1280, 1280))
        im.convert("RGB").save(pdir / n, "JPEG", quality=85)


def _choose(short: list[dict], prev_dir: Path, title: str) -> dict:
    api_content, names = [], []
    for i, c in enumerate(short):
        api_content.append({"type": "text", "text": f"Frame {i} at {llm._ts(c['t'])}:"})
        api_content.append(llm._img_block(prev_dir / c["path"]))
        names.append(f"Frame {i} at {llm._ts(c['t'])}: ./{c['path']}")
    want = f"The finished crochet piece: {title}" if title else "The finished crochet piece"
    api_content.append({"type": "text", "text": want})
    cc_prompt = ("Open every one of these image files with the Read tool, then choose.\n"
                 + "\n".join(names) + f"\n\n{want}")
    text = llm._complete(LISTING_SYSTEM, {"api_content": api_content, "cc_prompt": cc_prompt}, "pick",
                         max_tokens=3000, image_dir=prev_dir, max_turns=SHORTLIST + 6)
    return llm._json(text)


LISTING_SYSTEM = """You choose Etsy listing photos for a crochet pattern, from frames of the maker's own tutorial video. The photos are sold-product images: a buyer decides from them. You only choose; the frames are not edited by AI.

Pick exactly 3 frames (fewer only if fewer than 3 frames show the finished object at all) where the FINISHED object is:
- whole: no part cut off by the frame edge or covered by hands (fingers holding an edge are fine if the piece is fully visible; a blurred hand or finger in front of the piece is not),
- in sharp focus, not mid-motion, well lit,
- not a face-cam shot, not a title card,
- free of on-screen text inside the crop. Many tutorials burn subtitles, titles or a channel logo into the video. Text that sits outside the crop is fine; text that would overlap the crop is not. Strongly prefer frames where the piece sits clear of any text,
- the finished piece, not work in progress.
The 3 must be visibly different views (front / side / back, flat / held / styled, or a different setting), never near-duplicates. Rank them: the first is the listing cover, so make it the most appealing, clearest full view.

For each pick give:
- crop: a box in fractions of the frame, [x0, y0, x1, y1] with 0,0 top-left, that contains the whole piece with a comfortable margin of roughly 10% on every side. Code widens it to 4:3, so never cut through the piece to fit a shape.
- text_free_band: [y_top, y_bottom], the vertical fractions of the frame between which there is NO on-screen text, subtitle bar, title or logo across the full width ([0, 1] if the frame has none). Code keeps the crop inside this band.
- clean: true only if the whole piece fits inside the text-free band with no text over it and no blurred hand in front of it.

Return ONLY JSON: {"picks": [{"index": <frame index>, "crop": [x0, y0, x1, y1], "text_free_band": [y_top, y_bottom], "clean": true|false, "reason": "<one sentence>"}], "note": "<one sentence on overall quality, or empty>"}"""


def build(pid: int, project: dict, prog) -> dict:
    pdir = project_dir(pid)
    ldir = pdir / "listing"
    work = ldir / "work"
    shutil.rmtree(work, ignore_errors=True)
    (work / "src").mkdir(parents=True)
    cdir = work / "frames"
    cdir.mkdir()
    wins = _windows(project)
    if not wins:
        raise RuntimeError("No finished-object moments known yet; extract the pattern first")

    warning = None
    cands: list[dict] = []
    prog(f"Listing photos: downloading {len(wins)} finished-object clip(s) at full resolution")
    try:
        secs = _download_sections(project["url"], wins, work / "src")
        if not secs:
            raise RuntimeError("yt-dlp produced no clip files")
        for k, (f, start) in enumerate(secs):
            cands += _sample(f, start, cdir, f"s{k}")
    except Exception as e:
        video = pdir / "video.mp4"
        if not video.exists():
            raise
        warning = f"Full-resolution download failed, so these come from the 720p copy and may be soft ({str(e)[:160]})"
        print(f"[{pid}] listing: {warning}", flush=True)
        for k, (a, b) in enumerate(wins):
            cands += _sample(video, a, cdir, f"v{k}", seek=(a, b))
    if not cands:
        raise RuntimeError("Could not extract any frames for the listing photos")
    src_h = Image.open(cdir / cands[0]["path"]).height

    prog(f"Listing photos: scoring {len(cands)} frames for sharpness and exposure")
    short = _shortlist(cands, cdir)
    _preview(cdir, work / "preview", [c["path"] for c in short])

    prog("Listing photos: choosing the 3 best views of the finished piece")
    title = ((project.get("pattern") or {}).get("title") or project.get("title") or "").strip()
    try:
        data = _choose(short, work / "preview", title)
        picks = [p for p in data.get("picks", []) if isinstance(p.get("index"), int) and 0 <= p["index"] < len(short)]
        note = data.get("note") or ""
    except Exception as e:
        picks, note = [], f"vision choice failed ({e}); used the sharpest distinct frames"
    seen = set()
    picks = [p for p in picks if not (p["index"] in seen or seen.add(p["index"]))][:WANT]
    for i in range(len(short)):
        if len(picks) >= WANT:
            break
        if i not in seen:
            picks.append({"index": i, "crop": None, "reason": "sharpest remaining frame"})
            seen.add(i)

    prog("Listing photos: cropping to 4:3 and correcting colour and sharpness")
    for old in ldir.glob("listing_*.jpg"):
        old.unlink()
    stamp = int(time.time())
    photos = []
    for n, p in enumerate(picks, 1):
        c = short[p["index"]]
        out = ldir / f"listing_{n}_{stamp}.jpg"
        raw = ldir / f"listing_{n}_{stamp}_unedited.jpg"
        info = _finish(cdir / c["path"], p.get("crop"), p.get("text_free_band"), out, raw)
        photos.append({"file": out.name, "unedited": raw.name, "t": c["t"], "reason": p.get("reason", ""),
                       "clean": p.get("clean") is not False, **info})
    shutil.rmtree(work, ignore_errors=True)
    dirty = [str(n) for n, ph in enumerate(photos, 1) if not ph["clean"]]
    if dirty and not warning:
        warning = (f"Photo {', '.join(dirty)} could not avoid on-screen text or a covering hand: no cleaner frame "
                   "exists in this video. Upload your own photo for Etsy, or check it before using it.")
    if src_h < 1080 and not warning:
        warning = f"YouTube only has this video at {src_h}p, so the photos are upscaled and may look soft"
    return {"photos": photos, "source_height": src_h, "made_at": time.time(), "note": note, "warning": warning}
