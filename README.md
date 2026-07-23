# omp-agent

Turns the `omp` coding agent into a Telegram bot.
Long-polls Telegram, and for each authorized chat runs `omp -p` in its own
per-chat session directory so conversations stay coherent across messages.

omp has no messaging gateway of its own — this is the thin frontend a
framework like Hermes would otherwise provide, minus the ceremony: one Python
file, stdlib only, one setup command.

## Quickstart

```sh
curl -fsSL https://raw.githubusercontent.com/raka-raprast/ompagent/main/install.sh | bash
```

This clones the repo to `~/.omp-agent/src` (or reuses a local checkout) and
runs the setup wizard, which:

1. Asks for your bot token (from [@BotFather](https://t.me/BotFather)) and
   verifies it against the Telegram API.
2. Lets you allow specific chat ids, `*` for everyone, or auto-detects your
   chat id from the next message you send the bot.
3. Locates (or lets you point at) the `omp` binary.
4. Writes config to `~/.omp-agent/.env`.
5. On Linux, offers to install a `systemd --user` service so the bridge
   survives reboots — no `sudo` required (it also tries `loginctl
   enable-linger` so it keeps running after you log out). Installing the
   service deploys a validated *copy* of the checkout to
   `~/.omp-agent/release` and points `ExecStart` there, never at the
   checkout itself — that way hand-editing `bridge.py` in your working
   copy can never break the running bot; only `/update` (or re-running
   `setup`) redeploys it, and only after a byte-compile check passes.

Already have a checkout? Run the wizard directly:

```sh
python3 bridge.py setup
```

Re-running `setup` shows your current values as defaults, so it doubles as
an editor for existing config.

## Running manually

```sh
python3 bridge.py
```

Reads config from the environment first, then from `~/.omp-agent/.env`.

## Managing the service

```sh
systemctl --user status omp-bridge
journalctl --user -u omp-bridge -f
systemctl --user restart omp-bridge
```

## Bot commands

- `/start` — confirms the bridge is online.
- `/reset` — clears the conversation, starting a fresh omp session.
- `/model` — opens an inline-keyboard picker: pick a provider, then a model,
  drilling down and paginating in place. Only models the configured
  providers can actually serve show up (a "connected" catalog straight from
  `omp models --json`), grouped by provider with a `🔄 Refresh` button.
- `/model <name>` — searches the connected catalog by substring. An exact
  single match switches immediately; multiple matches render as a picker.
  No match falls back to passing `<name>` straight to omp's own `--model`
  fuzzy matcher. Either way the choice is saved to `~/.omp-agent/.env` so it
  survives a bridge restart.
- `/model default` — clears the override, back to omp's configured default.
- `/login` — opens an inline-keyboard picker over omp's full login-provider
  list (OAuth subscriptions and API-key providers alike — the same list
  omp's own interactive `/login` shows, ~50 entries, paginated 10 at a
  time). Driven over `omp --mode rpc`, since that's the only omp surface
  built to answer these prompts programmatically instead of a real TTY.
- `/login <name>` — searches that list by substring. An exact single match
  starts the flow immediately; multiple matches render as a picker.
- Once a provider's picked, the bridge relays whatever the flow needs back
  and forth: a URL to open (just informational — open it in any browser,
  the flow keeps polling on the bot's side), a value to paste (reply with
  plain text), or a yes/no (inline buttons). `/stop` cancels an in-flight
  attempt at any point. `/status` shows one if it's running.
  A few providers' flows aren't wired for RPC mode at all (e.g. GitHub
  Copilot as of omp v17) and reply with "requires interactive prompts which
  are not supported in RPC mode" — for those, SSH in and run `omp` (the real
  TUI) directly.
- `/status` — version + commit, current model, whether this chat has a run
  in progress (and for how long), any messages queued behind it, session
  state, bridge uptime, cron job count, and access mode.
- `/stop` — kills the in-flight omp process for this chat and drops any
  messages still queued behind it. No-op (reports so) if nothing is running.
- `/update` — `git pull --ff-only` in `OMP_BRIDGE_REPO_DIR` (the checkout,
  default: the directory `bridge.py` lives in); if that actually moved
  `HEAD`, byte-compiles the pulled code and — only if that passes — deploys
  it to `~/.omp-agent/release` (the directory the service actually runs
  from; see [Installation](#installation)) and restarts the service (via a
  detached `systemd-run` timer, so the restart doesn't cut off its own
  confirmation message) a few seconds later. Shows the version/commit
  change (e.g. `0.7.0 (b0d593e) → 0.8.0 (a1b2c3d)`, or just the commit range
  if `VERSION` didn't change). A commit that fails to compile is rolled
  back (`git reset --hard`) instead of ever reaching the running service.
  Reports "already up to date" if there was nothing to pull, and surfaces
  the raw git error (e.g. local changes in the way) without restarting if
  the pull fails. No systemd on this host? It still pulls and validates,
  but tells you to restart manually.

  If systemd's own restart budget is exhausted (`StartLimitBurst` in
  `omp-bridge.service` — 6 failures within 120s by default), it gives up
  instead of looping forever, which fires `omp-bridge-alert.service` and
  messages every allowed chat directly via the Telegram API that the bot is
  down and needs a look (`journalctl --user -u omp-bridge`).

  Versioning is a plain `VERSION` file at the repo root (e.g. `0.8.0`) —
  bump it by hand in whatever commit warrants a new version; `/status` and
  `/update` just read it back.
- `/help` — lists commands. Anything else is sent straight to omp.

## Cron jobs

Optional scheduled jobs — bare scripts or one-shot `omp -p` prompts — live in
`~/.omp-agent/cron.json` (override with `OMP_BRIDGE_CRON_FILE`). No file, no
jobs; this is a pure addition, off by default. Last-fired timestamps persist
to `<OMP_BRIDGE_HOME>/cron_state.json` so a restart never double-fires a job
within the same minute.

```json
{
  "jobs": [
    {
      "id": "github-trending",
      "name": "GitHub Trending Daily",
      "schedule": "0 9 * * *",
      "chat_id": "-1004307841424",
      "thread_id": "6",
      "kind": "script",
      "argv": ["python3", "/path/to/github_trending.py"],
      "timeout": 60
    },
    {
      "id": "news-digest",
      "name": "Daily Trending News Digest",
      "schedule": "0 7 * * *",
      "chat_id": "-1004307841424",
      "thread_id": "104",
      "kind": "prompt",
      "prompt": "You are a news curator ...",
      "tools": "web_search",
      "timeout": 300
    }
  ]
}
```

Fields: `schedule` is a standard 5-field cron expression evaluated in the
machine's local time zone. `chat_id`/`thread_id` pick the Telegram
destination (`thread_id` targets a forum topic in a supergroup). `kind` is
`script` (runs `argv`, delivers stdout verbatim) or `prompt` (runs
`omp -p <prompt>`, optionally scoped with `tools` and overridden with
`model`; falls back to `OMP_BRIDGE_MODEL`). A job whose stdout is empty
delivers nothing — that's "nothing to report," not an error. Set
`"enabled": false` to keep a job in the file without scheduling it.

## Configuration reference

All variables live in `~/.omp-agent/.env` (override the directory with
`OMP_AGENT_HOME`) and can also be set as real environment variables, which
take precedence.

| Variable              | Required | Default                  | Meaning |
|------------------------|----------|---------------------------|---------|
| `TELEGRAM_BOT_TOKEN`   | yes      | —                         | Bot token from @BotFather. |
| `OMP_BRIDGE_ALLOWED`   | yes      | —                         | Comma-separated chat ids, or `*` for everyone. |
| `OMP_BRIDGE_MODEL`     | no       | omp's configured default | Model override passed to `omp --model`. |
| `OMP_BRIDGE_HOME`      | no       | `~/.omp-agent/data`       | Base dir for `sessions/` and `workspace/`. |
| `OMP_BRIDGE_TIMEOUT`   | no       | `600`                     | Per-message `omp` timeout, in seconds. |
| `OMP_BRIDGE_HEARTBEAT_FIRST` | no | `180`                    | Seconds of silence before the *first* "still working" message; `0` disables heartbeats entirely. |
| `OMP_BRIDGE_HEARTBEAT_INTERVAL` | no | `120`                 | Seconds between *subsequent* heartbeats; `0` sends only the first. |
| `OMP_BRIDGE_HEARTBEAT_TEXT` | no  | see below                 | Heartbeat message template; `{elapsed}` is replaced with seconds waited. |
| `OMP_BIN`              | no       | resolved from `PATH`      | Path to the `omp` binary. |
| `OMP_BRIDGE_CRON_FILE` | no       | `~/.omp-agent/cron.json`  | Scheduled-job definitions; see [Cron jobs](#cron-jobs). |
| `OMP_BRIDGE_REPO_DIR`  | no       | dir containing `bridge.py` | Git checkout `/update` pulls in; set automatically by `setup` to the checkout it ran from. Distinct from `~/.omp-agent/release`, the dir the service actually runs from. |

## Security

The bridge runs `omp --auto-approve`, so anyone in `OMP_BRIDGE_ALLOWED` can
make it execute shell commands and edit files in `OMP_BRIDGE_HOME/workspace`.
Never set `OMP_BRIDGE_ALLOWED=*` on a bot whose token isn't private.

`cron.json` is trusted config, not chat input — its `argv`/`prompt` run
unattended on schedule (the `prompt` kind also via `--auto-approve`).
Anyone who can write that file can run arbitrary commands as the bridge user.
