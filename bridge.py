#!/usr/bin/env python3
"""omp <-> Telegram bridge.

A zero-dependency (stdlib-only) bridge that turns the `omp` coding agent into a
Telegram bot. Long-polls Telegram getUpdates, and for each authorized chat runs
`omp -p` in a per-chat session directory so conversations stay coherent.

omp has no messaging gateway of its own; this is the thin frontend that a
framework like Hermes would otherwise provide.

Setup:
  python3 bridge.py setup      Interactive wizard: bot token, allowed chats,
                                model, data dir, and (on Linux) a systemd
                                --user service so the bridge survives reboots.
                                Writes ~/.omp-agent/.env.
  python3 bridge.py            Run the bridge in the foreground.

Deploys:
  `setup` copies this checkout into ~/.omp-agent/release and points the
  systemd unit there, never at the checkout itself — hand-edits or a mid-pull
  checkout are never what's live. /update re-pulls the checkout named by
  OMP_BRIDGE_REPO_DIR, byte-compiles it, and only then re-deploys the copy
  and restarts; a bad pull is rejected (and rolled back) before it can take
  the bot down. Only when systemd itself gives up (see StartLimitBurst in
  the unit) does the omp-bridge-alert.service fire and message you on
  Telegram that the bot is down.

Config is read from the environment first, then from ~/.omp-agent/.env
(override the directory with OMP_AGENT_HOME):
  TELEGRAM_BOT_TOKEN     Bot token from @BotFather. Required.
  OMP_BRIDGE_ALLOWED     Comma-separated chat ids allowed to use the bot.
                         "*" allows everyone (DANGEROUS: --auto-approve lets
                         omp run shell commands/edits for anyone who messages
                         it).
  OMP_BRIDGE_MODEL       Model override passed to omp (default: omp's config).
                         Changeable at runtime from Telegram via /model.
  OMP_BRIDGE_HOME        Base dir for sessions + workspace.
                         Default: ~/.omp-agent/data
  OMP_BRIDGE_TIMEOUT     Per-message omp timeout in seconds (default: 3600).
  OMP_BRIDGE_HEARTBEAT_FIRST     Seconds of silence before the first "still
                         working" message (default: 180). 0 disables
                         heartbeats entirely. Typing indicators alone
                         fade/re-arm every few seconds and can look stalled
                         on long, tool-heavy turns.
  OMP_BRIDGE_HEARTBEAT_INTERVAL  Seconds between subsequent heartbeats after
                         the first (default: 120). 0 sends only the first
                         and no more.
  OMP_BRIDGE_HEARTBEAT_TEXT  Heartbeat message template; "{elapsed}" is
                         replaced with minutes waited so far.
  OMP_BIN                Path to the omp binary (default: resolve from PATH).
  OMP_BRIDGE_CRON_FILE   Scheduled-job definitions (default: ~/.omp-agent/cron.json).
                         Absent file = no cron jobs. See the "Cron scheduler"
                         section below for the job format.
  OMP_BRIDGE_REPO_DIR    Git checkout to pull from via /update. Default: the
                         directory this script lives in (fine if you don't
                         separate dev/prod; see "Deploys" below if you do).
"""

import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────

AGENT_HOME = Path(os.environ.get("OMP_AGENT_HOME", str(Path.home() / ".omp-agent")))
ENV_FILE = AGENT_HOME / ".env"


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE file, without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv(ENV_FILE)

TOKEN = ""
ALLOW_ALL = False
ALLOWED: set = set()
MODEL = ""
HOME: Path = AGENT_HOME / "data"
SESSIONS: Path = HOME / "sessions"
WORKSPACE: Path = HOME / "workspace"
MEDIA_DIR: Path = HOME / "media"
TIMEOUT = 3600
HEARTBEAT_FIRST = 180     # seconds before the first "still working" nudge; 0 disables entirely
HEARTBEAT_INTERVAL = 120  # seconds between subsequent nudges; 0 = first one only
HEARTBEAT_TEXT = "\u23f3 Still working on it ({elapsed} min so far)... I'll reply as soon as it's done."
TYPING_ENABLED = True
PROGRESS_ENABLED = True
OMP_BIN = ""
CRON_FILE: Path = AGENT_HOME / "cron.json"
CRON_STATE_FILE: Path = HOME / "cron_state.json"
CRON_JOBS: list = []

TG_LIMIT = 4096  # Telegram max message length
_STARTED_AT = time.monotonic()  # process start, for /status uptime


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def configure() -> None:
    """Load run-mode config from the environment. Called once, before main()."""
    global TOKEN, ALLOW_ALL, ALLOWED, MODEL, HOME, SESSIONS, WORKSPACE, MEDIA_DIR, TIMEOUT, OMP_BIN
    global HEARTBEAT_FIRST, HEARTBEAT_INTERVAL, HEARTBEAT_TEXT, TYPING_ENABLED, PROGRESS_ENABLED
    global CRON_FILE, CRON_STATE_FILE, CRON_JOBS

    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not TOKEN:
        sys.exit(f"No TELEGRAM_BOT_TOKEN configured. Run: {sys.executable} {Path(__file__).name} setup")

    allowed_raw = os.environ.get("OMP_BRIDGE_ALLOWED", "").strip()
    ALLOW_ALL = allowed_raw == "*"
    ALLOWED = set() if ALLOW_ALL else {c.strip() for c in allowed_raw.split(",") if c.strip()}
    if not ALLOW_ALL and not ALLOWED:
        sys.exit(f"No OMP_BRIDGE_ALLOWED configured. Run: {sys.executable} {Path(__file__).name} setup")

    MODEL = os.environ.get("OMP_BRIDGE_MODEL", "").strip()
    HOME = Path(os.environ.get("OMP_BRIDGE_HOME", str(AGENT_HOME / "data")))
    SESSIONS = HOME / "sessions"
    WORKSPACE = HOME / "workspace"
    MEDIA_DIR = HOME / "media"
    TIMEOUT = int(os.environ.get("OMP_BRIDGE_TIMEOUT", "3600"))
    HEARTBEAT_FIRST = int(os.environ.get("OMP_BRIDGE_HEARTBEAT_FIRST", "180"))
    HEARTBEAT_INTERVAL = int(os.environ.get("OMP_BRIDGE_HEARTBEAT_INTERVAL", "120"))
    HEARTBEAT_TEXT = os.environ.get("OMP_BRIDGE_HEARTBEAT_TEXT", "").strip() or HEARTBEAT_TEXT
    TYPING_ENABLED = _env_bool("OMP_BRIDGE_TYPING_ENABLED", True)
    PROGRESS_ENABLED = _env_bool("OMP_BRIDGE_PROGRESS_ENABLED", True)
    OMP_BIN = os.environ.get("OMP_BIN", "") or shutil.which("omp") or str(Path.home() / ".local" / "bin" / "omp")
    CRON_FILE = Path(os.environ.get("OMP_BRIDGE_CRON_FILE", str(AGENT_HOME / "cron.json")))

    SESSIONS.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    CRON_STATE_FILE = HOME / "cron_state.json"
    CRON_JOBS = _load_cron_jobs(CRON_FILE)


# ── Model override ───────────────────────────────────────────────────────────


def _write_env_var(key: str, value: str) -> None:
    """Update or add KEY=VALUE in the .env file, preserving every other line."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n")
    ENV_FILE.chmod(0o600)


def set_model(new_model: str) -> None:
    """Change the model omp is invoked with, in-process and in ~/.omp-agent/.env."""
    global MODEL
    MODEL = new_model.strip()
    _write_env_var("OMP_BRIDGE_MODEL", MODEL)


# ── Runtime toggles (typing indicator / progress heartbeats) ────────────────


def set_typing_enabled(enabled: bool) -> None:
    """Toggle the repeated 'typing...' indicator during a run, in-process and in ~/.omp-agent/.env."""
    global TYPING_ENABLED
    TYPING_ENABLED = enabled
    _write_env_var("OMP_BRIDGE_TYPING_ENABLED", "true" if enabled else "false")


def set_progress_enabled(enabled: bool) -> None:
    """Toggle the periodic 'still working' progress heartbeats, in-process and in ~/.omp-agent/.env."""
    global PROGRESS_ENABLED
    PROGRESS_ENABLED = enabled
    _write_env_var("OMP_BRIDGE_PROGRESS_ENABLED", "true" if enabled else "false")


# ── Telegram API helpers ────────────────────────────────────────────────────


def tg_call(token: str, method: str, params: dict | None = None, timeout: int = 60) -> dict:
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def api(method: str, params: dict | None = None, timeout: int = 60) -> dict:
    return tg_call(TOKEN, method, params, timeout)


def send(chat_id, text: str) -> None:
    text = text.strip() or "(empty response)"
    for i in range(0, len(text), TG_LIMIT):
        api("sendMessage", {"chat_id": chat_id, "text": text[i : i + TG_LIMIT]})


_IMAGE_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def download_telegram_file(file_id: str) -> bytes | None:
    """Download a Telegram-hosted file's raw bytes via getFile + the file API."""
    info = api("getFile", {"file_id": file_id})
    if not info.get("ok"):
        print(f"[bridge] getFile failed: {info.get('description') or info.get('error')}", flush=True)
        return None
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        print(f"[bridge] file download failed: {e}", flush=True)
        return None


