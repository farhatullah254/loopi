"""Quality review stage: Opus fixes vague or wrong details, then the deterministic checks decide
whether a second, targeted pass is needed. Runs after frames, so the reviewer can see the photos."""
import time

from . import llm, validate

MAX_PASSES = 2


def run(pattern: dict, cleaned: list[dict], pdir, progress=None) -> dict:
    fixes, passes, leftover = [], 0, None
    for _ in range(MAX_PASSES):
        r = llm.review_pattern(pattern, cleaned, pdir, issues=leftover, progress=progress)
        passes += 1
        fixes += r["fixes"]
        leftover = validate.quality(pattern)
        if not leftover:
            break
    pattern["review"] = {"at": time.time(), "passes": passes, "fixes": fixes, "unresolved": leftover or [],
                         "model": llm.model("extract")}
    pattern["validation"] = validate.run(pattern)
    return pattern["review"]
