"""Deterministic checks on an extracted pattern. Flags, never fixes."""
import re

ABBR_RE = re.compile(r"\b(sc|dc|hdc|tr|dtr|sl\s?st|ch|inc|dec|blo|flo|sk|st|sts|rep|yo|mr|fpdc|bpdc|"
                     r"sc2tog|dc2tog|hdc2tog|puff|bobble|popcorn|picot|tc|trc)\b", re.I)


SOURCE_RE = re.compile(r"\b(the video|this video|tutorial|the creator|on camera|on screen|not shown|not demonstrated|"
                       r"not stated|never states|the transcript|in the video)\b", re.I)


def _source_refs(pattern: dict):
    found = []
    texts = [("summary", pattern.get("summary") or "")]
    texts += [("notes", n) for n in pattern.get("notes", [])]
    texts += [("finishing", f) for f in pattern.get("finishing", [])]
    texts += [("materials", f"{m.get('item', '')} {m.get('detail', '')}") for m in pattern.get("materials", [])]
    for sec in pattern.get("sections", []):
        for st in sec.get("steps", []):
            texts.append((st.get("label") or st.get("id"), st.get("instruction") or ""))
    for where, t in texts:
        m = SOURCE_RE.search(t or "")
        if m:
            found.append((where, m.group(0)))
    return found


VAGUE_RE = re.compile(
    r"\b(?:weight of your choice|sized to suit|(?:a )?size smaller than the (?:yarn )?label|"
    r"as many (?:\w+ )?(?:rounds|rows|sts|stitches)? ?as you (?:want|like|wish)|"
    r"about \d+ (?:sts?|stitches|rounds?|rows?)\b|\d+ or \d+ (?:\w+ )?(?:rounds?|rows?|sts?|stitches)\b|"
    r"a few (?:rounds|rows|stitches|sts)|spacing the decreases evenly|until it looks right)", re.I)
CHOICE_RE = re.compile(r"(?:yarn|hook|weight|eyes?)[^.;]{0,40}\bof your choice|of your choice[^.;]{0,25}\b(?:yarn|hook|weight)", re.I)
YARN_WEIGHT_RE = re.compile(r"\b(?:lace|fingering|sock|sport|dk|light worsted|worsted|aran|chunky|bulky|jumbo|"
                            r"super bulky|\(\d\)|#\d|\d+[- ]?ply|weight \d)\b", re.I)
MM_RE = re.compile(r"\d(?:\.\d+)?\s?mm\b", re.I)


def quality(pattern: dict) -> list[dict]:
    """Checks for the things a buyer cannot guess. level 'qa' so the review pass can target them."""
    out = []
    mats = pattern.get("materials", [])
    text_of = lambda m: f"{m.get('item', '')} {m.get('detail', '')}"
    yarns = [m for m in mats if re.search(r"\byarn\b", m.get("item", ""), re.I)
             and not re.search(r"needle|bobbin|marker", m.get("item", ""), re.I)
             and not re.search(r"same (?:weight|type|yarn)", m.get("detail", ""), re.I)]
    hooks = [m for m in mats if re.search(r"\bhook\b", m.get("item", ""), re.I)]
    if not hooks:
        out.append("Materials: no crochet hook listed")
    for m in hooks:
        if not MM_RE.search(text_of(m)):
            out.append(f"Materials: hook has no size in mm (\"{text_of(m).strip()}\")")
    if not yarns:
        out.append("Materials: no yarn listed")
    for m in yarns:
        if not YARN_WEIGHT_RE.search(text_of(m)):
            out.append(f"Materials: yarn has no weight category (\"{text_of(m).strip()}\")")
    for m in mats:
        if re.search(r"\b(?:safety )?eyes?\b", m.get("item", ""), re.I) and not MM_RE.search(text_of(m)):
            out.append(f"Materials: eye size not given in mm (\"{text_of(m).strip()}\")")
    if not (pattern.get("finished_size") or "").strip():
        out.append("No finished size")
    if not (pattern.get("yarn_substitution") or "").strip():
        out.append("No yarn substitution note")
    texts = [("summary", pattern.get("summary") or ""), ("gauge", pattern.get("gauge") or "")]
    texts += [("materials", text_of(m)) for m in mats]
    texts += [("notes", n) for n in pattern.get("notes", [])]
    texts += [("finishing", f) for f in pattern.get("finishing", [])]
    for sec in pattern.get("sections", []):
        for st in sec.get("steps", []):
            texts.append((f"{sec.get('name')} {st.get('label') or st.get('id')}", st.get("instruction") or ""))
    for where, t in texts:
        for rx in (VAGUE_RE, CHOICE_RE):
            m = rx.search(t)
            if m and not re.search(r"colou?r(?:s|way)? of your choice", m.group(0), re.I):
                out.append(f"{where}: vague wording \"{m.group(0)}\"; give the exact value")
    return out


