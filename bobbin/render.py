"""Pattern JSON -> HTML -> PDF (headless Chromium via Playwright), with brand settings,
version records and a copy in the library folder."""
import html
import re
import shutil
import time
from pathlib import Path

from . import db, llm
from .paths import BRAND_DIR, LIBRARY_DIR, TEMPLATES_DIR, project_dir


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def brand() -> dict:
    logo = db.get_setting("brand_logo")
    return {"name": db.get_setting("brand_name", "") or "",
            "footer": db.get_setting("brand_footer", "") or "",
            "logo": logo if logo and (BRAND_DIR / logo).exists() else None}


def _frame_src(pdir: Path, img: dict) -> str | None:
    if not img.get("chosen"):
        return None
    sub = "uploads" if img.get("manual") else "frames"
    return (pdir / sub / img["chosen"]).as_uri()


def cover_image(project: dict) -> str | None:
    """Uploaded featured image, else the YouTube thumbnail, else the best final frame."""
    pdir = project_dir(project["id"])
    up = project.get("uploads") or {}
    if up.get("featured"):
        return (pdir / "uploads" / up["featured"][0]).as_uri()
    if project.get("thumb_file") and (pdir / project["thumb_file"]).exists():
        return (pdir / project["thumb_file"]).as_uri()
    finals = project.get("pattern", {}).get("final_images", [])
    good = sorted(finals, key=lambda i: {"good": 0, "ok": 1}.get(i.get("quality"), 2))
    for i in good:
        s = _frame_src(pdir, i)
        if s:
            return s
    return None


def final_images(project: dict) -> list[str]:
    """Uploaded finals win; AI frames only when none uploaded or explicitly kept."""
    pdir = project_dir(project["id"])
    up = project.get("uploads") or {}
    out = [(pdir / "uploads" / f).as_uri() for f in up.get("final", [])[:2]]
    if not out or up.get("keep_ai_finals"):
        out += [s for s in (_frame_src(pdir, i) for i in project["pattern"].get("final_images", [])) if s]
    return out


def _grid(srcs: list[str], caps: list[str]) -> str:
    """Rows of up to three, balanced so 4 becomes 2+2 and 5 becomes 3+2, never a lone orphan."""
    n = len(srcs)
    if n == 0:
        return ""
    rows = {1: [1], 2: [2], 3: [3], 4: [2, 2], 5: [3, 2], 6: [3, 3]}.get(n) or [3] * (n // 3) + ([n % 3] if n % 3 else [])
    if len(rows) > 1 and rows[-1] == 1:
        rows[-2], rows[-1] = rows[-2] - 1, 2
    out, i = [], 0
    for r in rows:
        cls = {1: "one", 2: "two", 3: "three"}[r]
        out.append(f"<div class='imgs {cls}'>")
        for s, c in zip(srcs[i:i + r], caps[i:i + r]):
            out.append(f"<figure><img src='{s}'>" + (f"<figcaption>{_e(c)}</figcaption>" if c else "") + "</figure>")
        out.append("</div>")
        i += r
    return "".join(out)


def build_html(project: dict) -> str:
    p = project["pattern"]
    pdir = project_dir(project["id"])
    b = brand()
    css = (TEMPLATES_DIR / "pattern.css").read_text()
    cover = cover_image(project)
    author = b["name"]
    logo = (BRAND_DIR / b["logo"]).as_uri() if b["logo"] else None

    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(p.get('title'))}</title>"
           f"<style>{css}</style></head><body>"]

    # ---- cover page
    out.append("<section class='cover'>")
    if cover:
        out.append(f"<div class='hero-wrap'><img class='hero' src='{cover}'></div>")
    out.append(f"<h1>{_e(p.get('title'))}</h1>")
    if p.get("summary"):
        out.append(f"<p class='summary'>{_e(p['summary'])}</p>")
    meta = []
    if p.get("skill_level"):
        meta.append(f"<span class='pill'>Skill level: {_e(p['skill_level']).title()}</span>")
    out.append("<p class='meta'>" + " ".join(meta) + "</p>")
    if logo or author:
        out.append("<div class='byline'>" + (f"<img class='logo' src='{logo}'>" if logo else "")
                   + (f"<span>{_e(author)}</span>" if author else "") + "</div>")
    out.append("</section>")

    # ---- front matter
    out.append("<section class='front'>")
    if p.get("materials"):
        out.append("<h2>Materials</h2><ul class='materials'>")
        for m in p["materials"]:
            out.append(f"<li><b>{_e(m.get('item'))}</b>"
                       + (f" &mdash; {_e(m['detail'])}" if m.get("detail") else "") + "</li>")
        out.append("</ul>")
    if p.get("finished_size"):
        out.append(f"<h2>Finished size</h2><p>{_e(p['finished_size'])}</p>")
    if p.get("gauge"):
        out.append(f"<h2>Gauge</h2><p>{_e(p['gauge'])}</p>")
    if p.get("yarn_substitution"):
        out.append(f"<h2>Yarn substitution</h2><p>{_e(p['yarn_substitution'])}</p>")
    if p.get("abbreviations"):
        out.append("<h2>Abbreviations</h2><table class='abbr'>")
        for a in p["abbreviations"]:
            out.append(f"<tr><td class='k'>{_e(a.get('abbr'))}</td><td>{_e(a.get('meaning'))}</td></tr>")
        out.append("</table>")
    if p.get("notes"):
        out.append("<h2>Notes</h2><ul class='notes'>")
        for n in p["notes"]:
            out.append(f"<li>{_e(n)}</li>")
        out.append("</ul>")
    out.append("</section>")

    # ---- pattern body
    for sec in p.get("sections", []):
        out.append("<section class='part'>")
        heading = f"<h2>{_e(sec.get('name'))}</h2>"
        if not sec.get("steps"):
            out.append(heading)
        for k, st in enumerate(sec.get("steps", [])):
            imgs = st.get("images", []) if st.get("tier") != "none" else []
            srcs = [s for s in (_frame_src(pdir, i) for i in imgs) if s]
            caps = [i.get("caption") or i.get("description") or "" for i in imgs if _frame_src(pdir, i)]
            label = st.get("label") or ""
            instr = st.get("instruction") or ""
            # "Rnd 2: Rnd 2: inc..." or "Rnd 3 (joining rnd): Rnd 3: ..." -> the label is printed once
            dup = re.match(r"((?:rnds?|rows?)\s*\d+(?:\s*[-–]\s*\d+)?)\s*:\s*", instr, re.I)
            if dup and label.lower().replace(" ", "").startswith(dup.group(1).lower().replace(" ", "")):
                instr = instr[dup.end():]
            count = st.get("stitch_count")
            count_html = f" <span class='count'>({count})</span>" if count is not None and f"({count})" not in instr else ""
            if st.get("kind") == "note":
                para = f"<p class='note'>{_e(instr)}</p>"
            else:
                para = f"<p class='instr'><b>{_e(label)}:</b> {_e(instr)}{count_html}</p>"
            if k == 0:
                # Heading and first instruction travel as one unbreakable block; break-after on the
                # heading alone makes Chromium print the two on top of each other at a page top.
                para = f"<div class='head-keep'>{heading}{para}</div>"
            out.append(f"<div class='step{' has-img' if srcs else ''}'>{para}{_grid(srcs, caps)}</div>")
        out.append("</section>")

    # ---- finishing
    finals = final_images(project)
    if p.get("finishing") or finals:
        out.append("<section class='part'><h2>Finishing</h2>")
        if p.get("finishing"):
            out.append("<ol>" + "".join(f"<li>{_e(f)}</li>" for f in p["finishing"]) + "</ol>")
        if finals:
            out.append("<div class='finals'>" + _grid(finals, [""] * len(finals)) + "</div>")
        out.append("</section>")
    if b["footer"]:
        out.append(f"<footer>{_e(b['footer'])}</footer>")
    out.append("</body></html>")
    return "\n".join(out)


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:80] or "pattern"


