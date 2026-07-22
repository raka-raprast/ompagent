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
   enable-linger` so it keeps running after you log out).

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
- `/help` — lists commands. Anything else is sent straight to omp.

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
| `OMP_BIN`              | no       | resolved from `PATH`      | Path to the `omp` binary. |

## Security

The bridge runs `omp --auto-approve`, so anyone in `OMP_BRIDGE_ALLOWED` can
make it execute shell commands and edit files in `OMP_BRIDGE_HOME/workspace`.
Never set `OMP_BRIDGE_ALLOWED=*` on a bot whose token isn't private.
