"""All Claude calls: transcript cleaning, pattern extraction, frame selection."""
import base64
import io
import json
import re
from pathlib import Path

import os

import anthropic

from . import db, engine_cc
from .paths import ROOT

DEFAULT_ENGINE = "claude-code"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Per-job defaults. Claude Code accepts aliases (sonnet / opus / haiku) or full model IDs;
# the API needs full IDs.
JOB_DEFAULTS = {
    "claude-code": {"clean": "claude-opus-5-5", "extract": "claude-opus-5-5", "pick": "claude-opus-5-5"},
    "api": {"clean": "claude-opus-5-5", "extract": "claude-opus-5-5", "pick": "claude-opus-5-5"},
}
EFFORT_DEFAULTS = {"clean": "high", "extract": "high", "pick": "low"}
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]
# Pinned to Opus 5.5 by full ID so an alias change never silently swaps the model mid-library.
# `[1m]` selects the 1M-context variant.
MODEL_CHOICES = {
    "claude-code": ["claude-opus-5-5", "claude-opus-5-5[1m]", "opus", "claude-opus-5", "sonnet", "claude-sonnet-5", "haiku"],
    "api": ["claude-opus-5-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
}

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def engine() -> str:
    e = db.get_setting("engine", DEFAULT_ENGINE) or DEFAULT_ENGINE
    return e if e in JOB_DEFAULTS else DEFAULT_ENGINE


def model(job: str) -> str:
    e = engine()
    return db.get_setting(f"model_{e}_{job}") or JOB_DEFAULTS[e][job]


def effort(job: str) -> str:
    v = db.get_setting(f"effort_{job}") or EFFORT_DEFAULTS[job]
    return v if v in EFFORT_CHOICES else EFFORT_DEFAULTS[job]


def settings() -> dict:
    e = engine()
    return {"engine": e, "models": {j: model(j) for j in ("clean", "extract", "pick")},
            "efforts": {j: effort(j) for j in ("clean", "extract", "pick")}, "effort_choices": EFFORT_CHOICES,
            "choices": MODEL_CHOICES[e], "claude_cli": engine_cc.available(), "claude_token": engine_cc.has_token(),
            "api_key_present": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def glossary() -> str:
    p = ROOT / "glossary.md"
    return p.read_text() if p.exists() else ""


def _text_of(msg) -> str:
    return "".join(b.text for b in msg.content if b.type == "text")


def _complete(system: str, user_content, job: str, max_tokens=64000, image_dir: Path | None = None,
              timeout: int | None = None, max_turns: int = 20) -> str:
    """Route one prompt to the configured engine. `user_content` is a string, or API content blocks
    for the vision jobs (the Claude Code path receives a text prompt naming the files instead)."""
    if engine() == "claude-code":
        prompt = user_content if isinstance(user_content, str) else user_content["cc_prompt"]
        return engine_cc.complete(system, prompt, model(job), effort(job), image_dir=image_dir,
                                  timeout=timeout, max_turns=max_turns)
    content = user_content if isinstance(user_content, str) else user_content["api_content"]
    return _complete_api(system, content, model(job), max_tokens, effort(job))


def _complete_api(system: str, content, mdl: str, max_tokens: int, eff: str = "high") -> str:
    """Streamed SDK completion. Tries the server-side refusal fallback first, drops it if rejected."""
    kwargs = dict(model=mdl, max_tokens=max_tokens, system=system, output_config={"effort": eff},
                  messages=[{"role": "user", "content": content}])
    try:
        with client().beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs) as s:
            msg = s.get_final_message()
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower() and "beta" not in str(e).lower():
            raise
        with client().messages.stream(**kwargs) as s:
            msg = s.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request: " + str(getattr(msg, "stop_details", "")))
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off at max_tokens; try a shorter video segment.")
    return _text_of(msg)


def _json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _ts(t: float) -> str:
    t = int(round(t or 0))
    return f"{t // 60}:{t % 60:02d}"


# ---------------------------------------------------------------- cleaning

CLEAN_SYSTEM = """You are preparing the caption track of a crochet tutorial video for pattern extraction.

You receive YouTube caption cues, each as `start end | text`, with times in seconds. Auto-captions are fragmentary, lowercase, unpunctuated and mishear crochet vocabulary.

Produce clean, complete sentences with faithful timing. Rules:
- Merge fragments into full sentences. A sentence's start is the start of its first fragment; its end is the end of its last fragment.
- Fix speech-recognition errors using crochet knowledge ("single crow shay" = single crochet, "chain to" = chain 2, "slip stitch" etc). Never change a number unless the surrounding words make the mishearing certain.
- Remove fillers (um, like, you know, so yeah), false starts, and repeated words.
- When the creator corrects themself ("do 5... actually 6"), keep only the corrected statement.
- Keep EVERY instruction, stitch count, material, hook size, yarn detail, tip and warning. Do not summarise instructions. Sponsor reads, greetings, channel talk and unrelated chatter are kept but tagged so they can be dropped later.
- Keep emphasis phrases such as "watch closely", "this is the tricky part", "pause here" because they signal difficulty.
- Every sentence gets a kind: instruction | count | materials | tip | structure | meta | chatter | sponsor.
  structure = announcing a new part ("now we start the handle"). meta = intro/outro/about the video. count = a stitch count statement on its own.

Return ONLY JSON: {"sentences": [{"start": 12.3, "end": 15.9, "text": "...", "kind": "instruction"}, ...]}"""


def clean_captions(cues: list[dict], meta: dict, progress=None) -> list[dict]:
    out = []
    chunk = 220
    total = (len(cues) + chunk - 1) // chunk
    for i in range(0, len(cues), chunk):
        part = cues[i:i + chunk]
        if progress:
            progress(f"Cleaning transcript {i // chunk + 1}/{total}")
        lines = "\n".join(f"{c['start']:.2f} {c['end']:.2f} | {c['text']}" for c in part)
        head = f"Video: {meta.get('title')}\nChannel: {meta.get('channel')}\n"
        if i:
            head += "This is a continuation chunk of the same video; the previous chunk ended at " \
                    f"{cues[i - 1]['end']:.1f}s.\n"
        text = _complete(CLEAN_SYSTEM, head + "\nCaption cues:\n" + lines, "clean")
        data = _json(text)
        for s in data.get("sentences", []):
            try:
                out.append({"start": float(s["start"]), "end": float(s["end"]),
                            "text": str(s["text"]).strip(), "kind": s.get("kind", "instruction")})
            except (KeyError, TypeError, ValueError):
                continue
    out.sort(key=lambda s: s["start"])
    return out


# ---------------------------------------------------------------- extraction

EXTRACT_SYSTEM = """You are a senior crochet pattern editor. You turn a cleaned, timestamped tutorial transcript into a written pattern the way a professional designer would publish it, and you decide where the reader needs a photo.

OUTPUT: ONLY a JSON object with this shape:
{
 "title": str, "summary": str, "skill_level": "beginner|easy|intermediate|advanced",
 "terminology_detected": "US|UK|unclear",
 "materials": [{"item": str, "detail": str}],
 "abbreviations": [{"abbr": str, "meaning": str}],
 "gauge": str|null,
 "finished_size": str,
 "yarn_substitution": str,
 "notes": [str],
 "sections": [{"name": str, "steps": [
    {"id": str, "label": str, "kind": "round|row|step|repeat|note",
     "instruction": str, "stitch_count": int|null, "delta": int|null,
     "start": float, "end": float,
     "tier": "none|anchor|sequence|walkthrough", "tier_reason": str,
     "images": [{"t": float, "description": str}]}]}],
 "finishing": [str],
 "final_images": [{"t": float, "description": str}]
}

PATTERN RULES
- ALWAYS write in US terminology. If the creator uses UK terms (dc meaning single crochet, tr meaning double crochet, htr, "treble"), convert every stitch to its US equivalent and set terminology_detected to "UK". Otherwise "US".
- Standard notation: "Rnd 3: *sc, inc* around (18)", "Row 2: ch 1, turn, sc in each st across (24)". Stitch count at the end of every round/row in parentheses when known.
- Collapse repeats: "Rnds 6-20: sc in each st around (36)" is one step with kind "repeat".
- stitch_count is the count at the END of that round/row. delta is stitch_count minus the previous round/row's count in the same section; null for the first, null when unknown.
- Never invent a number. If the creator never states a count and it cannot be derived with certainty from the instruction, set stitch_count to null and write the instruction so it works without one.
- Sections follow the object's parts (Base, Body, Handle, Strap, Flap, Assembly...). Use the creator's chapter names when they exist.
- Every abbreviation used in any instruction must appear in "abbreviations".
- materials: yarn (weight category, fibre/texture, colour), hook size in mm with the US letter/number, safety eye or button sizes in mm, stuffing, notions. Use exactly what the creator or the video description states. When a detail is NOT stated, never write "of your choice", "sized to suit", "a size smaller than the label" or any other non-answer: infer the most likely value from what the creator says about the yarn and the object (and standard practice for that yarn) and mark it "approx." (e.g. "Super bulky (6) chenille / velvet plush yarn, approx." and "6 mm (J-10) hook, approx."). Yarn AMOUNT (grams, yards, skeins) only if the creator gives one; otherwise leave quantity out entirely, never estimate it.
- finished_size: the finished measurements. Stated size wins; otherwise estimate it from the yarn weight, hook and the stitch counts and write it as "Approx. 8 in (20 cm) across the legs, 5 in (13 cm) tall with the yarn and hook listed". Never empty.
- yarn_substitution: 1-2 sentences specific to THIS yarn: what to substitute with, which hook to pair it with, and what changes (size, texture, stitch visibility) if the reader uses a different weight. Never empty.
- COUNTS ARE NEVER OPEN-ENDED. Do not write "as many rounds as you want", "about 10 sts", "2 or 3 rounds", "a few rounds", or "one decrease round spacing the decreases evenly" without the actual repeat. Give the exact number the creator used (or the one the stitch counts and photos prove), and if the creator offers flexibility add it as an option after the exact instruction: "Rnds 3-5: sc in each st around (16). For longer legs, add 1-2 more rounds." A label must match its instruction: "Rnds 3-5" is exactly three rounds.
- Placement steps (eyes, ears, limbs, joins) give positions in rounds and stitches: "between Rnds 8 and 9, 4 sts apart".
- Ignore sponsor reads, greetings and chatter entirely.
- start/end of a step are the transcript times of when it is being taught.

IMAGE RULES - decide per step how much the reader needs to SEE, not per pattern
- none: repeat rounds, counting, notes, anything already shown identically before.
- anchor (1 image): plain rounds where one photo of the finished round suffices.
- sequence (2-3 images): a new stitch, an increase/decrease rhythm, a colour change, joining a new piece.
- walkthrough (4-6 images): magic ring, first V-stitch, foundation chain into a ring, joins, seams, assembly, anything the creator slows down for, repeats, or flags as tricky. Time spent per stitch in the transcript is a strong signal: 40 seconds on 6 stitches means walkthrough.
- The DENSITY setting shifts every tier: "concise" one tier down (walkthrough->sequence, anchor->none), "beginner" one tier up, "standard" unchanged.
- images[].t is the exact second where the described state is visible on screen. Speech precedes the result: the finished stitches usually appear 2-6 seconds after the words. For sequence/walkthrough spread the timestamps evenly across the step's window so they read as a progression. description says precisely what the frame should show ("6 dc completed in the 3rd chain, hook resting, stitch marker in first dc").
- final_images: 2-4 timestamps late in the video where the finished object is shown whole (often the intro too). These become the cover and the finishing section.

VOICE - the pattern is a standalone document
- NEVER mention the video, the tutorial, the creator, the camera, "on screen", "not shown", "not demonstrated", "not stated" or "the transcript" anywhere in the pattern text: not in steps, notes, finishing or the summary. The reader has only this PDF.
- When the creator skips something, DECIDE what kind of gap it is before writing anything:
  a) A standard skill every crocheter has (fasten off, weave in ends, magic ring, ch 1 and turn, sl st join, blocking): one short line at most, often nothing. Never a paragraph.
  b) A component that is usually bought or chosen rather than crocheted (straps, chain straps, D-rings, swivel clips, zips, magnetic snaps, buttons, linings, cord, ribbon): do not write a crochet procedure for it. Name it as the maker's choice in one line ("attach a strap of your choice to the top edge with a yarn needle and matching yarn, or clip on a purchased strap") and list it under materials as optional.
  c) A genuinely pattern-specific step the reader cannot guess (a shaping run, an unusual join, how two pieces line up): write it out properly.
  If in doubt, say less. A pattern that trusts the reader reads better than one that explains how to weave in ends.
- When the creator leaves a NUMBER to the maker (length, width, number of rounds), write it as the maker's choice with a sensible way to decide it: "work rows until the strap measures your preferred length, about 2 in shorter than the finished length you want since dc stretches". Do not say that the number was not given.
- A stitch count that cannot be derived stays null in stitch_count, but the instruction text never says the count is unknown; write the round so it works regardless ("dc in each st around").
- Notes are craft notes for the reader (tension, curling, seam placement, sizing), not editorial notes about the source.

Ground the instructions in the transcript; fill only routine gaps with standard practice, and never invent a number the creator did not give."""


def extract_pattern(cleaned: list[dict], meta: dict, density: str, progress=None) -> dict:
    if progress:
        progress("Extracting the pattern")
    keep = [s for s in cleaned if s.get("kind") not in ("chatter", "sponsor")]
    lines = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] ({s['kind']}) {s['text']}" for s in keep)
    chapters = "\n".join(f"  {_ts(c.get('start_time', 0))} {c.get('title')}" for c in meta.get("chapters") or [])
    user = (f"Video title: {meta.get('title')}\nChannel: {meta.get('channel')}\n"
            f"Duration: {_ts(meta.get('duration') or 0)}\nDensity setting: {density}\n"
            f"Creator chapters:\n{chapters or '  (none)'}\n\n"
            f"Video description (first 1500 chars):\n{(meta.get('description') or '')[:1500]}\n\n"
            f"Glossary and conventions:\n{glossary()}\n\n"
            f"Cleaned transcript:\n{lines}")
    text = _complete(EXTRACT_SYSTEM, user, "extract")
    pattern = _json(text)
    _normalise(pattern)
    return pattern


