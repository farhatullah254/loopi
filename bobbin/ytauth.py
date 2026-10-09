"""Optional YouTube sign-in, used ONLY to fetch a caption file after YouTube refused it as a guest.

The user signs in themselves in a separate Chrome profile (data/yt-chrome), so their everyday
Chrome sessions are never read or rotated. After they close that window, the YouTube/Google
cookies are exported once to data/yt-cookies.txt (mode 600) for yt-dlp.
"""
import os
import shutil
import subprocess
import time
from http.cookiejar import MozillaCookieJar

from .paths import DATA_DIR

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR = DATA_DIR / "yt-chrome"
COOKIES = DATA_DIR / "yt-cookies.txt"
SIGNIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"
AUTH_NAMES = {"LOGIN_INFO", "SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"}
KEEP_DOMAINS = ("youtube.com", "google.com")


def window_open() -> bool:
    r = subprocess.run(["pgrep", "-f", f"user-data-dir={PROFILE_DIR}"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def open_signin():
    if not os.path.exists(CHROME):
        raise RuntimeError("Google Chrome is not installed in /Applications")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([CHROME, f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check",
                      "--new-window", SIGNIN_URL],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def save_cookies() -> dict:
    """Export the sign-in profile's YouTube/Google cookies. The window must be closed first."""
    if window_open():
        raise RuntimeError("Close the Loopi sign-in Chrome window first (Cmd+Q), then press Save sign-in again.")
    if not (PROFILE_DIR / "Default").exists():
        raise RuntimeError("No sign-in yet. Press Open sign-in window, sign in to YouTube, quit that window, then save.")
    from yt_dlp.cookies import extract_cookies_from_browser
    jar = extract_cookies_from_browser("chrome", str(PROFILE_DIR / "Default"))
    out = MozillaCookieJar(str(COOKIES))
    n = 0
    for c in jar:
        if any(c.domain.lstrip(".").endswith(d) for d in KEEP_DOMAINS):
            out.set_cookie(c)
            n += 1
    signed_in = any(c.name in AUTH_NAMES and "youtube.com" in c.domain for c in out)
    if not signed_in:
        raise RuntimeError("Could not find a YouTube sign-in in that window. Sign in on youtube.com there, "
                           "quit the window, and save again.")
    out.save(ignore_discard=True, ignore_expires=True)
    os.chmod(COOKIES, 0o600)
    return {"cookies": n}


def status() -> dict:
    signed_in = False
    if COOKIES.exists():
        try:
            jar = MozillaCookieJar(str(COOKIES))
            jar.load(ignore_discard=True, ignore_expires=True)
            signed_in = any(c.name in AUTH_NAMES and "youtube.com" in c.domain for c in jar)
        except Exception:
            signed_in = False
    return {"signed_in": signed_in, "saved_at": COOKIES.stat().st_mtime if COOKIES.exists() else None,
            "window_open": window_open(), "chrome": os.path.exists(CHROME)}


def sign_out():
    if window_open():
        raise RuntimeError("Close the Loopi sign-in Chrome window first.")
    if COOKIES.exists():
        COOKIES.unlink()
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)


def cookies_file():
    return str(COOKIES) if status()["signed_in"] else None