def save_chat_media(chat_id, data: bytes, suffix: str) -> Path:
    """Persist downloaded media under a per-chat directory; returns the absolute path."""
    d = MEDIA_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{int(time.time() * 1000)}-{secrets.token_hex(4)}{suffix}"
    path.write_bytes(data)
    return path


TYPING_REFRESH = 4  # seconds; Telegram's "typing…" indicator fades after ~5s unread


def typing(chat_id) -> None:
    api("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def send_keyboard(chat_id, text: str, keyboard: dict):
    """Send a message with an inline keyboard attached. Returns the new message_id, or None on failure."""
    resp = api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": json.dumps(keyboard),
    })
    if not resp.get("ok"):
        print(f"[bridge] send_keyboard failed: {resp.get('error') or resp.get('description')}", flush=True)
        return None
    return resp["result"]["message_id"]


def edit_message(chat_id, message_id, text: str, keyboard: dict | None = None) -> None:
    """Edit an existing message's text (and reply_markup, if given) in place."""
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard is not None:
        params["reply_markup"] = json.dumps(keyboard)
    resp = api("editMessageText", params)
    if not resp.get("ok"):
        print(f"[bridge] edit_message failed: {resp.get('error') or resp.get('description')}", flush=True)


def answer_callback(callback_query_id: str, text: str = "") -> None:
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    api("answerCallbackQuery", params)


# ── omp runner ──────────────────────────────────────────────────────────────


def session_dir(chat_id) -> Path:
    d = SESSIONS / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def has_session(sdir: Path) -> bool:
    return any(sdir.glob("*.jsonl"))



def list_sessions(sdir: Path) -> list:
    """Every session (*.jsonl) in a chat's session dir, newest first."""
    sessions = []
    for f in sdir.glob("*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        sessions.append({"id": f.stem, "mtime": mtime})
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def _session_short_id(session_id: str) -> str:
    """Session files are named '<timestamp>_<uuid>'; the uuid prefix alone is
    enough to type into /resume and reads far better than the full stem."""
    uid = session_id.split("_", 1)[1] if "_" in session_id else session_id
    return uid[:8]


ACTIVE_SESSION_MARKER = ".active_session"


def get_active_session(sdir: Path) -> str | None:
    """The session /resume (or the last completed run) pinned as 'current'
    for this chat, or None to fall back to omp's own --continue heuristic."""
    try:
        session_id = (sdir / ACTIVE_SESSION_MARKER).read_text().strip()
    except OSError:
        return None
    if session_id and (sdir / f"{session_id}.jsonl").exists():
        return session_id
    return None


def set_active_session(sdir: Path, session_id: str | None) -> None:
    marker = sdir / ACTIVE_SESSION_MARKER
    try:
        if session_id:
            marker.write_text(session_id)
        else:
            marker.unlink(missing_ok=True)
    except OSError as e:
        print(f"[bridge] failed to update active session marker: {e}", flush=True)


def session_list_text(chat_id) -> str:
    sdir = session_dir(chat_id)
    sessions = list_sessions(sdir)
    if not sessions:
        return "\U0001f5c2 No sessions yet for this chat. Send a message to start one."
    active = get_active_session(sdir) or sessions[0]["id"]
    lines = ["\U0001f5c2 Sessions", ""]
    for s in sessions:
        marker = "\u2b50 " if s["id"] == active else "   "
        when = datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{marker}{when}  {_session_short_id(s['id'])}")
    lines.append("")
    lines.append("/resume <id> switches the active one (any unique substring works).")
    return "\n".join(lines)

IS_WINDOWS = sys.platform == "win32"

# Every long-lived child (omp itself, or a cron/login subprocess) is launched
# in its own process group so a kill takes shell children with it too —
# auto-approve runs shell commands, and killing just the launcher would
# strand them still holding the stdout pipe open, hanging communicate() past
# the kill. POSIX: a real process group + SIGKILL. Windows: CREATE_NEW_PROCESS_GROUP
# at spawn time, taskkill /T (tree) /F (force) to tear it down.
POPEN_GROUP_KWARGS = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
)


def _kill_process_group(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_omp(chat_id, message: str, attachments: list | None = None) -> str:
    sdir = session_dir(chat_id)
    active = get_active_session(sdir)
    cmd = [OMP_BIN, "-p", "--session-dir", str(sdir), "--auto-approve", "--cwd", str(WORKSPACE)]
    if active:
        cmd += ["--resume", active]
    elif has_session(sdir):
        cmd.append("--continue")
    if MODEL:
        cmd += ["--model", MODEL]
    for path in attachments or []:
        cmd.append(f"@{path}")
    cmd.append(message)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(WORKSPACE), **POPEN_GROUP_KWARGS
        )
    except OSError as e:
        return f"⚠️ failed to start omp: {e}"

    key = str(chat_id)
    start = time.monotonic()
    entry = {"proc": proc, "started": start, "stopped": False, "message": message[:80]}
    with _active_lock:
        _active_procs[key] = entry

    deadline = start + TIMEOUT
    next_heartbeat = start + HEARTBEAT_FIRST if (PROGRESS_ENABLED and HEARTBEAT_FIRST > 0) else None
    try:
        while True:
            if TYPING_ENABLED:
                typing(chat_id)  # re-armed every loop so the indicator never lapses mid-run
            try:
                stdout, _ = proc.communicate(timeout=TYPING_REFRESH)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if now >= deadline:
                    _kill_process_group(proc)
                    proc.communicate()
                    return f"⏱️ omp timed out after {TIMEOUT}s."
                if next_heartbeat is not None and now >= next_heartbeat:
                    # A real message, not just the typing indicator — Telegram's
                    # typing bubble fades after ~5s and our own re-arm cadence
                    # (every TYPING_REFRESH) can still read as "stuck" on long,
                    # tool-heavy turns.
                    try:
                        send(chat_id, HEARTBEAT_TEXT.format(elapsed=round((now - start) / 60)))
                    except Exception as e:  # noqa: BLE001
                        print(f"[bridge] heartbeat send failed: {e}", flush=True)
                    next_heartbeat = now + HEARTBEAT_INTERVAL if HEARTBEAT_INTERVAL > 0 else None
    except BaseException:
        _kill_process_group(proc)
        proc.communicate()
        raise
    finally:
        with _active_lock:
            _active_procs.pop(key, None)

    sessions = list_sessions(sdir)
    if sessions:
        set_active_session(sdir, sessions[0]["id"])

    if entry["stopped"]:
        return "🛑 Stopped."

    out = stdout.decode(errors="replace").strip()
    if proc.returncode != 0 and not out:
        return f"⚠️ omp exited {proc.returncode} with no output."
    return out or "(omp produced no output)"


# ── Active-run tracking (for /status, /abort, /stop) ────────────────────────

_active_procs: dict = {}
_active_lock = threading.Lock()


def stop_run(chat_id) -> tuple:
    """Kill the in-flight omp process for `chat_id` (if any) and drop any
    messages still queued behind it. Returns (was_running, dropped_count).
    """
    key = str(chat_id)
    with _active_lock:
        entry = _active_procs.get(key)
        if entry is not None:
            entry["stopped"] = True
    if entry is not None:
        _kill_process_group(entry["proc"])

    with _workers_lock:
        q = _workers.get(chat_id)
    dropped = 0
    if q is not None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break
            q.task_done()
            dropped += 1
    return entry is not None, dropped


def active_run_info(chat_id) -> dict | None:
    """Return {"started", "message"} for the in-flight run on this chat, or None."""
    with _active_lock:
        entry = _active_procs.get(str(chat_id))
        if entry is None:
            return None
        return {"started": entry["started"], "message": entry["message"]}


# ── Bot pause state (/start, /stop, /restart, /abort) ───────────────────────
#
# /stop now means "pause this chat until /start (or /restart)": cancel
# in-flight work exactly like the old /stop did, then flip this flag so
# handle_message ignores everything else until the chat is un-paused.
# /abort is the old /stop — cancel in-flight work, no pause. In-memory only
# (like _picker_sessions/_login_flows below): a bridge restart always comes
# back running for every chat.

_stopped_chats: set = set()
_stopped_lock = threading.Lock()
_ALWAYS_ALLOWED_WHILE_STOPPED = {"start", "restart", "status", "help", "stop", "abort"}


def is_stopped(chat_id) -> bool:
    with _stopped_lock:
        return str(chat_id) in _stopped_chats


def set_stopped(chat_id, value: bool) -> None:
    key = str(chat_id)
    with _stopped_lock:
        if value:
            _stopped_chats.add(key)
        else:
            _stopped_chats.discard(key)


