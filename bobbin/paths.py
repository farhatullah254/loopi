from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR = ROOT / "templates"
DB_PATH = DATA_DIR / "bobbin.db"
LIBRARY_DIR = DATA_DIR / "library"
BRAND_DIR = DATA_DIR / "brand"

for d in (DATA_DIR, PROJECTS_DIR, LIBRARY_DIR, BRAND_DIR):
    d.mkdir(parents=True, exist_ok=True)


def project_dir(pid: int) -> Path:
    p = PROJECTS_DIR / str(pid)
    (p / "frames").mkdir(parents=True, exist_ok=True)
    (p / "uploads").mkdir(parents=True, exist_ok=True)
    return p
