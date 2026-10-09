"""Run prompts through the local Claude Code CLI (`claude -p`) so usage bills the Claude subscription.

Text-only jobs run with every tool disabled. Frame-picking jobs get the Read tool and the frame
directory as cwd, so Claude opens the candidate JPEGs itself.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
TIMEOUT_TEXT = 25 * 60
TIMEOUT_PICK = 6 * 60
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
AUTH_HELP = ("Your Claude token has expired or been revoked. In Terminal run `claude setup-token`, copy the "
             "token it prints, paste it in Loopi Settings → Claude engine → Claude token, then press re-run.")


def set_token(token: str) -> None:
    """Save a `claude setup-token` token to .env (mode 600) and use it immediately, no restart."""
    token = "".join(token.split()).strip('"\'')
    if not token.startswith("sk-ant-") or len(token) < 60:
        raise ValueError("That does not look like a Claude token. It starts with sk-ant-oat and is about 100 characters.")
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if not l.startswith("CLAUDE_CODE_OAUTH_TOKEN=")]
    lines.append(f'CLAUDE_CODE_OAUTH_TOKEN="{token}"')
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token


def has_token() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def available() -> bool:
    return bool(CLAUDE) and Path(CLAUDE).exists()


def _env() -> dict:
    """Clean environment: no nested-session markers, no API proxy, no API key."""
    keep = ("HOME", "PATH", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL", "CLAUDE_CODE_OAUTH_TOKEN")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["TERM"] = "dumb"
    return env


def complete(system: str, prompt: str, model: str, effort: str = "high", image_dir: Path | None = None,
             timeout: int | None = None, max_turns: int = 20) -> str:
    if not available():
        raise RuntimeError("Claude Code CLI not found. Install Claude Code or switch the engine to API key.")
    args = [CLAUDE, "-p", prompt, "--system-prompt", system, "--output-format", "json",
            "--model", model, "--effort", effort, "--no-session-persistence", "--setting-sources", "user"]
    if image_dir is None:
        args += ["--tools", ""]
        cwd = None
    else:
        args += ["--tools", "Read", "--allowedTools", "Read", "--permission-mode", "dontAsk", "--max-turns", str(max_turns)]
        cwd = str(image_dir)
    try:
        p = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_env(),
                           cwd=cwd, timeout=timeout or (TIMEOUT_PICK if image_dir else TIMEOUT_TEXT))
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude Code did not answer in time. If it is not logged in, run `claude` in a "
                           "terminal and sign in, then retry.")
    out = p.stdout.strip()
    if p.returncode != 0 and not out:
        raise RuntimeError("Claude Code failed: " + (p.stderr.strip()[-600:] or f"exit {p.returncode}"))
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        start, end = out.find("{"), out.rfind("}")
        if start < 0:
            raise RuntimeError("Claude Code returned no JSON: " + out[-400:])
        data = json.loads(out[start:end + 1])
    if isinstance(data, list):
        data = next((d for d in reversed(data) if d.get("type") == "result"), data[-1])
    if data.get("is_error"):
        msg = str(data.get("result") or data.get("error") or data)
        if "401" in msg or "authenticate" in msg.lower() or "revoked" in msg.lower() or "expired" in msg.lower():
            raise RuntimeError(AUTH_HELP + " Details: " + msg[:200])
        raise RuntimeError("Claude Code error: " + msg[:600])
    result = data.get("result")
    if result is None:
        raise RuntimeError("Claude Code returned no result: " + out[:400])
    return result if isinstance(result, str) else json.dumps(result)


def check(model: str = "opus", effort: str = "low") -> dict:
    """Quick login/availability probe used by the settings panel."""
    if not available():
        return {"ok": False, "detail": "claude CLI not found on PATH"}
    try:
        text = complete("Answer with JSON only.", 'Reply with exactly {"ok": true}', model, effort, timeout=75)
        return {"ok": '"ok"' in text, "detail": text[:200]}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:400]}