def _normalise(p: dict):
    p.setdefault("sections", [])
    p.setdefault("materials", [])
    p.setdefault("abbreviations", [])
    p.setdefault("notes", [])
    p.setdefault("finishing", [])
    p.setdefault("final_images", [])
    n = 0
    for si, sec in enumerate(p["sections"]):
        sec.setdefault("steps", [])
        for st in sec["steps"]:
            n += 1
            st["id"] = st.get("id") or f"s{si + 1}-{n}"
            st.setdefault("tier", "none")
            st.setdefault("images", [])
            st.setdefault("kind", "step")
            for img in st["images"]:
                img.setdefault("candidates", [])
                img.setdefault("chosen", None)
                img.setdefault("manual", False)
    for img in p["final_images"]:
        img.setdefault("candidates", [])
        img.setdefault("chosen", None)
        img.setdefault("manual", False)


# ---------------------------------------------------------------- frame choice

PICK_SYSTEM = """You judge video frames for a crochet pattern PDF. You get several frames from the same few seconds of a tutorial, each labelled with its index and time, plus a description of what the frame should show.

Pick the frame that best shows the described state of the work: crochet clearly visible, in focus, hands not covering it, not a face-cam shot, not a mid-motion blur, not a title card. Prefer the frame where the step looks COMPLETED over one mid-stitch unless the description asks for mid-stitch. If none is acceptable, still pick the least bad and say so.

Return ONLY JSON: {"choice": <index>, "quality": "good|ok|poor", "reason": "<one sentence>"}"""


