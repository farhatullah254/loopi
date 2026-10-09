import json
import sqlite3
import threading
import time

from .paths import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  video_id TEXT,
  title TEXT,
  channel TEXT,
  duration REAL,
  thumbnail TEXT,
  density TEXT NOT NULL DEFAULT 'standard',
  stage TEXT NOT NULL DEFAULT 'new',
  status TEXT NOT NULL DEFAULT 'idle',
  progress TEXT,
  error TEXT,
  captions_json TEXT,
  cleaned_json TEXT,
  pattern_json TEXT,
  uploads_json TEXT NOT NULL DEFAULT '{"featured": [], "final": []}',
  pdf_path TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS pdf_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  filename TEXT NOT NULL,
  library_file TEXT,
  size INTEGER,
  pages INTEGER,
  settings_json TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER,
  step_id TEXT,
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  at REAL NOT NULL
);
"""

JSON_COLS = ("captions_json", "cleaned_json", "pattern_json", "uploads_json")


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


MIGRATIONS = [
    "ALTER TABLE projects ADD COLUMN thumb_file TEXT",
]


def init():
    with _conn() as c:
        c.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                c.execute(m)
            except sqlite3.OperationalError:
                pass


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS:
        if k in d and d[k] is not None:
            try:
                d[k[:-5]] = json.loads(d[k])
            except Exception:
                d[k[:-5]] = None
            del d[k]
    return d


def create_project(url: str, density: str) -> int:
    now = time.time()
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO projects (url, density, created_at, updated_at) VALUES (?,?,?,?)",
            (url, density, now, now))
        return cur.lastrowid


def list_projects():
    with _conn() as c:
        rows = c.execute(
            "SELECT p.id, p.url, p.video_id, p.title, p.channel, p.duration, p.thumbnail, p.thumb_file, p.density, "
            "p.stage, p.status, p.progress, p.error, p.pdf_path, p.uploads_json, p.created_at, p.updated_at, "
            "json_extract(p.pattern_json, '$.title') AS pattern_title, "
            "json_extract(p.pattern_json, '$.skill_level') AS skill_level, "
            "(SELECT COUNT(*) FROM pdf_versions v WHERE v.project_id = p.id) AS pdf_count, "
            "(SELECT MAX(created_at) FROM pdf_versions v WHERE v.project_id = p.id) AS pdf_at "
            "FROM projects p ORDER BY p.id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["uploads"] = json.loads(d.pop("uploads_json") or "{}")
            except Exception:
                d["uploads"] = {}
            out.append(d)
        return out


def find_by_video(video_id: str):
    with _conn() as c:
        r = c.execute("SELECT id FROM projects WHERE video_id=? ORDER BY id DESC LIMIT 1", (video_id,)).fetchone()
        return r["id"] if r else None


def add_pdf_version(pid: int, filename: str, library_file: str | None, size: int, pages: int, settings: dict) -> int:
    with _lock, _conn() as c:
        cur = c.execute("INSERT INTO pdf_versions (project_id, filename, library_file, size, pages, settings_json, created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (pid, filename, library_file, size, pages, json.dumps(settings), time.time()))
        return cur.lastrowid


def pdf_versions(pid: int):
    with _conn() as c:
        rows = c.execute("SELECT * FROM pdf_versions WHERE project_id=? ORDER BY id DESC", (pid,)).fetchall()
        return [dict(r) for r in rows]


def all_settings() -> dict:
    with _conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


def get_project(pid: int, full=True):
    with _conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        d = _row(r)
        if d and not full:
            d.pop("captions", None)
            d.pop("cleaned", None)
        return d


def update_project(pid: int, **fields):
    if not fields:
        return
    for k in list(fields):
        if k in ("captions", "cleaned", "pattern", "uploads"):
            fields[k + "_json"] = json.dumps(fields.pop(k))
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn() as c:
        c.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))


def delete_project(pid: int):
    with _lock, _conn() as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
        c.execute("DELETE FROM review_log WHERE project_id=?", (pid,))


def log_review(pid: int, step_id: str, field: str, old, new):
    with _lock, _conn() as c:
        c.execute("INSERT INTO review_log (project_id, step_id, field, old_value, new_value, at) VALUES (?,?,?,?,?,?)",
                  (pid, step_id, field, json.dumps(old), json.dumps(new), time.time()))


def review_stats():
    with _conn() as c:
        rows = c.execute("SELECT field, old_value, new_value FROM review_log WHERE field='tier'").fetchall()
    up = down = 0
    order = ["none", "anchor", "sequence", "walkthrough"]
    for r in rows:
        try:
            o, n = json.loads(r["old_value"]), json.loads(r["new_value"])
            if order.index(n) > order.index(o):
                up += 1
            elif order.index(n) < order.index(o):
                down += 1
        except Exception:
            pass
    return {"tier_up": up, "tier_down": down}


def get_setting(key: str, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str):
    with _lock, _conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def release_stale_jobs() -> int:
    """At startup nothing is running, so any 'running'/'queued' row was orphaned by a shutdown."""
    with _lock, _conn() as c:
        cur = c.execute("UPDATE projects SET status='error', error=?, updated_at=? WHERE status IN ('running','queued')",
                        ("Interrupted by a restart. Everything finished so far is kept; press the re-run button "
                         "for the next stage to continue.", time.time()))
        return cur.rowcount


def waiting_projects() -> list[int]:
    with _conn() as c:
        return [r["id"] for r in c.execute("SELECT id FROM projects WHERE status='waiting'")]