def cancel_current_work(chat_id) -> list:
    """Kill the in-flight omp run and/or login attempt for this chat, drop
    queued messages, and return human-readable summary lines (maybe empty)."""
    stopped_run, dropped = stop_run(chat_id)
    login_stopped = stop_login(chat_id)
    parts = []
    if stopped_run and dropped:
        parts.append(f"\U0001f6d1 Stopping the current run and dropped {dropped} queued message(s).")
    elif stopped_run:
        parts.append("\U0001f6d1 Stopping the current run...")
    elif dropped:
        parts.append(f"\U0001f6d1 Dropped {dropped} queued message(s) (nothing was actively running).")
    if login_stopped:
        parts.append("\U0001f6d1 Cancelling the in-progress login...")
    return parts


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s}s"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {m}m"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h"


def status_text(chat_id) -> str:
    lines = ["\U0001f4ca Status", ""]
    lines.append(f"Version: {_bridge_version()} ({_bridge_commit()})")
    lines.append(f"Model: {MODEL or '(omp config default)'}")
    lines.append("Bot: \U0001f6d1 stopped for this chat (send /start to resume)" if is_stopped(chat_id) else "Bot: \u25b6 running")

    info = active_run_info(chat_id)
    if info:
        elapsed = int(time.monotonic() - info["started"])
        lines.append(f"This chat: running for {elapsed}s \u2014 \u201c{info['message']}\u201d")
    else:
        lines.append("This chat: idle")

    with _login_lock:
        login_flow = _login_flows.get(str(chat_id))
    if login_flow:
        lines.append(f"This chat: logging in to {login_flow['provider']['name']}")

    sdir = session_dir(chat_id)
    sessions = list_sessions(sdir)
    if sessions:
        active = get_active_session(sdir) or sessions[0]["id"]
        lines.append(f"Session: {_session_short_id(active)} active ({len(sessions)} total; /session to list)")
    else:
        lines.append("Session: none yet (next message starts fresh)")

    with _workers_lock:
        q = _workers.get(chat_id)
    queued = q.qsize() if q is not None else 0
    if queued:
        lines.append(f"Queued: {queued} message(s) waiting")

    lines.append(f"Bridge uptime: {_format_duration(time.monotonic() - _STARTED_AT)}")
    lines.append(f"omp: {OMP_BIN}")
    lines.append(f"Cron jobs: {len(CRON_JOBS)} loaded" if CRON_JOBS else "Cron jobs: none configured")
    lines.append("Access: everyone (OMP_BRIDGE_ALLOWED=*)" if ALLOW_ALL else f"Access: {len(ALLOWED)} allowed chat(s)")

    return "\n".join(lines)


# ── Self-update ───────────────────────────────────────────────────────────────
#
# /update pulls the latest commit from git and, if anything actually changed,
# schedules a *deferred* restart via a detached `systemd-run` timer instead of
# restarting inline. Restarting inline would `systemctl restart` the very
# cgroup this process lives in, killing the handler (and its own confirmation
# reply) before it could ever be sent.

REPO_DIR = Path(os.environ.get("OMP_BRIDGE_REPO_DIR", str(Path(__file__).resolve().parent)))
# Wherever this running process's own bridge.py actually lives — the systemd
# ExecStart target. When dev/prod are separated this is ~/.omp-agent/release,
# distinct from REPO_DIR (the git checkout); when they're not, it's the same
# directory and _deploy() below becomes a no-op.
RELEASE_DIR = Path(__file__).resolve().parent
UPDATE_RESTART_DELAY = 5  # seconds; long enough for the confirmation message to land first


def _validate_source(src: Path) -> tuple:
    """Byte-compile every .py in src. A pulled commit that doesn't even parse
    must be rejected here, never handed to the live service to crash on."""
    py_files = sorted(str(p) for p in src.glob("*.py"))
    if not py_files:
        return False, f"no .py files found in {src}"
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", *py_files],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return False, (proc.stdout + proc.stderr).strip()
    return True, ""


def _deploy(src: Path, dst: Path) -> None:
    """Atomically mirror src into dst (the systemd-managed release dir)."""
    tmp = dst.with_name(dst.name + ".new")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    if dst.exists():
        shutil.rmtree(dst)
    tmp.rename(dst)