def _img_block(path: Path, max_w=900) -> dict:
    from PIL import Image
    im = Image.open(path)
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=80)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def pick_frame(candidates: list[dict], frames_dir: Path, description: str) -> dict:
    api_content = []
    names = []
    for i, c in enumerate(candidates):
        api_content.append({"type": "text", "text": f"Frame {i} at {_ts(c['t'])}:"})
        api_content.append(_img_block(frames_dir / c["path"]))
        names.append(f"Frame {i} at {_ts(c['t'])}: ./{c['path']}")
    api_content.append({"type": "text", "text": f"Wanted: {description}"})
    cc_prompt = ("Open every one of these image files with the Read tool, then judge them.\n"
                 + "\n".join(names) + f"\n\nWanted: {description}")
    text = _complete(PICK_SYSTEM, {"api_content": api_content, "cc_prompt": cc_prompt}, "pick",
                     max_tokens=2000, image_dir=frames_dir)
    data = _json(text)
    idx = int(data.get("choice", 0))
    idx = min(max(idx, 0), len(candidates) - 1)
    return {"index": idx, "quality": data.get("quality", "ok"), "reason": data.get("reason", "")}


# ---------------------------------------------------------------- voice polish

POLISH_SYSTEM = """You are a crochet pattern editor doing a wording pass on an already-extracted pattern. The structure, step ids, images and timestamps are fixed; you only rewrite text.

Rules (the pattern must read as a standalone document):
- Remove every reference to the video, tutorial, creator, camera, screen, transcript, or to something being "not shown", "not demonstrated", "not stated", "never states".
- Where something was skipped, decide what kind of gap it is: (a) a standard skill every crocheter has (fasten off, weave in ends, magic ring, turning chains, sl st join) gets one short line at most, often nothing; (b) a component usually bought or chosen rather than crocheted (straps, chain straps, D-rings, clips, zips, snaps, buttons, linings, cord, ribbon) gets one line naming it as the maker's choice, never a crochet procedure; (c) only a genuinely pattern-specific step the reader cannot guess gets written out. If in doubt, say less.
- Trim over-explanation you find: a paragraph on weaving in ends or a multi-line recipe for a plain strap becomes one line.
- Where a number is the maker's choice, phrase it as a choice with a sensible way to decide it. Never say the number was not given. Never invent a stitch count.
- Notes become craft notes for the reader (tension, curling, seam placement, sizing). Drop notes that were only about the source.
- Keep everything else word for word. US terminology, standard notation.

- Materials: a detail that was never given becomes a concrete, most-likely value marked "approx." (yarn weight category and type, hook size in mm with US size, eye sizes in mm). Never "of your choice", "sized to suit", "not stated". Yarn amount (grams, yards, skeins) appears only if the creator gave one; remove any estimated amount.

Return ONLY JSON:
{"summary": str, "notes": [str], "finishing": [str], "materials": [{"item": str, "detail": str}], "steps": [{"id": str, "instruction": str}]}
Include a steps entry ONLY for steps whose instruction you changed."""