def _install_browser():
    """macOS empties ~/Library/Caches under disk pressure, which deletes Playwright's browser. Put it back."""
    import subprocess
    import sys
    print("PDF engine missing (cache was cleared); reinstalling it once", flush=True)
    p = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium-headless-shell"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    if p.returncode != 0:
        raise RuntimeError("The PDF engine is missing and reinstalling it failed. In Terminal run: "
                           "cd ~/Downloads/Workspace/AGENTS_NEST/LOOPI && .venv/bin/python -m playwright install "
                           "chromium-headless-shell. Details: " + (p.stderr or p.stdout)[-300:])


def to_pdf(project: dict) -> dict:
    from playwright.sync_api import sync_playwright
    from pypdf import PdfReader

    pdir = project_dir(project["id"])
    html_path = pdir / "pattern.html"
    pdf_path = pdir / "pattern.pdf"
    html_path.write_text(build_html(project))
    b = brand()
    footer_tpl = ("<div style='font:9px Helvetica,Arial,sans-serif;color:#888;width:100%;padding:0 16mm;"
                  "display:flex;justify-content:space-between'><span>" + _e(b["name"]) + "</span>"
                  "<span><span class='pageNumber'></span> / <span class='totalPages'></span></span></div>")
    with sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as e:
            if "Executable doesn't exist" not in str(e):
                raise
            _install_browser()
            br = pw.chromium.launch()
        page = br.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_load_state("networkidle")
        page.pdf(path=str(pdf_path), format="A4", print_background=True,
                 margin={"top": "16mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
                 display_header_footer=True, header_template="<div></div>", footer_template=footer_tpl)
        br.close()
    pages = len(PdfReader(str(pdf_path)).pages)
    title = project["pattern"].get("title") or project.get("title") or f"pattern-{project['id']}"
    lib_name = f"{_slug(title)}-{time.strftime('%Y%m%d-%H%M')}.pdf"
    shutil.copyfile(pdf_path, LIBRARY_DIR / lib_name)
    st = llm.settings()
    settings = {"engine": st["engine"], "models": st["models"], "efforts": st["efforts"]}
    vid = db.add_pdf_version(project["id"], pdf_path.name, lib_name, pdf_path.stat().st_size, pages, settings)
    return {"pdf": pdf_path, "library_file": lib_name, "pages": pages, "size": pdf_path.stat().st_size, "version_id": vid}