def _bridge_version() -> str:
    """Read the VERSION file from REPO_DIR. 'unknown' if missing/unreadable —
    e.g. a checkout predating this file, or REPO_DIR misconfigured."""
    try:
        return (REPO_DIR / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _bridge_commit() -> str:
    rc, out = _run_git(["rev-parse", "--short", "HEAD"])
    return out if rc == 0 else "unknown"


def _run_git(args: list, timeout: int = 30) -> tuple:
    """Run a git command in REPO_DIR. Returns (returncode, combined stdout+stderr)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_DIR), *args], capture_output=True, text=True, timeout=timeout
        )
    except Exception as e:  # noqa: BLE001
        return 1, f"failed to run git: {e}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def update_bridge(chat_id) -> None:
    """Pull the latest bridge code and, if it changed, schedule a restart."""
    if not (REPO_DIR / ".git").exists():
        send(chat_id, f"⚠️ {REPO_DIR} isn't a git checkout — can't self-update. Set OMP_BRIDGE_REPO_DIR.")
        return

    rc, before = _run_git(["rev-parse", "HEAD"])
    if rc != 0:
        send(chat_id, f"⚠️ git rev-parse failed:\n{before}")
        return
    before_version = _bridge_version()

    rc, pull_out = _run_git(["pull", "--ff-only"])
    if rc != 0:
        send(chat_id, f"⚠️ git pull failed:\n{pull_out}")
        return

    rc, after = _run_git(["rev-parse", "HEAD"])
    if rc != 0:
        send(chat_id, f"⚠️ git rev-parse failed:\n{after}")
        return

    if after == before:
        send(chat_id, f"✅ Already up to date ({before_version}, {before[:7]}).")
        return

    ok, err = _validate_source(REPO_DIR)
    if not ok:
        _run_git(["reset", "--hard", before])  # don't leave the checkout on broken code
        send(chat_id, f"⚠️ pulled {after[:7]} but it fails to compile — rolled back to {before[:7]}:\n{err}")
        return

    if RELEASE_DIR != REPO_DIR:
        try:
            _deploy(REPO_DIR, RELEASE_DIR)
        except Exception as e:  # noqa: BLE001
            _run_git(["reset", "--hard", before])
            send(chat_id, f"⚠️ pulled {after[:7]} but deploying it to {RELEASE_DIR} failed — rolled back to {before[:7]}:\n{e}")
            return

    after_version = _bridge_version()  # re-read: the pull may have changed VERSION itself
    if after_version != before_version:
        version_line = f"{before_version} ({before[:7]}) \u2192 {after_version} ({after[:7]})"
    else:
        version_line = f"{after_version} ({before[:7]} \u2192 {after[:7]})"

    _, log_out = _run_git(["log", "--oneline", f"{before}..{after}"])
    lines = [f"⬆️ Updated {version_line}:", log_out]

    if not shutil.which("systemctl"):
        lines.append("\n⚠️ No systemd found — restart manually to load the new code (python3 bridge.py).")
        send(chat_id, "\n".join(lines))
        return

    with _active_lock:
        active_count = len(_active_procs)
    if active_count:
        lines.append(f"\n⚠️ {active_count} run(s) in progress (this chat or others) will be interrupted.")

    lines.append(f"\nRestarting in {UPDATE_RESTART_DELAY}s to load the new code — give it a few seconds, then say hi.")
    send(chat_id, "\n".join(lines))

    unit = f"omp-bridge-update-{int(time.time())}"
    try:
        subprocess.run(
            [
                "systemd-run", "--user",
                f"--on-active={UPDATE_RESTART_DELAY}s",
                f"--unit={unit}",
                "--description=omp-bridge self-update restart",
                "systemctl", "--user", "restart", "omp-bridge",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        send(chat_id, f"⚠️ pulled the update but couldn't schedule the restart: {e}\nRestart manually.")


# ── Model catalog & picker ───────────────────────────────────────────────────
#
# Backs the interactive /model picker. `omp models --json` reports every
# model the locally configured providers can actually serve — only
# providers with credentials show up, i.e. "connected" models, not omp's
# full static catalog. Grouped by provider into an inline keyboard, drilled
# down provider -> model, edited in place as the user navigates.

MODELS_CACHE_TTL = 300  # seconds; `omp models --json` shells out + hits the catalog db
PROVIDER_PAGE_SIZE = 10
MODEL_PAGE_SIZE = 8

_models_cache: dict = {"data": None, "fetched_at": 0.0}

# Friendly display names for common provider slugs; anything else falls back
# to a title-cased version of the slug, so a newly authenticated provider
# shows up immediately without a code change.
_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openai-codex": "OpenAI Codex",
    "google": "Google",
    "google-vertex": "Google Vertex",
    "azure": "Azure OpenAI",
    "bedrock": "AWS Bedrock",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "mistral": "Mistral",
    "deepseek": "DeepSeek",
    "xai": "xAI",
    "cohere": "Cohere",
    "ollama": "Ollama",
    "together": "Together AI",
}


def _provider_label(slug: str) -> str:
    return _PROVIDER_LABELS.get(slug, slug.replace("-", " ").replace("_", " ").title())


def get_models(force: bool = False) -> list:
    """Return the connected model catalog, caching for MODELS_CACHE_TTL seconds.

    Falls back to a stale cache (rather than an empty list) if `omp models`
    fails transiently, so a picker mid-navigation doesn't suddenly go blank.
    """
    now = time.monotonic()
    cached = _models_cache["data"]
    if cached is not None and not force and (now - _models_cache["fetched_at"]) < MODELS_CACHE_TTL:
        return cached

    try:
        proc = subprocess.run([OMP_BIN, "models", "--json"], capture_output=True, text=True, timeout=30)
        models = json.loads(proc.stdout).get("models", [])
    except Exception as e:  # noqa: BLE001
        print(f"[bridge] failed to load model catalog: {e}", flush=True)
        return cached or []

    _models_cache["data"] = models
    _models_cache["fetched_at"] = now
    return models


def group_by_provider(models: list, current_selector: str) -> list:
    """Group models by provider, sorted by display name.

    Each entry keeps its (selector, name) — `selector` is exactly what
    `--model` accepts, `name` is the friendly display string.
    """
    by_slug: dict = {}
    for m in models:
        slug = m.get("provider", "")
        if not slug:
            continue
        by_slug.setdefault(slug, []).append({"selector": m["selector"], "name": m.get("name", m["id"])})

    current_provider = (
        current_selector.split("/", 1)[0] if current_selector and "/" in current_selector else None
    )

    providers = []
    for slug, entries in by_slug.items():
        entries.sort(key=lambda e: e["selector"])
        providers.append(
            {
                "slug": slug,
                "name": _provider_label(slug),
                "models": entries,
                "total": len(entries),
                "is_current": slug == current_provider,
            }
        )
    providers.sort(key=lambda p: p["name"].lower())
    return providers


def search_models(models: list, query: str) -> list:
    """Substring match against selector or display name, case-insensitive."""
    q = query.strip().lower()
    if not q:
        return []
    matches = [
        {"selector": m["selector"], "name": m.get("name", m["id"])}
        for m in models
        if q in m["selector"].lower() or q in m.get("name", "").lower()
    ]
    matches.sort(key=lambda e: e["selector"])
    return matches


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def build_provider_keyboard(providers: list, page: int = 0) -> tuple:
    """Paginated top-level provider picker. Returns (keyboard, page, total_pages)."""
    total = len(providers)
    total_pages = max(1, -(-total // PROVIDER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PROVIDER_PAGE_SIZE
    chunk = providers[start : start + PROVIDER_PAGE_SIZE]

    buttons = []
    for p in chunk:
        label = f"{p['name']} ({p['total']})"
        if p["is_current"]:
            label = f"\u2713 {label}"
        buttons.append(_btn(label, f"mp:{p['slug']}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("\u25c0 Prev", f"pg:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{total_pages}", "mx:noop"))
        if page < total_pages - 1:
            nav.append(_btn("Next \u25b6", f"pg:{page + 1}"))
        rows.append(nav)

    rows.append([_btn("\U0001f504 Refresh", "mr"), _btn("\u2717 Cancel", "mx")])
    return {"inline_keyboard": rows}, page, total_pages


def build_model_keyboard(entries: list, page: int = 0, show_back: bool = True) -> tuple:
    """Paginated model picker. Returns (keyboard, page_info_suffix_for_header)."""
    total = len(entries)
    total_pages = max(1, -(-total // MODEL_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * MODEL_PAGE_SIZE
    end = min(start + MODEL_PAGE_SIZE, total)

    buttons = []
    for i, entry in enumerate(entries[start:end]):
        abs_idx = start + i
        label = entry["name"] or entry["selector"]
        if len(label) > 38:
            label = label[:35] + "..."
        buttons.append(_btn(label, f"mm:{abs_idx}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("\u25c0 Prev", f"mg:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{total_pages}", "mx:noop"))
        if page < total_pages - 1:
            nav.append(_btn("Next \u25b6", f"mg:{page + 1}"))
        rows.append(nav)

    last_row = [_btn("\u25c0 Back", "mb")] if show_back else []
    last_row.append(_btn("\u2717 Cancel", "mx"))
    rows.append(last_row)

    page_info = f" ({start + 1}\u2013{end} of {total})" if total_pages > 1 else ""
    return {"inline_keyboard": rows}, page_info


# In-memory picker session state, keyed by str(chat_id). Deliberately not
# persisted to disk: a picker left open across a bridge restart just reports
# "expired" and the user re-issues /model.
_picker_sessions: dict = {}
_picker_lock = threading.Lock()


def _model_header(current: str) -> str:
    return f"\u2699 Model Configuration\n\nCurrent model: {current or '(omp config default)'}\nSelect a provider:"


def send_model_picker(chat_id) -> None:
    models = get_models()
    if not models:
        send(
            chat_id,
            "\u26a0\ufe0f No connected models found. Check the provider credentials "
            "`omp` is configured with (`omp models` on the host).",
        )
        return
    providers = group_by_provider(models, MODEL)
    keyboard, _page, _total_pages = build_provider_keyboard(providers, 0)
    msg_id = send_keyboard(chat_id, _model_header(MODEL), keyboard)
    if msg_id is not None:
        with _picker_lock:
            _picker_sessions[str(chat_id)] = {"mode": "provider", "providers": providers, "page": 0}


def send_model_search_results(chat_id, query: str, matches: list) -> None:
    keyboard, page_info = build_model_keyboard(matches, 0, show_back=False)
    text = f"\u2699 Model Configuration\n\n{len(matches)} models match '{query}'{page_info}\nSelect one:"
    msg_id = send_keyboard(chat_id, text, keyboard)
    if msg_id is not None:
        with _picker_lock:
            _picker_sessions[str(chat_id)] = {"mode": "search", "list": matches, "page": 0}


def handle_callback_query(cq: dict) -> None:
    data = cq.get("data") or ""
    cq_id = cq.get("id")
    message = cq.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if chat_id is None or message_id is None:
        if cq_id:
            answer_callback(cq_id)
        return

    if not authorized(chat_id):
        answer_callback(cq_id, "\u26d4 Not authorized.")
        return

    key = str(chat_id)
    if data == "cfg:typing":
        set_typing_enabled(not TYPING_ENABLED)
        edit_message(chat_id, message_id, _config_text(), _config_keyboard())
        answer_callback(cq_id, "Typing status " + ("enabled" if TYPING_ENABLED else "disabled"))
        return

    if data == "cfg:progress":
        set_progress_enabled(not PROGRESS_ENABLED)
        edit_message(chat_id, message_id, _config_text(), _config_keyboard())
        answer_callback(cq_id, "Progress updates " + ("enabled" if PROGRESS_ENABLED else "disabled"))
        return

    if data == "mx:noop":
        answer_callback(cq_id)
        return

    if data in ("lc:yes", "lc:no"):
        with _login_lock:
            flow = _login_flows.get(key)
        if flow is None or flow.get("awaiting") != "confirm":
            answer_callback(cq_id, "No pending confirmation.")
            return
        flow["answers"].put(data == "lc:yes")
        edit_message(chat_id, message_id, "\u2753 " + ("Yes" if data == "lc:yes" else "No"), {"inline_keyboard": []})
        answer_callback(cq_id)
        return

    with _picker_lock:
        session = _picker_sessions.get(key)
    if session is None:
        answer_callback(cq_id, "Picker expired \u2014 use /model or /login again.")
        return

    if data == "mx":
        with _picker_lock:
            _picker_sessions.pop(key, None)
        edit_message(chat_id, message_id, "Model selection cancelled.", {"inline_keyboard": []})
        answer_callback(cq_id)
        return

    if data == "mr":
        models = get_models(force=True)
        providers = group_by_provider(models, MODEL)
        session["mode"], session["providers"], session["page"] = "provider", providers, 0
        keyboard, _page, _total_pages = build_provider_keyboard(providers, 0)
        edit_message(chat_id, message_id, _model_header(MODEL), keyboard)
        answer_callback(cq_id, "Refreshed.")
        return

    if data == "mb":
        if session.get("mode") == "model":
            session["mode"] = "provider"
            keyboard, _page, _total_pages = build_provider_keyboard(
                session["providers"], session.get("provider_page", 0)
            )
            edit_message(chat_id, message_id, _model_header(MODEL), keyboard)
        else:
            with _picker_lock:
                _picker_sessions.pop(key, None)
            edit_message(chat_id, message_id, "Model selection cancelled.", {"inline_keyboard": []})
        answer_callback(cq_id)
        return

    if data.startswith("pg:"):
        if session.get("mode") != "provider":
            answer_callback(cq_id)
            return
        page = int(data.split(":", 1)[1])
        session["page"] = page
        keyboard, _page, _total_pages = build_provider_keyboard(session["providers"], page)
        edit_message(chat_id, message_id, _model_header(MODEL), keyboard)
        answer_callback(cq_id)
        return

    if data.startswith("mp:"):
        slug = data.split(":", 1)[1]
        provider = next((p for p in session.get("providers", []) if p["slug"] == slug), None)
        if provider is None:
            answer_callback(cq_id, "Provider not found.")
            return
        session["mode"] = "model"
        session["provider_page"] = session.get("page", 0)
        session["list"] = provider["models"]
        session["model_page"] = 0
        session["provider_name"] = provider["name"]
        keyboard, page_info = build_model_keyboard(provider["models"], 0)
        edit_message(
            chat_id,
            message_id,
            f"\u2699 Model Configuration\n\nProvider: {provider['name']}{page_info}\nSelect a model:",
            keyboard,
        )
        answer_callback(cq_id)
        return

    if data.startswith("mg:"):
        mode = session.get("mode")
        if mode not in ("model", "search"):
            answer_callback(cq_id)
            return
        page = int(data.split(":", 1)[1])
        session["model_page" if mode == "model" else "page"] = page
        entries = session.get("list", [])
        keyboard, page_info = build_model_keyboard(entries, page, show_back=(mode == "model"))
        if mode == "model":
            text = f"\u2699 Model Configuration\n\nProvider: {session['provider_name']}{page_info}\nSelect a model:"
        else:
            text = f"\u2699 Model Configuration\n\n{len(entries)} models match your search{page_info}\nSelect one:"
        edit_message(chat_id, message_id, text, keyboard)
        answer_callback(cq_id)
        return

    if data.startswith("mm:"):
        idx = int(data.split(":", 1)[1])
        entries = session.get("list", [])
        if idx < 0 or idx >= len(entries):
            answer_callback(cq_id, "Invalid selection.")
            return
        entry = entries[idx]
        set_model(entry["selector"])
        with _picker_lock:
            _picker_sessions.pop(key, None)
        edit_message(
            chat_id, message_id,
            f"\u2705 Switched to {entry['selector']} ({entry['name']})",
            {"inline_keyboard": []},
        )
        answer_callback(cq_id, "Model switched!")
        return

    if data == "lx:noop":
        answer_callback(cq_id)
        return

    if data == "lx":
        with _picker_lock:
            _picker_sessions.pop(key, None)
        edit_message(chat_id, message_id, "Login cancelled.", {"inline_keyboard": []})
        answer_callback(cq_id)
        return

    if data.startswith("lg:"):
        if not session.get("login"):
            answer_callback(cq_id)
            return
        page = int(data.split(":", 1)[1])
        session["page"] = page
        keyboard, _page, _total_pages = build_login_keyboard(session["providers"], page)
        edit_message(chat_id, message_id, session.get("header", _login_header()), keyboard)
        answer_callback(cq_id)
        return

    if data.startswith("lp:"):
        if not session.get("login"):
            answer_callback(cq_id)
            return
        provider_id = data.split(":", 1)[1]
        provider = next((p for p in session.get("providers", []) if p["id"] == provider_id), None)
        if provider is None:
            answer_callback(cq_id, "Provider not found.")
            return
        with _picker_lock:
            _picker_sessions.pop(key, None)
        edit_message(chat_id, message_id, f"\U0001f510 Starting login to {provider['name']}...", {"inline_keyboard": []})
        answer_callback(cq_id, "Starting...")
        start_login(chat_id, provider)
        return

    answer_callback(cq_id)


# ── Login ────────────────────────────────────────────────────────────────────
#
# /login mirrors omp's own interactive `/login` slash command — OAuth or an
# API-key paste against one of ~50 providers — which only exists as a TTY
# flow. `omp --mode rpc` is the one surface built to answer those prompts
# programmatically: get_login_providers lists them, login/providerId starts
# one, and whatever it needs back (a URL to open, a value to paste, a
# yes/no) arrives as extension_ui_request frames this module answers over a
# background thread for as long as the flow takes — an OAuth round trip can
# sit for minutes waiting on the user's browser.

LOGIN_PROVIDERS_CACHE_TTL = 300  # seconds
LOGIN_PAGE_SIZE = 10
LOGIN_TIMEOUT = 900  # seconds; generous enough for a slow OAuth round trip

_login_providers_cache: dict = {"data": None, "fetched_at": 0.0}

# One entry per chat with a login attempt in flight, keyed by str(chat_id).
# `answers` is how the Telegram-side handlers (a plain-text reply, a Yes/No
# button tap) hand a value back to the worker thread blocked inside
# _handle_login_ui_request. `awaiting` is None, "text", or "confirm".
_login_flows: dict = {}
_login_lock = threading.Lock()
_LOGIN_CANCELLED = object()  # sentinel pushed to `answers` by /stop or /abort


def _rpc_call(command: dict, timeout: int = 20) -> dict | None:
    """Run a single command through a one-shot `omp --mode rpc` process: write
    it, close stdin, collect the matching response frame. Per the RPC docs,
    omp exits on its own once stdin closes and processing settles."""
    payload = json.dumps(command) + "\n"
    try:
        proc = subprocess.run(
            [OMP_BIN, "--mode", "rpc", "--no-session", "--cwd", str(WORKSPACE)],
            input=payload, capture_output=True, text=True, timeout=timeout,
        )
        out = proc.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
    except OSError as e:
        print(f"[bridge] failed to start omp rpc: {e}", flush=True)
        return None
    for line in (out or "").splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if frame.get("type") == "response" and frame.get("id") == command.get("id"):
            return frame
    return None


def get_login_providers(force: bool = False) -> list:
    """Return the full provider list `/login` can target, caching for
    LOGIN_PROVIDERS_CACHE_TTL seconds (mirrors get_models()'s caching)."""
    now = time.monotonic()
    cached = _login_providers_cache["data"]
    if cached is not None and not force and (now - _login_providers_cache["fetched_at"]) < LOGIN_PROVIDERS_CACHE_TTL:
        return cached
    resp = _rpc_call({"id": "providers", "type": "get_login_providers"})
    if resp is None or not resp.get("success"):
        print(f"[bridge] failed to load login providers: {resp}", flush=True)
        return cached or []
    providers = resp.get("data", {}).get("providers", [])
    _login_providers_cache["data"] = providers
    _login_providers_cache["fetched_at"] = now
    return providers


def search_login_providers(providers: list, query: str) -> list:
    """Substring match against provider id or display name, case-insensitive."""
    q = query.strip().lower()
    if not q:
        return []
    matches = [p for p in providers if q in p["id"].lower() or q in p["name"].lower()]
    matches.sort(key=lambda p: p["name"].lower())
    return matches


def _login_label(p: dict) -> str:
    label = ("\u2713 " if p.get("authenticated") else "") + p["name"]
    return label if len(label) <= 42 else label[:39] + "..."


def build_login_keyboard(providers: list, page: int = 0) -> tuple:
    """Paginated, one-button-per-row provider picker (names run long)."""
    total = len(providers)
    total_pages = max(1, -(-total // LOGIN_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * LOGIN_PAGE_SIZE
    chunk = providers[start : start + LOGIN_PAGE_SIZE]

    rows = [[_btn(_login_label(p), f"lp:{p['id']}")] for p in chunk]
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("\u25c0 Prev", f"lg:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{total_pages}", "lx:noop"))
        if page < total_pages - 1:
            nav.append(_btn("Next \u25b6", f"lg:{page + 1}"))
        rows.append(nav)
    rows.append([_btn("\u2717 Cancel", "lx")])
    return {"inline_keyboard": rows}, page, total_pages


def _login_header() -> str:
    return "\U0001f510 Login\n\nSelect a provider (\u2713 = already logged in):"


def send_login_picker(chat_id) -> None:
    providers = get_login_providers()
    if not providers:
        send(chat_id, "\u26a0\ufe0f Couldn't load the login provider list from omp.")
        return
    header = _login_header()
    keyboard, _page, _total_pages = build_login_keyboard(providers, 0)
    msg_id = send_keyboard(chat_id, header, keyboard)
    if msg_id is not None:
        with _picker_lock:
            _picker_sessions[str(chat_id)] = {"login": True, "providers": providers, "page": 0, "header": header}


def send_login_search_results(chat_id, query: str, matches: list) -> None:
    header = f"\U0001f510 Login\n\n{len(matches)} provider(s) match '{query}'\nSelect one:"
    keyboard, _page, _total_pages = build_login_keyboard(matches, 0)
    msg_id = send_keyboard(chat_id, header, keyboard)
    if msg_id is not None:
        with _picker_lock:
            _picker_sessions[str(chat_id)] = {"login": True, "providers": matches, "page": 0, "header": header}


def start_login(chat_id, provider: dict) -> None:
    key = str(chat_id)
    with _login_lock:
        if key in _login_flows:
            send(chat_id, "\u26a0\ufe0f A login attempt is already in progress for this chat. /abort to cancel it.")
            return
        _login_flows[key] = {"provider": provider, "answers": queue.Queue(), "proc": None, "awaiting": None}
    threading.Thread(target=_login_worker, args=(chat_id, provider), daemon=True).start()


def stop_login(chat_id) -> bool:
    """Cancel any in-flight /login attempt for this chat. Returns True if one
    was cancelled. If a prompt is pending, unblocks it gracefully so omp gets
    a real `extension_ui_response`; otherwise there's nothing to answer, so
    the RPC process is just killed."""
    key = str(chat_id)
    with _login_lock:
        flow = _login_flows.get(key)
        if flow is None:
            return False
        flow["cancel_requested"] = True
        awaiting, proc = flow.get("awaiting"), flow.get("proc")
    if awaiting in ("text", "confirm"):
        flow["answers"].put(_LOGIN_CANCELLED)
    elif proc is not None:
        _kill_process_group(proc)
    return True


def _login_send_cmd(proc: subprocess.Popen, obj: dict) -> None:
    try:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _handle_login_ui_request(chat_id, key: str, frame: dict, proc: subprocess.Popen, deadline: float) -> bool:
    """Answer one extension_ui_request from an in-flight login. Returns False
    if the flow was cancelled (by /stop, /abort, or a timeout) and the caller should
    stop; True to keep listening for more frames."""
    method = frame.get("method")
    ui_id = frame.get("id")

    if method == "open_url":
        url = frame.get("url", "")
        instructions = frame.get("instructions") or ""
        send(chat_id, (f"{instructions}\n{url}" if instructions else url).strip())
        return True

    if method == "notify":
        send(chat_id, frame.get("message", ""))
        return True

    if method not in ("input", "select", "confirm"):
        return True  # setWidget/setTitle/setStatus/etc: informational, no response needed

    with _login_lock:
        flow = _login_flows.get(key)
        if flow is None:
            return False
        flow["awaiting"] = "confirm" if method == "confirm" else "text"
        flow["ui_id"] = ui_id
        answers = flow["answers"]

    if method == "confirm":
        msg = frame.get("message") or frame.get("title") or "Confirm?"
        send_keyboard(chat_id, f"\u2753 {msg}", {"inline_keyboard": [[_btn("Yes", "lc:yes"), _btn("No", "lc:no")]]})
    else:
        title = frame.get("title") or frame.get("message") or "Input needed"
        prompt = f"\u270f\ufe0f {title}"
        placeholder = frame.get("placeholder")
        if placeholder:
            prompt += f"\n(e.g. {placeholder})"
        options = frame.get("options")
        if options:
            opt_lines = "\n".join(f"  {o.get('label', o) if isinstance(o, dict) else o}" for o in options)
            prompt += f"\nOptions:\n{opt_lines}"
        prompt += "\n\nReply with the value, or /abort to cancel."
        send(chat_id, prompt)

    remaining = deadline - time.monotonic()
    wait_for = max(1.0, min(remaining, (frame.get("timeout") or LOGIN_TIMEOUT * 1000) / 1000))
    try:
        value = answers.get(timeout=wait_for)
    except queue.Empty:
        _login_send_cmd(proc, {"type": "extension_ui_response", "id": ui_id, "cancelled": True, "timedOut": True})
        return False

    with _login_lock:
        flow = _login_flows.get(key)
        if flow:
            flow["awaiting"] = None

    if value is _LOGIN_CANCELLED:
        _login_send_cmd(proc, {"type": "extension_ui_response", "id": ui_id, "cancelled": True})
        return False
    if method == "confirm":
        _login_send_cmd(proc, {"type": "extension_ui_response", "id": ui_id, "confirmed": bool(value)})
    else:
        _login_send_cmd(proc, {"type": "extension_ui_response", "id": ui_id, "value": str(value)})
    return True


def _login_worker(chat_id, provider: dict) -> None:
    key = str(chat_id)
    provider_id, provider_name = provider["id"], provider["name"]
    try:
        proc = subprocess.Popen(
            [OMP_BIN, "--mode", "rpc", "--no-session", "--cwd", str(WORKSPACE)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, **POPEN_GROUP_KWARGS,
        )
    except OSError as e:
        send(chat_id, f"\u26a0\ufe0f couldn't start omp: {e}")
        with _login_lock:
            _login_flows.pop(key, None)
        return

    with _login_lock:
        flow = _login_flows.get(key)
        if flow is None:  # /stop or /abort already cancelled before the process even started
            _kill_process_group(proc)
            return
        flow["proc"] = proc

    lines: "queue.Queue" = queue.Queue()

    def pump() -> None:
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if raw:
                    lines.put(raw)
        except Exception:  # noqa: BLE001
            pass
        lines.put(None)  # sentinel: stdout closed

    threading.Thread(target=pump, daemon=True).start()

    req_id = "login"
    _login_send_cmd(proc, {"id": req_id, "type": "login", "providerId": provider_id})

    deadline = time.monotonic() + LOGIN_TIMEOUT
    result = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = f"\u231b Login to {provider_name} timed out."
                break
            try:
                line = lines.get(timeout=min(remaining, 5))
            except queue.Empty:
                continue
            if line is None:
                with _login_lock:
                    cancelled = bool(_login_flows.get(key, {}).get("cancel_requested"))
                result = (
                    f"\U0001f6d1 Login to {provider_name} cancelled."
                    if cancelled
                    else f"\u26a0\ufe0f Lost connection to omp during login to {provider_name}."
                )
                break
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue

            ftype = frame.get("type")
            if ftype == "response" and frame.get("id") == req_id:
                if frame.get("success"):
                    result = f"\u2705 Logged in to {provider_name}."
                else:
                    result = f"\u274c Login to {provider_name} failed: {frame.get('error', 'unknown error')}"
                break
            if ftype == "extension_ui_request":
                if not _handle_login_ui_request(chat_id, key, frame, proc, deadline):
                    result = f"\U0001f6d1 Login to {provider_name} cancelled."
                    break
    finally:
        with _login_lock:
            _login_flows.pop(key, None)
        _kill_process_group(proc)

    if result:
        send(chat_id, result)


# ── Config ───────────────────────────────────────────────────────────────────


def _config_text() -> str:
    typing_state = "\u2705 ON" if TYPING_ENABLED else "\u2b1c OFF"
    progress_state = "\u2705 ON" if PROGRESS_ENABLED else "\u2b1c OFF"
    return (
        "\u2699 Configuration\n\n"
        f"\u2328\ufe0f Typing status: {typing_state}\n"
        f"\U0001f4ca Progress updates: {progress_state} "
        f"(first nudge after {HEARTBEAT_FIRST // 60} min, then every {HEARTBEAT_INTERVAL // 60} min)\n\n"
        "Tap a button to toggle."
    )


def _config_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [_btn(
                "Typing status: " + ("ON \u2192 tap to turn off" if TYPING_ENABLED else "OFF \u2192 tap to turn on"),
                "cfg:typing",
            )],
            [_btn(
                "Progress updates: " + ("ON \u2192 tap to turn off" if PROGRESS_ENABLED else "OFF \u2192 tap to turn on"),
                "cfg:progress",
            )],
        ]
    }


def send_config(chat_id) -> None:
    send_keyboard(chat_id, _config_text(), _config_keyboard())


# ── Cron scheduler ────────────────────────────────────────────────────────────
#
# Job definitions live in a data file (default ~/.omp-agent/cron.json), not in
# this source — mirrors how the bridge's own config lives in .env. Each job:
#   id         stable string, used as the dedupe key in cron_state.json
#   name       display name, used only in [cron] log lines
#   schedule   5-field cron expression (minute hour day month weekday), evaluated
#              in the machine's local time zone
#   chat_id    Telegram chat to deliver to (group ids are negative)
#   thread_id  optional forum-topic id within that chat
#   kind       "script" — run `argv`, deliver stdout verbatim
#              "prompt" — run `omp -p <prompt>` (optionally scoped by `tools`,
#                         overridden by `model`), deliver its stdout
#   timeout    seconds before the job is killed; default 60 for scripts, 300 for prompts
# A job silent on stdout (matching the migrated Hermes scripts' own convention)
# delivers nothing — that's not a failure, just "nothing to report today."

CRON_CHECK_INTERVAL = 20  # seconds; coarse poll, still fine enough to not miss a minute


def _load_cron_jobs(path: Path) -> list:
    if not path.exists():
        return []
    try:
        jobs = json.loads(path.read_text()).get("jobs", [])
    except Exception as e:  # noqa: BLE001
        print(f"[cron] failed to parse {path}: {e}", flush=True)
        return []
    return [j for j in jobs if j.get("enabled", True)]


def _load_cron_state() -> dict:
    if CRON_STATE_FILE.exists():
        try:
            return json.loads(CRON_STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_cron_state(state: dict) -> None:
    try:
        CRON_STATE_FILE.write_text(json.dumps(state))
    except OSError as e:
        print(f"[cron] failed to persist state: {e}", flush=True)


def _cron_field_matches(spec: str, value: int) -> bool:
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            if value % step == 0:
                return True
            continue
        lo, hi = (int(x) for x in part.split("-", 1)) if "-" in part else (int(part), int(part))
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
    return False


def cron_due(expr: str, dt: datetime) -> bool:
    """True when `dt` matches a standard 5-field cron expression, in dt's zone."""
    minute, hour, dom, month, dow = expr.split()
    py_dow = dt.isoweekday() % 7  # cron: 0=Sunday..6=Saturday; Python: 1=Mon..7=Sun
    dow_candidates = (py_dow, 7) if py_dow == 0 else (py_dow,)
    return (
        _cron_field_matches(minute, dt.minute)
        and _cron_field_matches(hour, dt.hour)
        and _cron_field_matches(dom, dt.day)
        and _cron_field_matches(month, dt.month)
        and any(_cron_field_matches(dow, c) for c in dow_candidates)
    )


def deliver(chat_id: str, text: str, thread_id: str | None = None) -> None:
    """Send cron output verbatim; silently does nothing for empty text (not an error)."""
    text = text.strip()
    if not text:
        return
    params = {"chat_id": chat_id}
    if thread_id:
        params["message_thread_id"] = thread_id
    for i in range(0, len(text), TG_LIMIT):
        api("sendMessage", dict(params, text=text[i : i + TG_LIMIT]))


def _run_cron_subprocess(argv: list, timeout: int) -> str:
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **POPEN_GROUP_KWARGS)
    except OSError as e:
        print(f"[cron] failed to start {argv!r}: {e}", flush=True)
        return ""
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.communicate()
        print(f"[cron] {argv!r} timed out after {timeout}s", flush=True)
        return ""
    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()[:500]
        print(f"[cron] {argv!r} exited {proc.returncode}: {err}", flush=True)
    return stdout.decode(errors="replace").strip()


def _run_cron_job(job: dict) -> str:
    kind = job.get("kind")
    if kind == "script":
        return _run_cron_subprocess(job["argv"], job.get("timeout", 60))
    if kind == "prompt":
        cmd = [OMP_BIN, "-p", "--no-session", "--auto-approve", "--cwd", str(WORKSPACE)]
        model = job.get("model") or MODEL
        if model:
            cmd += ["--model", model]
        if job.get("tools"):
            cmd += ["--tools", job["tools"]]
        cmd.append(job["prompt"])
        return _run_cron_subprocess(cmd, job.get("timeout", 300))
    print(f"[cron] job {job.get('id')!r} has unknown kind {kind!r}", flush=True)
    return ""


def _cron_worker(job: dict) -> None:
    print(f"[cron] running {job.get('name', job.get('id'))!r}", flush=True)
    try:
        text = _run_cron_job(job)
    except Exception as e:  # noqa: BLE001
        print(f"[cron] {job.get('name', job.get('id'))!r} failed: {e}", flush=True)
        return
    deliver(job["chat_id"], text, job.get("thread_id"))
    print(f"[cron] {job.get('name', job.get('id'))!r} {'delivered' if text.strip() else 'silent (no output)'}", flush=True)


def _cron_loop() -> None:
    if not CRON_JOBS:
        return
    print(f"[cron] {len(CRON_JOBS)} job(s) loaded from {CRON_FILE}", flush=True)
    state = _load_cron_state()
    while True:
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        for job in CRON_JOBS:
            job_id = job.get("id")
            if not job_id or state.get(job_id) == minute_key:
                continue
            if cron_due(job.get("schedule", ""), now):
                state[job_id] = minute_key
                _save_cron_state(state)
                threading.Thread(target=_cron_worker, args=(job,), daemon=True).start()
        time.sleep(CRON_CHECK_INTERVAL)


# ── Per-chat workers ─────────────────────────────────────────────────────────

_workers: dict = {}
_workers_lock = threading.Lock()


def _worker_loop(chat_id, q: "queue.Queue") -> None:
    while True:
        message, attachments = q.get()
        try:
            reply = run_omp(chat_id, message, attachments)
            send(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            send(chat_id, f"⚠️ bridge error: {e}")
        finally:
            q.task_done()


def enqueue(chat_id, message: str, attachments: list | None = None) -> None:
    with _workers_lock:
        q = _workers.get(chat_id)
        if q is None:
            q = queue.Queue()
            _workers[chat_id] = q
            threading.Thread(target=_worker_loop, args=(chat_id, q), daemon=True).start()
    q.put((message, attachments or []))


# ── Message handling ─────────────────────────────────────────────────────────


def authorized(chat_id) -> bool:
    return ALLOW_ALL or str(chat_id) in ALLOWED


def handle_message(msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()
    attachments: list[str] = []
    photo = msg.get("photo")
    document = msg.get("document")
    image_document = (
        document if document and (document.get("mime_type") or "").startswith("image/") else None
    )

    if not text and not photo and not image_document:
        return

    if not authorized(chat_id):
        send(chat_id, f"⛔ Not authorized. Your chat id is {chat_id}.")
        print(f"[bridge] denied chat {chat_id}", flush=True)
        return

    if is_stopped(chat_id):
        cmd_probe = text.split()[0].lstrip("/").split("@")[0].lower() if not attachments and text.startswith("/") else ""
        if cmd_probe not in _ALWAYS_ALLOWED_WHILE_STOPPED:
            send(chat_id, "\U0001f6d1 Bot is stopped for this chat. Send /start to resume.")
            return

    key = str(chat_id)
    with _login_lock:
        flow = _login_flows.get(key)
    if flow is not None and flow.get("awaiting") == "text":
        stopword = text.split()[0].lower() if text.startswith("/") else ""
        if stopword not in ("/stop", "/abort", "/cancel"):
            flow["answers"].put(text)
            send(chat_id, "\U0001f44d Got it, continuing login...")
            return

    if photo or image_document:
        if photo:
            file_id, suffix = photo[-1]["file_id"], ".jpg"
        else:
            file_id = image_document["file_id"]
            name_ext = Path(image_document.get("file_name", "")).suffix
            suffix = name_ext or _IMAGE_MIME_EXT.get(image_document.get("mime_type", ""), ".jpg")
        data = download_telegram_file(file_id)
        if data is None:
            send(chat_id, "⚠️ Couldn't download your image. Please try sending it again.")
            return
        attachments.append(str(save_chat_media(chat_id, data, suffix)))
        if not text:
            text = "Take a look at the attached image and tell me what you see."
    if not attachments and text.startswith("/"):
        cmd = text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd == "start":
            was_stopped = is_stopped(chat_id)
            set_stopped(chat_id, False)
            prefix = "\u25b6 Resumed. " if was_stopped else ""
            send(chat_id, prefix + "🤖 omp bridge online. Send a message and I'll run it through omp. /reset clears our conversation, /model shows or changes the model, /login connects a provider account, /config toggles typing/progress notifications, /session lists past sessions (/resume <id> to switch), /status shows what's running, /abort cancels the current run, /stop pauses the bot here.")
            return
        if cmd == "restart":
            parts = cancel_current_work(chat_id)
            was_stopped = is_stopped(chat_id)
            set_stopped(chat_id, False)
            parts.append("\U0001f504 Restarted — bot resumed for this chat." if was_stopped else "\U0001f504 Restarted — cancelled any in-flight work, bot is online.")
            send(chat_id, "\n".join(parts))
            return
        if cmd == "reset":
            sdir = session_dir(chat_id)
            shutil.rmtree(sdir, ignore_errors=True)
            send(chat_id, "🧹 Conversation reset.")
            return
        if cmd == "session":
            send(chat_id, session_list_text(chat_id))
            return
        if cmd == "resume":
            arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if not arg:
                send(chat_id, "Usage: /resume <sessionId> — see /session for the list.")
                return
            sdir = session_dir(chat_id)
            matches = [s for s in list_sessions(sdir) if arg.lower() in s["id"].lower()]
            if not matches:
                send(chat_id, f"No session matches {arg!r}. /session shows the full list.")
                return
            if len(matches) > 1:
                lines = [f"{len(matches)} sessions match {arg!r} — be more specific:", ""]
                lines += [f"  {_session_short_id(m['id'])}" for m in matches]
                send(chat_id, "\n".join(lines))
                return
            set_active_session(sdir, matches[0]["id"])
            send(chat_id, f"\u23ea Resumed session {_session_short_id(matches[0]['id'])}. Next message continues it.")
            return
        if cmd == "model":
            arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if not arg:
                send_model_picker(chat_id)
                return
            if arg.lower() in ("default", "clear", "reset", "none"):
                set_model("")
                with _picker_lock:
                    _picker_sessions.pop(str(chat_id), None)
                send(chat_id, "✅ Model override cleared — omp will use its configured default.")
                return
            matches = search_models(get_models(), arg)
            if len(matches) == 1:
                set_model(matches[0]["selector"])
                with _picker_lock:
                    _picker_sessions.pop(str(chat_id), None)
                send(chat_id, f"✅ Model set to {matches[0]['selector']} ({matches[0]['name']}). Takes effect on the next message.")
                return
            if len(matches) > 1:
                send_model_search_results(chat_id, arg, matches)
                return
            # No hit in the connected catalog — pass through as-is; omp's own
            # --model fuzzy matcher may still resolve it.
            set_model(arg)
            send(chat_id, f"✅ Model set to {MODEL} (not found in the connected catalog — passed through as-is). Verify with /model.")
            return
        if cmd == "config":
            send_config(chat_id)
            return
        if cmd == "login":
            key = str(chat_id)
            with _login_lock:
                if key in _login_flows:
                    send(chat_id, "⚠️ A login attempt is already in progress for this chat. /abort to cancel it.")
                    return
            arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if not arg:
                send_login_picker(chat_id)
                return
            matches = search_login_providers(get_login_providers(), arg)
            if len(matches) == 1:
                start_login(chat_id, matches[0])
                return
            if len(matches) > 1:
                send_login_search_results(chat_id, arg, matches)
                return
            send(chat_id, f"No provider matches {arg!r}. /login shows the full list.")
            return
        if cmd == "status":
            send(chat_id, status_text(chat_id))
            return
        if cmd == "stop":
            parts = cancel_current_work(chat_id)
            set_stopped(chat_id, True)
            parts.append("\U0001f6d1 Bot stopped for this chat. Send /start (or /restart) to resume.")
            send(chat_id, "\n".join(parts))
            return
        if cmd == "abort":
            parts = cancel_current_work(chat_id)
            if not parts:
                parts.append("Nothing is running right now.")
            send(chat_id, "\n".join(parts))
            return
        if cmd == "update":
            update_bridge(chat_id)
            return
        if cmd == "help":
            send(chat_id, "Commands: /start (resume), /restart (cancel current run + resume), /stop (pause until /start), /abort (cancel current run), /reset (new conversation), /model [name] (show/change model), /login [provider] (connect a provider account), /config (toggle typing status / progress updates), /session (list sessions), /resume <id> (switch session), /status (what's running), /update (pull + restart), /help. Anything else is sent to omp.")
            return
        # Unknown slash command -> pass through to omp as normal text.

    note = f" [+{len(attachments)} image]" if attachments else ""
    print(f"[bridge] chat {chat_id}: {text[:80]!r}{note}", flush=True)
    enqueue(chat_id, text, attachments)

# ── Long-poll loop ────────────────────────────────────────────────────────────


def main() -> None:
    configure()
    me = api("getMe")
    if not me.get("ok"):
        sys.exit(f"getMe failed: {me}")
    uname = me["result"].get("username")
    print(f"[bridge] connected as @{uname}", flush=True)
    print(f"[bridge] omp={OMP_BIN} model={MODEL or '(config default)'} allowed={'ALL' if ALLOW_ALL else ALLOWED}", flush=True)
    threading.Thread(target=_cron_loop, daemon=True).start()

    offset = None
    while True:
        params = {"timeout": 50}
        if offset is not None:
            params["offset"] = offset
        resp = api("getUpdates", params, timeout=60)
        if not resp.get("ok"):
            err = resp.get("error") or resp.get("description")
            print(f"[bridge] getUpdates error: {err}", flush=True)
            time.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if msg:
                try:
                    handle_message(msg)
                except Exception as e:  # noqa: BLE001
                    print(f"[bridge] handle error: {e}", flush=True)
                continue
            cq = upd.get("callback_query")
            if cq:
                try:
                    handle_callback_query(cq)
                except Exception as e:  # noqa: BLE001
                    print(f"[bridge] callback error: {e}", flush=True)


# ── Setup wizard ──────────────────────────────────────────────────────────────


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def _autodetect_chat_id(token: str) -> str:
    input("  Send the bot any message on Telegram now, then press Enter here... ")
    resp = tg_call(token, "getUpdates", {"timeout": 0})
    if not resp.get("ok"):
        print(f"  ✗ getUpdates failed: {resp.get('error') or resp.get('description')}")
        return ""
    for upd in reversed(resp.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message")
        if msg:
            chat_id = str(msg["chat"]["id"])
            print(f"  ✓ detected chat id {chat_id}")
            return chat_id
    print("  no message seen yet.")
    return ""


def _install_user_service() -> None:
    source_dir = Path(__file__).resolve().parent
    release_dir = AGENT_HOME / "release"

    ok, err = _validate_source(source_dir)
    if not ok:
        print(f"  ✗ {source_dir} fails to compile, refusing to install a service on top of it:\n{err}")
        return

    if release_dir != source_dir:
        _deploy(source_dir, release_dir)
        exec_target = release_dir / "bridge.py"
        print(f"  ✓ deployed {source_dir} -> {release_dir}")
    else:
        exec_target = source_dir / "bridge.py"

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    unit_path = unit_dir / "omp-bridge.service"
    unit_path.write_text(
        "[Unit]\n"
        "Description=omp <-> Telegram bridge\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        # After this many failures inside the interval, systemd stops retrying
        # and marks the unit failed instead of restarting forever — that's
        # what lets OnFailure= below ever fire.
        "StartLimitIntervalSec=120\n"
        "StartLimitBurst=6\n"
        "OnFailure=omp-bridge-alert.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile={ENV_FILE}\n"
        f"ExecStart={sys.executable} {exec_target}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )

    alert_path = unit_dir / "omp-bridge-alert.service"
    alert_path.write_text(
        "[Unit]\n"
        "Description=omp-bridge crash-loop notifier\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"EnvironmentFile={ENV_FILE}\n"
        f"ExecStart=/usr/bin/env bash {exec_target.parent / 'notify_failure.sh'}\n"
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "omp-bridge.service"], check=False)
    print(f"  ✓ installed + started {unit_path}")
    print("    status: systemctl --user status omp-bridge")
    print("    logs:   journalctl --user -u omp-bridge -f")
    user = os.environ.get("USER", "")
    linger = subprocess.run(["loginctl", "enable-linger", user], capture_output=True, text=True)
    if linger.returncode != 0:
        print(f"  ⚠ couldn't enable linger — the service stops when you log out.")
        print(f"    fix: sudo loginctl enable-linger {user}")


def setup_wizard() -> None:
    print("omp-agent setup — configures the Telegram bridge in one pass.\n")

    token_default = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    while True:
        token = _ask("Telegram bot token (from @BotFather)", token_default)
        if not token:
            print("  a bot token is required.")
            continue
        me = tg_call(token, "getMe")
        if me.get("ok"):
            print(f"  ✓ authenticated as @{me['result'].get('username')}")
            break
        print(f"  ✗ token rejected: {me.get('description') or me.get('error')}")

    print("\nWho may message this bot? omp runs with --auto-approve, so anyone allowed")
    print("here can make it execute shell commands and edit files.")
    allowed_default = os.environ.get("OMP_BRIDGE_ALLOWED", "")
    choice = _ask("Allowed chat ids (comma-separated, '*' for everyone, blank to auto-detect)", allowed_default)
    if not choice:
        detected = _autodetect_chat_id(token)
        choice = detected or _ask("Allowed chat ids (comma-separated, or *)", "")
    allowed = choice

    omp_default = os.environ.get("OMP_BIN", "") or shutil.which("omp") or str(Path.home() / ".local" / "bin" / "omp")
    if not shutil.which(omp_default) and not Path(omp_default).exists():
        print(f"\n  ⚠ omp binary not found at {omp_default!r} — install omp before running the bridge.")
    omp_bin = _ask("Path to the omp binary", omp_default)

    model = _ask("Model override (blank = omp's configured default)", os.environ.get("OMP_BRIDGE_MODEL", ""))
    home = _ask("Data directory (sessions + workspace)", os.environ.get("OMP_BRIDGE_HOME", str(AGENT_HOME / "data")))
    timeout = _ask("Per-message timeout, seconds", os.environ.get("OMP_BRIDGE_TIMEOUT", "3600"))

    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        f"TELEGRAM_BOT_TOKEN={token}\n"
        f"OMP_BRIDGE_ALLOWED={allowed}\n"
        f"OMP_BRIDGE_MODEL={model}\n"
        f"OMP_BRIDGE_HOME={home}\n"
        f"OMP_BRIDGE_TIMEOUT={timeout}\n"
        f"OMP_BIN={omp_bin}\n"
        f"OMP_BRIDGE_REPO_DIR={Path(__file__).resolve().parent}\n"
    )
    ENV_FILE.chmod(0o600)
    print(f"\n✓ wrote {ENV_FILE}")

    if shutil.which("systemctl"):
        if _ask("Install as a systemd --user service so it survives reboot? (y/n)", "y").lower().startswith("y"):
            _install_user_service()
            print("\nSetup complete. Message your bot to try it.")
            return
    else:
        print("\nNo systemd found on this system.")

    print(f"\nSetup complete. Run it with: {sys.executable} {Path(__file__).name}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "--setup"):
        setup_wizard()
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down", flush=True)