def polish_voice(pattern: dict, progress=None) -> dict:
    if progress:
        progress("Rewording the pattern")
    slim = {"summary": pattern.get("summary"), "notes": pattern.get("notes", []),
            "finishing": pattern.get("finishing", []), "materials": pattern.get("materials", []),
            "sections": [{"name": sec.get("name"),
                          "steps": [{"id": st["id"], "label": st.get("label"), "instruction": st.get("instruction")}
                                    for st in sec.get("steps", [])]} for sec in pattern.get("sections", [])]}
    text = _complete(POLISH_SYSTEM, "Pattern:\n" + json.dumps(slim, ensure_ascii=False, indent=1), "extract")
    data = _json(text)
    if isinstance(data.get("summary"), str):
        pattern["summary"] = data["summary"]
    if isinstance(data.get("notes"), list):
        pattern["notes"] = [str(n) for n in data["notes"]]
    if isinstance(data.get("finishing"), list):
        pattern["finishing"] = [str(f) for f in data["finishing"]]
    if isinstance(data.get("materials"), list) and data["materials"]:
        pattern["materials"] = [{"item": str(m.get("item", "")), "detail": str(m.get("detail", ""))} for m in data["materials"]]
    changed = {s.get("id"): s.get("instruction") for s in data.get("steps", []) if s.get("id")}
    n = 0
    for sec in pattern.get("sections", []):
        for st in sec.get("steps", []):
            if st["id"] in changed and changed[st["id"]]:
                st["instruction"] = changed[st["id"]]
                n += 1
    return {"steps_changed": n}


