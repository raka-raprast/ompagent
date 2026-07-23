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
  OMP_BRIDGE_TIMEOUT     Per-message omp timeout in seconds (default: 600).
  OMP_BRIDGE_HEARTBEAT_FIRST     Seconds of silence before the first "still
                         working" message (default: 180). 0 disables
                         heartbeats entirely. Typing indicators alone
                         fade/re-arm every few seconds and can look stalled
                         on long, tool-heavy turns.
  OMP_BRIDGE_HEARTBEAT_INTERVAL  Seconds between subsequent heartbeats after
                         the first (default: 120). 0 sends only the first
                         and no more.
  OMP_BRIDGE_HEARTBEAT_TEXT  Heartbeat message template; "{elapsed}" is
                         replaced with seconds waited so far.
  OMP_BIN                Path to the omp binary (default: resolve from PATH).
  OMP_BRIDGE_CRON_FILE   Scheduled-job definitions (default: ~/.omp-agent/cron.json).
                         Absent file = no cron jobs. See the "Cron scheduler"
                         section below for the job format.
"""

import json
import os
import queue
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
TIMEOUT = 600
HEARTBEAT_FIRST = 180     # seconds before the first "still working" nudge; 0 disables entirely
HEARTBEAT_INTERVAL = 120  # seconds between subsequent nudges; 0 = first one only
HEARTBEAT_TEXT = "\u23f3 Still working on it ({elapsed}s so far)... I'll reply as soon as it's done."
OMP_BIN = ""
CRON_FILE: Path = AGENT_HOME / "cron.json"
CRON_STATE_FILE: Path = HOME / "cron_state.json"
CRON_JOBS: list = []

TG_LIMIT = 4096  # Telegram max message length
_STARTED_AT = time.monotonic()  # process start, for /status uptime


def configure() -> None:
    """Load run-mode config from the environment. Called once, before main()."""
    global TOKEN, ALLOW_ALL, ALLOWED, MODEL, HOME, SESSIONS, WORKSPACE, TIMEOUT, OMP_BIN
    global HEARTBEAT_FIRST, HEARTBEAT_INTERVAL, HEARTBEAT_TEXT
    global CRON_FILE, CRON_STATE_FILE, CRON_JOBS

    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not TOKEN:
        sys.exit(f"No TELEGRAM_BOT_TOKEN configured. Run: python3 {Path(__file__).name} setup")

    allowed_raw = os.environ.get("OMP_BRIDGE_ALLOWED", "").strip()
    ALLOW_ALL = allowed_raw == "*"
    ALLOWED = set() if ALLOW_ALL else {c.strip() for c in allowed_raw.split(",") if c.strip()}
    if not ALLOW_ALL and not ALLOWED:
        sys.exit(f"No OMP_BRIDGE_ALLOWED configured. Run: python3 {Path(__file__).name} setup")

    MODEL = os.environ.get("OMP_BRIDGE_MODEL", "").strip()
    HOME = Path(os.environ.get("OMP_BRIDGE_HOME", str(AGENT_HOME / "data")))
    SESSIONS = HOME / "sessions"
    WORKSPACE = HOME / "workspace"
    TIMEOUT = int(os.environ.get("OMP_BRIDGE_TIMEOUT", "600"))
    HEARTBEAT_FIRST = int(os.environ.get("OMP_BRIDGE_HEARTBEAT_FIRST", "180"))
    HEARTBEAT_INTERVAL = int(os.environ.get("OMP_BRIDGE_HEARTBEAT_INTERVAL", "120"))
    HEARTBEAT_TEXT = os.environ.get("OMP_BRIDGE_HEARTBEAT_TEXT", "").strip() or HEARTBEAT_TEXT
    OMP_BIN = os.environ.get("OMP_BIN", "") or shutil.which("omp") or str(Path.home() / ".local" / "bin" / "omp")
    CRON_FILE = Path(os.environ.get("OMP_BRIDGE_CRON_FILE", str(AGENT_HOME / "cron.json")))

    SESSIONS.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
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


def _kill_process_group(proc: subprocess.Popen) -> None:
    # Callers launch with start_new_session=True, putting the child in its own process
    # group; killing just the top pid would strand any shell children (auto-approve runs
    # shell commands) still holding the stdout pipe open, so communicate() would hang past
    # the kill.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_omp(chat_id, message: str) -> str:
    sdir = session_dir(chat_id)
    cmd = [OMP_BIN, "-p", "--session-dir", str(sdir), "--auto-approve", "--cwd", str(WORKSPACE)]
    if has_session(sdir):
        cmd.append("--continue")
    if MODEL:
        cmd += ["--model", MODEL]
    cmd.append(message)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(WORKSPACE), start_new_session=True
        )
    except OSError as e:
        return f"⚠️ failed to start omp: {e}"

    key = str(chat_id)
    start = time.monotonic()
    entry = {"proc": proc, "started": start, "stopped": False, "message": message[:80]}
    with _active_lock:
        _active_procs[key] = entry

    deadline = start + TIMEOUT
    next_heartbeat = start + HEARTBEAT_FIRST if HEARTBEAT_FIRST > 0 else None
    try:
        while True:
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
                        send(chat_id, HEARTBEAT_TEXT.format(elapsed=int(now - start)))
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

    if entry["stopped"]:
        return "🛑 Stopped."

    out = stdout.decode(errors="replace").strip()
    if proc.returncode != 0 and not out:
        return f"⚠️ omp exited {proc.returncode} with no output."
    return out or "(omp produced no output)"


# ── Active-run tracking (for /status, /stop) ─────────────────────────────────

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
    lines.append(f"Model: {MODEL or '(omp config default)'}")

    info = active_run_info(chat_id)
    if info:
        elapsed = int(time.monotonic() - info["started"])
        lines.append(f"This chat: running for {elapsed}s \u2014 \u201c{info['message']}\u201d")
    else:
        lines.append("This chat: idle")

    sdir = session_dir(chat_id)
    lines.append(
        "Session: in progress" if has_session(sdir) else "Session: none yet (next message starts fresh)"
    )

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

    if data == "mx:noop":
        answer_callback(cq_id)
        return

    key = str(chat_id)
    with _picker_lock:
        session = _picker_sessions.get(key)
    if session is None:
        answer_callback(cq_id, "Picker expired \u2014 use /model again.")
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

    answer_callback(cq_id)


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
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
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
        message = q.get()
        try:
            reply = run_omp(chat_id, message)
            send(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            send(chat_id, f"⚠️ bridge error: {e}")
        finally:
            q.task_done()


def enqueue(chat_id, message: str) -> None:
    with _workers_lock:
        q = _workers.get(chat_id)
        if q is None:
            q = queue.Queue()
            _workers[chat_id] = q
            threading.Thread(target=_worker_loop, args=(chat_id, q), daemon=True).start()
    q.put(message)


# ── Message handling ─────────────────────────────────────────────────────────


def authorized(chat_id) -> bool:
    return ALLOW_ALL or str(chat_id) in ALLOWED


def handle_message(msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return

    if not authorized(chat_id):
        send(chat_id, f"⛔ Not authorized. Your chat id is {chat_id}.")
        print(f"[bridge] denied chat {chat_id}", flush=True)
        return

    if text.startswith("/"):
        cmd = text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd == "start":
            send(chat_id, "🤖 omp bridge online. Send a message and I'll run it through omp. /reset clears our conversation, /model shows or changes the model, /status shows what's running, /stop cancels it.")
            return
        if cmd == "reset":
            sdir = session_dir(chat_id)
            shutil.rmtree(sdir, ignore_errors=True)
            send(chat_id, "🧹 Conversation reset.")
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
        if cmd == "status":
            send(chat_id, status_text(chat_id))
            return
        if cmd == "stop":
            stopped, dropped = stop_run(chat_id)
            if stopped and dropped:
                send(chat_id, f"🛑 Stopping the current run and dropped {dropped} queued message(s).")
            elif stopped:
                send(chat_id, "🛑 Stopping the current run...")
            elif dropped:
                send(chat_id, f"🛑 Dropped {dropped} queued message(s) (nothing was actively running).")
            else:
                send(chat_id, "Nothing is running right now.")
            return
        if cmd == "help":
            send(chat_id, "Commands: /start, /reset (new conversation), /model [name] (show/change model), /status (what's running), /stop (cancel the current run), /help. Anything else is sent to omp.")
            return
        # Unknown slash command -> pass through to omp as normal text.

    print(f"[bridge] chat {chat_id}: {text[:80]!r}", flush=True)
    enqueue(chat_id, text)


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
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "omp-bridge.service"
    unit_path.write_text(
        "[Unit]\n"
        "Description=omp <-> Telegram bridge\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile={ENV_FILE}\n"
        f"ExecStart={sys.executable} {Path(__file__).resolve()}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
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
    timeout = _ask("Per-message timeout, seconds", os.environ.get("OMP_BRIDGE_TIMEOUT", "600"))

    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        f"TELEGRAM_BOT_TOKEN={token}\n"
        f"OMP_BRIDGE_ALLOWED={allowed}\n"
        f"OMP_BRIDGE_MODEL={model}\n"
        f"OMP_BRIDGE_HOME={home}\n"
        f"OMP_BRIDGE_TIMEOUT={timeout}\n"
        f"OMP_BIN={omp_bin}\n"
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

    print(f"\nSetup complete. Run it with: python3 {Path(__file__).name}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "--setup"):
        setup_wizard()
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down", flush=True)