def run(pattern: dict) -> dict:
    issues = []
    for msg in quality(pattern):
        issues.append({"step_id": None, "level": "warn", "kind": "qa", "msg": msg})
    for where, word in _source_refs(pattern):
        issues.append({"step_id": None, "level": "warn",
                       "msg": f"{where}: mentions the source (\"{word}\"); the pattern should stand alone. Re-extract or edit."})
    defined = {a.get("abbr", "").lower().replace(" ", "") for a in pattern.get("abbreviations", [])}
    used = set()
    for sec in pattern.get("sections", []):
        prev = None
        prev_label = None
        for st in sec.get("steps", []):
            sid = st.get("id")
            instr = st.get("instruction") or ""
            for m in ABBR_RE.finditer(instr):
                used.add(m.group(1).lower().replace(" ", ""))
            count = st.get("stitch_count")
            delta = st.get("delta")
            if st.get("kind") in ("round", "row"):
                if count is None:
                    issues.append({"step_id": sid, "level": "warn", "msg": f"{st.get('label')}: no stitch count"})
                elif prev is not None and delta is not None and prev + delta != count:
                    issues.append({"step_id": sid, "level": "error",
                                   "msg": f"{st.get('label')}: {prev_label} had {prev}, delta {delta:+d}, "
                                          f"expected {prev + delta} but count says {count}"})
                elif prev is not None and delta is None and count != prev:
                    issues.append({"step_id": sid, "level": "warn",
                                   "msg": f"{st.get('label')}: count changed {prev} to {count} with no stated increase/decrease"})
                if count is not None:
                    prev, prev_label = count, st.get("label")
            if not instr.strip():
                issues.append({"step_id": sid, "level": "error", "msg": f"{st.get('label')}: empty instruction"})
            if st.get("start") is None or st.get("end") is None:
                issues.append({"step_id": sid, "level": "warn", "msg": f"{st.get('label')}: missing timestamps"})
        labels = [s.get("label", "") for s in sec.get("steps", []) if s.get("kind") in ("round", "row")]
        nums = [int(m.group(1)) for l in labels for m in [re.search(r"(\d+)", l or "")] if m]
        for a, b in zip(nums, nums[1:]):
            if b not in (a, a + 1) and b > a + 1:
                issues.append({"step_id": None, "level": "warn",
                               "msg": f"{sec.get('name')}: numbering jumps from {a} to {b}"})
    missing = sorted(u for u in used - defined if u not in ("st", "sts", "rep", "sk"))
    if missing:
        issues.append({"step_id": None, "level": "warn",
                       "msg": "Abbreviations used but not defined: " + ", ".join(missing)})
    if pattern.get("terminology_detected") == "UK":
        issues.append({"step_id": None, "level": "info",
                       "msg": "Video used UK terms; pattern has been converted to US terms. Spot-check dc/tr."})
    return {"issues": issues, "errors": sum(1 for i in issues if i["level"] == "error"),
            "warnings": sum(1 for i in issues if i["level"] == "warn")}