# ---------------------------------------------------------------- quality review

REVIEW_SYSTEM = """You are the final technical editor of a crochet pattern before it is sold as a PDF. A paying customer must be able to buy the right yarn and hook and follow every round without guessing. Real customer complaint you are preventing: "the pattern is vague about hook size, yarn, and some instructions".

You receive the pattern JSON, the full cleaned transcript of the source tutorial (with the creator's own words about yarn, hook, sizes and counts), the video description when available, and photos of the work at each step and of the finished object. Open every listed image file with the Read tool before you answer; the photos show the yarn texture and thickness, the hook, eye sizes and the true shape of colour changes.

Review against this checklist and FIX every problem you find:
1. Materials. Yarn: weight category with its number (e.g. "Super bulky (6)"), fibre/texture (chenille, velvet plush, cotton, acrylic), and colours. Hook: size in mm plus US size. Safety eyes/buttons: sizes in mm and how many of each. Stuffing and notions. Source priority: (a) stated in the transcript or description -> use it exactly; (b) not stated -> infer the most likely value from the photos and transcript and write it with "approx." Never "of your choice", "weight of your choice", "sized to suit", "a size smaller than the label suggests", "not stated". Never estimate yarn quantity unless the creator gave one.
2. finished_size: stated size wins; otherwise estimate from yarn weight, hook and stitch counts, in inches and cm, marked "Approx.". Never empty.
3. yarn_substitution: 1-2 sentences specific to this yarn: what to substitute with, which hook to pair it with, what changes if a different weight is used. Never empty.
4. gauge: keep a stated gauge; otherwise for toys/amigurumi write one practical line ("Not critical; work tightly enough that stuffing does not show. Using the yarn and hook above gives the finished size stated."), for fitted items an approximate gauge marked approx.
5. Open-ended instructions: replace "as many rounds as you want", "about N sts", "2 or 3 rounds", "a few", "one decrease round spacing the decreases evenly" (without the repeat), "work until it looks right" with the exact numbers the creator used, derived from the transcript, the stitch counts and the photos. Flexibility becomes an optional extra sentence AFTER the exact instruction. When a round/row count is inferred from the photos rather than stated, also give the measurement it should reach so the reader can check it ("Rnds 3-18: sc around (16). The ear measures approx. 5 in / 13 cm.").
6. Every round/row has its ending stitch count; repeat arithmetic is right (e.g. *3 sc, dec* on 40 sts gives 32); a range label matches its instruction ("Rnds 3-5" is three rounds); stitch_count and delta fields agree with the text.
7. Placement: eyes, limbs, ears, fangs, joins say which rounds and how many stitches apart/where.
8. Text vs photos: if a stitch count, shape or placement in the text contradicts what the photos clearly show, correct the text, and fix image captions that describe something else. COLOUR IS OUT OF SCOPE: leave colour choices and colour-change wording exactly as written, and never report colour as a problem.
9. Text hygiene: words run together ("inthe", "anoval"), doubled words, an instruction that repeats its own label ("Rnd 2: inc..." when the label is already "Rnd 2"), typos, inconsistent abbreviations, abbreviations used but not defined.
10. Voice: standalone document, US terms, no mention of video/tutorial/creator/camera/transcript.

Do not add or remove steps and do not touch image timestamps. You may change a step's label, instruction, stitch_count and delta, and an image caption.

Return ONLY JSON:
{"fixes": [{"where": str, "problem": str, "fix": str}],
 "summary": str, "materials": [{"item": str, "detail": str}], "gauge": str|null,
 "finished_size": str, "yarn_substitution": str, "notes": [str], "finishing": [str],
 "abbreviations": [{"abbr": str, "meaning": str}],
 "steps": [{"id": str, "label": str, "instruction": str, "stitch_count": int|null, "delta": int|null,
            "captions": [{"index": int, "caption": str}]}]}
Return every top-level field in full. Include a steps entry ONLY for steps you changed (captions only when changed). "fixes" lists every change in plain words for the shop owner, one entry per problem."""

