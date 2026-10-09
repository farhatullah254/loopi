#!/usr/bin/env python3
import base64
import json
import mimetypes
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from bobbin import db, jobs, llm, render, review, storage, validate, youtube, ytauth
from bobbin.paths import BRAND_DIR, LIBRARY_DIR, PROJECTS_DIR, STATIC_DIR, project_dir

HOST = os.environ.get("LOOPI_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOOPI_PORT", "3300"))
MAX_BODY = 40 * 1024 * 1024
DENSITIES = ("concise", "standard", "beginner")


class Handler(BaseHTTPRequestHandler):
    server_version = "Loopi/1.0"

    def log_message(self, fmt, *args):
        line = fmt % args
        if line.startswith('"GET ') and ('" 200' in line or '" 206' in line or '" 304' in line):
            return
        sys.stderr.write(line + "\n")

    def _send(self, code, payload, ctype="application/json", headers=None):
        data = payload if isinstance(payload, bytes) else (
            payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if method == "GET" and not parts:
                return self._static("index.html")
            if method == "GET" and parts[0] == "static" and len(parts) == 2:
                return self._static(parts[1])
            if method == "GET" and parts[0] == "files" and len(parts) == 4:
                return self._file(int(parts[1]), parts[2], parts[3])
            if method == "GET" and parts[0] == "brand" and len(parts) == 2:
                return self._plain_file(BRAND_DIR, parts[1])
            if method == "GET" and parts[0] == "library" and len(parts) == 2:
                return self._plain_file(LIBRARY_DIR, parts[1], attach=True)
            if parts[:1] != ["api"]:
                return self._send(404, {"error": "not found"})
            return self._api(method, parts[1:], parse_qs(url.query))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _static(self, name):
        path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return self._send(200, path.read_bytes(), ctype)

    def _plain_file(self, base, name, attach=False):
        path = (base / name).resolve()
        if base.resolve() not in path.parents or not path.is_file() or "/" in name:
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        hdrs = {"Content-Disposition": f'attachment; filename="{name}"'} if attach else {"Cache-Control": "max-age=3600"}
        return self._send(200, path.read_bytes(), ctype, hdrs)

    def _file(self, pid, kind, name):
        if kind not in ("frames", "uploads", "pdf", "video", "thumb") or "/" in name or ".." in name:
            return self._send(404, "not found", "text/plain")
        pdir = PROJECTS_DIR / str(pid)
        path = (pdir / ("" if kind in ("pdf", "video", "thumb") else kind) / name).resolve()
        if pdir.resolve() not in path.parents or not path.is_file():
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if kind == "video":
            return self._ranged(path, ctype)
        hdrs = {"Cache-Control": "max-age=3600"} if kind != "pdf" else {
            "Content-Disposition": f'inline; filename="{name}"'}
        return self._send(200, path.read_bytes(), ctype, hdrs)

    def _ranged(self, path, ctype):
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                elif m.group(1):
                    end = min(start + 4 * 1024 * 1024, size - 1)
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # ------------------------------------------------------------------ api
    def _api(self, method, parts, q):
        if parts == ["projects"] and method == "GET":
            return self._send(200, {"projects": db.list_projects()})
        if parts == ["projects"] and method == "POST":
            body = self._json()
            url = (body.get("url") or "").strip()
            if not youtube.video_id(url):
                raise ValueError("That does not look like a YouTube video URL")
            density = body.get("density") if body.get("density") in DENSITIES else "standard"
            existing = db.find_by_video(youtube.video_id(url))
            if existing and not body.get("force"):
                return self._send(200, {"id": existing, "existing": True})
            pid = db.create_project(url, density)
            up = {"featured": [], "final": [], "keep_ai_finals": False}
            for kind, limit in (("featured", 1), ("final", 2)):
                for f in (body.get("images") or {}).get(kind, [])[:limit]:
                    up[kind].append(_save_upload(pid, f.get("name"), f.get("b64")))
            db.update_project(pid, uploads=up)
            jobs.enqueue(pid, body.get("target") or "ready")
            return self._send(201, {"id": pid})
        if parts == ["settings"] and method == "GET":
            st = llm.settings()
            st["review"] = db.review_stats()
            st["workers"] = jobs.workers()
            st["all_choices"] = llm.MODEL_CHOICES
            return self._send(200, st)
        if parts == ["settings"] and method == "PATCH":
            body = self._json()
            if body.get("engine") in llm.JOB_DEFAULTS:
                db.set_setting("engine", body["engine"])
            if body.get("workers"):
                db.set_setting("workers", str(max(1, min(jobs.MAX_WORKERS, int(body["workers"])))))
            eng = llm.engine()
            for job in ("clean", "extract", "pick"):
                if body.get(f"model_{job}"):
                    db.set_setting(f"model_{eng}_{job}", str(body[f"model_{job}"]).strip())
                if body.get(f"effort_{job}") in llm.EFFORT_CHOICES:
                    db.set_setting(f"effort_{job}", body[f"effort_{job}"])
            return self._send(200, llm.settings())
        if parts == ["brand"] and method == "GET":
            return self._send(200, render.brand())
        if parts == ["brand"] and method == "PATCH":
            body = self._json()
            for k in ("name", "footer"):
                if k in body:
                    db.set_setting(f"brand_{k}", str(body[k]).strip())
            if body.get("logo_b64"):
                name = "logo_" + re.sub(r"[^A-Za-z0-9._-]", "_", body.get("logo_name") or "logo.png")
                (BRAND_DIR / name).write_bytes(base64.b64decode(body["logo_b64"].split(",")[-1]))
                db.set_setting("brand_logo", name)
            if body.get("remove_logo"):
                db.set_setting("brand_logo", "")
            return self._send(200, render.brand())
        if parts == ["ytauth"] and method == "GET":
            return self._send(200, ytauth.status())
        if parts == ["ytauth", "open"] and method == "POST":
            ytauth.open_signin()
            return self._send(200, ytauth.status())
        if parts == ["ytauth", "save"] and method == "POST":
            r = ytauth.save_cookies()
            return self._send(200, {**ytauth.status(), **r})
        if parts == ["ytauth", "signout"] and method == "POST":
            ytauth.sign_out()
            return self._send(200, ytauth.status())
        if parts == ["storage"] and method == "GET":
            return self._send(200, storage.usage())
        if parts == ["storage", "clean"] and method == "POST":
            r = storage.clean(self._json().get("what"))
            return self._send(200, {**r, **storage.usage()})
        if parts == ["library"] and method == "GET":
            files = sorted(LIBRARY_DIR.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
            return self._send(200, {"files": [{"name": f.name, "size": f.stat().st_size, "at": f.stat().st_mtime} for f in files]})
        if parts == ["settings", "token"] and method == "POST":
            from bobbin import engine_cc
            engine_cc.set_token(self._json().get("token") or "")
            return self._send(200, engine_cc.check(llm.model("pick"), llm.effort("pick")))
        if parts == ["settings", "test"] and method == "POST":
            from bobbin import engine_cc
            if llm.engine() == "claude-code":
                return self._send(200, engine_cc.check(llm.model("pick"), llm.effort("pick")))
            try:
                text = llm._complete_api("Answer with JSON only.", 'Reply with exactly {"ok": true}', llm.model("pick"), 200, llm.effort("pick"))
                return self._send(200, {"ok": '"ok"' in text, "detail": text[:200]})
            except Exception as e:
                return self._send(200, {"ok": False, "detail": str(e)[:400]})

        if len(parts) >= 2 and parts[0] == "projects":
            pid = int(parts[1])
            p = db.get_project(pid, full=False)
            if not p:
                return self._send(404, {"error": "no such project"})
            rest = parts[2:]
            if not rest and method == "GET":
                p["running"] = jobs.is_running(pid)
                return self._send(200, p)
            if not rest and method == "DELETE":
                if jobs.is_running(pid):
                    raise ValueError("Project is running; wait for it to finish")
                db.delete_project(pid)
                import shutil
                shutil.rmtree(project_dir(pid), ignore_errors=True)
                return self._send(200, {"ok": True})
            if rest == ["transcript"] and method == "GET":
                full = db.get_project(pid)
                return self._send(200, {"captions": full.get("captions") or [], "cleaned": full.get("cleaned") or []})
            if rest == ["run"] and method == "POST":
                body = self._json()
                target = body.get("target") or "ready"
                if body.get("from"):
                    db.update_project(pid, stage=body["from"])
                if not jobs.enqueue(pid, target):
                    raise ValueError("Already running")
                return self._send(200, {"ok": True})
            if rest == ["density"] and method == "PATCH":
                d = self._json().get("density")
                if d not in DENSITIES:
                    raise ValueError("bad density")
                db.update_project(pid, density=d)
                return self._send(200, {"ok": True})
            if rest == ["pattern"] and method == "PATCH":
                return self._patch_pattern(pid)
            if rest == ["grab"] and method == "POST":
                return self._grab(pid)
            if rest == ["upload"] and method == "POST":
                return self._upload(pid)
            if rest[:1] == ["upload"] and method == "DELETE" and len(rest) == 3:
                return self._unupload(pid, rest[1], rest[2])
            if rest == ["render"] and method == "POST":
                full = db.get_project(pid)
                if not full.get("pattern"):
                    raise ValueError("No pattern to render yet")
                if not full["pattern"].get("review"):
                    # Never sell an unreviewed pattern: patterns made before the review stage get one pass now.
                    if jobs.is_running(pid):
                        raise ValueError("Project is running; wait for it to finish")
                    review.run(full["pattern"], full.get("cleaned") or [], project_dir(pid))
                    db.update_project(pid, pattern=full["pattern"])
                r = render.to_pdf(full)
                db.update_project(pid, pdf_path=r["pdf"].name)
                return self._send(200, {"pdf": f"/files/{pid}/pdf/{r['pdf'].name}?t={int(time.time())}",
                                        "library": f"/library/{r['library_file']}", "pages": r["pages"], "size": r["size"]})
            if rest == ["polish"] and method == "POST":
                full = db.get_project(pid)
                if not full.get("pattern"):
                    raise ValueError("No pattern yet")
                if jobs.is_running(pid):
                    raise ValueError("Project is running; wait for it to finish")
                pattern = full["pattern"]
                r = llm.polish_voice(pattern)
                pattern["validation"] = validate.run(pattern)
                db.update_project(pid, pattern=pattern)
                return self._send(200, {"steps_changed": r["steps_changed"], "validation": pattern["validation"]})
            if rest == ["versions"] and method == "GET":
                return self._send(200, {"versions": db.pdf_versions(pid)})
            if rest == ["uploads"] and method == "PATCH":
                body = self._json()
                full = db.get_project(pid)
                up = full.get("uploads") or {"featured": [], "final": []}
                if "keep_ai_finals" in body:
                    up["keep_ai_finals"] = bool(body["keep_ai_finals"])
                db.update_project(pid, uploads=up)
                return self._send(200, {"uploads": up})
            if rest == ["html"] and method == "GET":
                full = db.get_project(pid)
                return self._send(200, render.build_html(full), "text/html")
        return self._send(404, {"error": "not found"})

    def _patch_pattern(self, pid):
        body = self._json()
        full = db.get_project(pid)
        pattern = full.get("pattern") or {}
        if "pattern" in body:
            new = body["pattern"]
            _diff_log(pid, pattern, new)
            pattern = new
        elif "step" in body:
            st_new = body["step"]
            for sec in pattern.get("sections", []):
                for i, st in enumerate(sec["steps"]):
                    if st["id"] == st_new.get("id"):
                        for f in ("instruction", "label", "stitch_count", "delta", "tier", "kind"):
                            if f in st_new and st_new[f] != st.get(f):
                                db.log_review(pid, st["id"], f, st.get(f), st_new[f])
                                st[f] = st_new[f]
                        if "images" in st_new:
                            st["images"] = st_new["images"]
        elif "final_images" in body:
            pattern["final_images"] = body["final_images"]
        elif "meta" in body:
            for f in ("title", "summary", "skill_level", "materials", "abbreviations", "notes", "gauge", "finishing",
                      "finished_size", "yarn_substitution"):
                if f in body["meta"]:
                    pattern[f] = body["meta"][f]
        pattern["validation"] = validate.run(pattern)
        db.update_project(pid, pattern=pattern)
        return self._send(200, {"validation": pattern["validation"]})

    def _grab(self, pid):
        """Grab a burst at a time for a step image (or final image), pick with vision, save."""
        body = self._json()
        t = float(body["t"])
        full = db.get_project(pid)
        pattern = full["pattern"]
        pdir = project_dir(pid)
        video = jobs.ensure_video(pid, full)
        img = None
        tag = None
        if body.get("step_id"):
            for sec in pattern["sections"]:
                for st in sec["steps"]:
                    if st["id"] == body["step_id"]:
                        k = int(body.get("index", len(st["images"])))
                        while len(st["images"]) <= k:
                            st["images"].append({"t": t, "description": body.get("description") or "", "candidates": [],
                                                 "chosen": None, "manual": False})
                        img = st["images"][k]
                        if st.get("tier") == "none":
                            st["tier"] = "anchor"
                        tag = f"{st['id']}_{k}_{int(time.time())}"
        else:
            k = int(body.get("index", len(pattern["final_images"])))
            while len(pattern["final_images"]) <= k:
                pattern["final_images"].append({"t": t, "description": "finished object", "candidates": [],
                                                "chosen": None, "manual": False})
            img = pattern["final_images"][k]
            tag = f"final_{k}_{int(time.time())}"
        img["t"] = t
        img["manual"] = False
        if body.get("description"):
            img["description"] = body["description"]
        if body.get("exact"):
            from bobbin import frames
            path = frames.grab(video, t, pdir / "frames" / f"{tag}_exact.jpg")
            img["candidates"] = [{"t": t, "path": path.name}]
            img["chosen"] = path.name
            img["quality"] = "manual"
        else:
            jobs.grab_and_pick(video, pdir / "frames", tag, img, full.get("duration"))
        db.update_project(pid, pattern=pattern)
        return self._send(200, {"image": img})

    def _upload(self, pid):
        body = self._json()
        kind = body.get("kind")
        if kind not in ("featured", "final", "step"):
            raise ValueError("kind must be featured, final or step")
        name = _save_upload(pid, body.get("name"), body["b64"])
        full = db.get_project(pid)
        if kind == "step":
            pattern = full["pattern"]
            targets = []
            if body.get("step_id"):
                targets = [st for sec in pattern["sections"] for st in sec["steps"] if st["id"] == body["step_id"]]
            else:
                targets = [{"images": pattern.setdefault("final_images", [])}]
            for st in targets:
                imgs = st["images"]
                k = int(body.get("index", len(imgs)))
                if k < len(imgs):
                    img = imgs[k]
                    if not img.get("manual"):
                        img["prev_chosen"] = img.get("chosen")
                    img["chosen"] = name
                    img["manual"] = True
                    img["quality"] = "manual"
                    img["pick_reason"] = "replaced with your own image"
                    if body.get("description"):
                        img["description"] = body["description"]
                else:
                    imgs.append({"t": None, "description": body.get("description") or "uploaded image",
                                 "candidates": [], "chosen": name, "manual": True, "quality": "manual"})
                if st.get("tier") == "none":
                    st["tier"] = "anchor"
            db.update_project(pid, pattern=pattern)
        else:
            up = full.get("uploads") or {"featured": [], "final": []}
            limit = 1 if kind == "featured" else 2
            lst = up.setdefault(kind, [])
            lst.append(name)
            up[kind] = lst[-limit:]
            db.update_project(pid, uploads=up)
        return self._send(200, {"name": name})

    def _unupload(self, pid, kind, name):
        full = db.get_project(pid)
        up = full.get("uploads") or {"featured": [], "final": []}
        if name in up.get(kind, []):
            up[kind].remove(name)
            db.update_project(pid, uploads=up)
        return self._send(200, {"ok": True})


def _save_upload(pid, name, b64) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "upload.jpg")
    name = f"{int(time.time() * 1000)}_{name}"
    raw = base64.b64decode((b64 or "").split(",")[-1])
    if not raw:
        raise ValueError("Empty image upload")
    (project_dir(pid) / "uploads" / name).write_bytes(raw)
    return name


def _diff_log(pid, old, new):
    old_steps = {s["id"]: s for sec in old.get("sections", []) for s in sec.get("steps", [])}
    for sec in new.get("sections", []):
        for s in sec.get("steps", []):
            o = old_steps.get(s.get("id"))
            if not o:
                continue
            for f in ("instruction", "stitch_count", "tier"):
                if o.get(f) != s.get(f):
                    db.log_review(pid, s["id"], f, o.get(f), s.get(f))


def main():
    db.init()
    stale = db.release_stale_jobs()
    if stale:
        print(f"Released {stale} job(s) left running by a previous shutdown; press re-run on them to continue", flush=True)
    jobs.start_worker()
    for pid in db.waiting_projects():
        jobs.schedule_retry(pid, 5)
        db.update_project(pid, progress="Server restarted; caption retry in 5 minutes")
    st = llm.settings()
    print(f"Loopi  http://{HOST}:{PORT}   engine={st['engine']}   models={st['models']}   efforts={st['efforts']}   "
          f"claude CLI {'found' if st['claude_cli'] else 'MISSING'}   API key {'present' if st['api_key_present'] else 'absent'}", flush=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
