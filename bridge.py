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
  OMP_BRIDGE_HOME        Base dir for sessions + workspace.
                         Default: ~/.omp-agent/data
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
OMP_BIN = ""

TG_LIMIT = 4096  # Telegram max message length


def configure() -> None:
    """Load run-mode config from the environment. Called once, before main()."""
    global TOKEN, ALLOW_ALL, ALLOWED, MODEL, HOME, SESSIONS, WORKSPACE, TIMEOUT, OMP_BIN

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
    OMP_BIN = os.environ.get("OMP_BIN", "") or shutil.which("omp") or str(Path.home() / ".local" / "bin" / "omp")

    SESSIONS.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)


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
    configure()
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