REVIEW_MAX_IMAGES = 18


def _review_images(pattern: dict, pdir: Path) -> list[tuple[str, str]]:
    """(relative path, label) for the chosen frames the reviewer should look at: every finished-object
    image plus step frames spread evenly across the pattern."""
    finals, steps = [], []
    for k, img in enumerate(pattern.get("final_images", [])):
        if img.get("chosen"):
            sub = "uploads" if img.get("manual") else "frames"
            finals.append((f"{sub}/{img['chosen']}", f"finished object {k + 1}"))
    for sec in pattern.get("sections", []):
        for st in sec.get("steps", []):
            if st.get("tier") == "none":
                continue
            for k, img in enumerate(st.get("images", [])):
                if img.get("chosen"):
                    sub = "uploads" if img.get("manual") else "frames"
                    steps.append((f"{sub}/{img['chosen']}", f"{sec.get('name')} / {st.get('label')} image {k}"))
    room = max(0, REVIEW_MAX_IMAGES - len(finals))
    if len(steps) > room:
        stride = len(steps) / room if room else 1
        steps = [steps[int(i * stride)] for i in range(room)]
    return [x for x in finals + steps if (pdir / x[0]).exists()]


def _review_payload(pattern: dict) -> dict:
    return {k: pattern.get(k) for k in ("title", "summary", "skill_level", "materials", "gauge", "finished_size",
                                         "yarn_substitution", "abbreviations", "notes", "finishing")} | {
        "sections": [{"name": sec.get("name"), "steps": [
            {"id": st["id"], "label": st.get("label"), "kind": st.get("kind"), "instruction": st.get("instruction"),
             "stitch_count": st.get("stitch_count"), "delta": st.get("delta"),
             "captions": [i.get("caption") or i.get("description") or "" for i in st.get("images", [])]}
            for st in sec.get("steps", [])]} for sec in pattern.get("sections", [])]}


