#!/usr/bin/env python3
"""omp <-> Telegram bridge.

A zero-dependency (stdlib-only) bridge that turns the `omp` coding agent into a
Telegram bot. Long-polls Telegram getUpdates, and for each authorized chat runs
`omp -p` in a per-chat session directory so conversations stay coherent.

omp has no messaging gateway of its own; this is the thin frontend that Hermes
would otherwise provide.

Environment overrides:
  TELEGRAM_BOT_TOKEN     Bot token. If unset, read from ~/.hermes/.env.
  OMP_BRIDGE_ALLOWED     Comma-separated chat ids allowed to use the bot.
                         Default: 834503008 (the owner). "*" allows everyone
                         (DANGEROUS: --auto-approve lets omp run shell/edits).
  OMP_BRIDGE_MODEL       Model override passed to omp (default: omp's config).
  OMP_BRIDGE_HOME        Base dir for sessions + workspace.
                         Default: ~/omp-telegram-bridge
  OMP_BRIDGE_TIMEOUT     Per-message omp timeout in seconds (default: 600).
  OMP_BIN                Path to the omp binary (default: resolve from PATH).
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────


def _load_token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    sys.exit("No TELEGRAM_BOT_TOKEN in env or ~/.hermes/.env")


TOKEN = _load_token()
API = f"https://api.telegram.org/bot{TOKEN}"

_allowed_raw = os.environ.get("OMP_BRIDGE_ALLOWED", "834503008").strip()
ALLOW_ALL = _allowed_raw == "*"
ALLOWED = set() if ALLOW_ALL else {c.strip() for c in _allowed_raw.split(",") if c.strip()}

MODEL = os.environ.get("OMP_BRIDGE_MODEL", "").strip()
HOME = Path(os.environ.get("OMP_BRIDGE_HOME", str(Path.home() / "omp-telegram-bridge")))
SESSIONS = HOME / "sessions"
WORKSPACE = HOME / "workspace"
TIMEOUT = int(os.environ.get("OMP_BRIDGE_TIMEOUT", "600"))
OMP_BIN = os.environ.get("OMP_BIN", "") or shutil.which("omp") or str(Path.home() / ".local" / "bin" / "omp")

SESSIONS.mkdir(parents=True, exist_ok=True)
WORKSPACE.mkdir(parents=True, exist_ok=True)

TG_LIMIT = 4096  # Telegram max message length

# ── Telegram API helpers ────────────────────────────────────────────────────


def api(method: str, params: dict | None = None, timeout: int = 60) -> dict:
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
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


def send(chat_id, text: str) -> None:
    text = text.strip() or "(empty response)"
    for i in range(0, len(text), TG_LIMIT):
        api("sendMessage", {"chat_id": chat_id, "text": text[i : i + TG_LIMIT]})


def typing(chat_id) -> None:
    api("sendChatAction", {"chat_id": chat_id, "action": "typing"})


# ── omp runner ──────────────────────────────────────────────────────────────


def session_dir(chat_id) -> Path:
    d = SESSIONS / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def has_session(sdir: Path) -> bool:
    return any(sdir.glob("*.jsonl"))


def run_omp(chat_id, message: str) -> str:
    sdir = session_dir(chat_id)
    cmd = [OMP_BIN, "-p", "--session-dir", str(sdir), "--auto-approve", "--cwd", str(WORKSPACE)]
    if has_session(sdir):
        cmd.append("--continue")
    if MODEL:
        cmd += ["--model", MODEL]
    cmd.append(message)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT,
            cwd=str(WORKSPACE),
        )
    except subprocess.TimeoutExpired:
        return f"⏱️ omp timed out after {TIMEOUT}s."
    out = proc.stdout.decode(errors="replace").strip()
    if proc.returncode != 0 and not out:
        return f"⚠️ omp exited {proc.returncode} with no output."
    return out or "(omp produced no output)"


# ── Per-chat workers ─────────────────────────────────────────────────────────

_workers: dict = {}
_workers_lock = threading.Lock()


def _worker_loop(chat_id, q: "queue.Queue") -> None:
    while True:
        message = q.get()
        try:
            typing(chat_id)
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
            send(chat_id, "🤖 omp bridge online. Send a message and I'll run it through omp. /reset clears our conversation.")
            return
        if cmd == "reset":
            sdir = session_dir(chat_id)
            shutil.rmtree(sdir, ignore_errors=True)
            send(chat_id, "🧹 Conversation reset.")
            return
        if cmd == "help":
            send(chat_id, "Commands: /start, /reset (new conversation), /help. Anything else is sent to omp.")
            return
        # Unknown slash command -> pass through to omp as normal text.

    print(f"[bridge] chat {chat_id}: {text[:80]!r}", flush=True)
    enqueue(chat_id, text)


# ── Long-poll loop ────────────────────────────────────────────────────────────


def main() -> None:
    me = api("getMe")
    if not me.get("ok"):
        sys.exit(f"getMe failed: {me}")
    uname = me["result"].get("username")
    print(f"[bridge] connected as @{uname}", flush=True)
    print(f"[bridge] omp={OMP_BIN} model={MODEL or '(config default)'} allowed={'ALL' if ALLOW_ALL else ALLOWED}", flush=True)

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down", flush=True)