def _apply_review(pattern: dict, data: dict) -> int:
    for f in ("summary", "finished_size", "yarn_substitution"):
        if isinstance(data.get(f), str) and data[f].strip():
            pattern[f] = data[f].strip()
    if "gauge" in data and (data["gauge"] is None or isinstance(data["gauge"], str)):
        pattern["gauge"] = data["gauge"] or pattern.get("gauge")
    for f in ("notes", "finishing"):
        if isinstance(data.get(f), list):
            pattern[f] = [str(x) for x in data[f] if str(x).strip()]
    if isinstance(data.get("materials"), list) and data["materials"]:
        pattern["materials"] = [{"item": str(m.get("item", "")), "detail": str(m.get("detail", ""))}
                                for m in data["materials"] if isinstance(m, dict)]
    if isinstance(data.get("abbreviations"), list) and data["abbreviations"]:
        pattern["abbreviations"] = [{"abbr": str(a.get("abbr", "")), "meaning": str(a.get("meaning", ""))}
                                    for a in data["abbreviations"] if isinstance(a, dict)]
    changed = {s.get("id"): s for s in data.get("steps", []) if isinstance(s, dict) and s.get("id")}
    n = 0
    for sec in pattern.get("sections", []):
        for st in sec.get("steps", []):
            new = changed.get(st["id"])
            if not new:
                continue
            for f in ("label", "instruction"):
                if isinstance(new.get(f), str) and new[f].strip():
                    st[f] = new[f].strip()
            for f in ("stitch_count", "delta"):
                if f in new and (new[f] is None or isinstance(new[f], int)):
                    st[f] = new[f]
            for c in new.get("captions") or []:
                try:
                    st["images"][int(c["index"])]["caption"] = str(c["caption"])
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            n += 1
    return n


def review_pattern(pattern: dict, cleaned: list[dict], pdir: Path, issues: list[str] | None = None,
                   progress=None) -> dict:
    """One quality-review pass: Opus reads the pattern, transcript and photos, fixes problems in place,
    and returns the list of fixes it made. `issues` are leftover validator flags for a second pass."""
    imgs = _review_images(pattern, pdir)
    keep = [s for s in cleaned or [] if s.get("kind") not in ("chatter", "sponsor")]
    transcript = "\n".join(f"[{s['start']:.0f}s] ({s.get('kind')}) {s['text']}" for s in keep)
    desc = (pattern.get("source") or {}).get("description") or ""
    head = ("Pattern JSON:\n" + json.dumps(_review_payload(pattern), ensure_ascii=False, indent=1)
            + (f"\n\nVideo description:\n{desc[:3000]}" if desc else "")
            + f"\n\nCleaned transcript:\n{transcript or '(not available)'}\n\nGlossary:\n{glossary()}")
    if issues:
        head += ("\n\nAn automatic check still flags these problems after the previous review. Fix every one:\n- "
                 + "\n- ".join(issues))
    api_content = [{"type": "text", "text": head}]
    for path, label in imgs:
        api_content.append({"type": "text", "text": f"Photo: {label}"})
        api_content.append(_img_block(pdir / path))
    cc_prompt = head
    if imgs:
        cc_prompt += ("\n\nOpen every one of these photos with the Read tool before answering:\n"
                      + "\n".join(f"./{path} ({label})" for path, label in imgs))
    if progress:
        progress("Reviewing the pattern for vague or wrong details" + (" (second pass)" if issues else ""))
    text = _complete(REVIEW_SYSTEM, {"api_content": api_content, "cc_prompt": cc_prompt}, "extract",
                     image_dir=pdir if imgs else None, timeout=30 * 60, max_turns=60)
    data = _json(text)
    n = _apply_review(pattern, data)
    fixes = [{"where": str(f.get("where", "")), "problem": str(f.get("problem", "")), "fix": str(f.get("fix", ""))}
             for f in data.get("fixes", []) if isinstance(f, dict)]
    return {"fixes": fixes, "steps_changed": n, "images_seen": len(imgs)}
