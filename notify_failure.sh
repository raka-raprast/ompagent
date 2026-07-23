#!/usr/bin/env bash
# Fired by systemd's OnFailure= when omp-bridge exhausts its restart budget
# (StartLimitBurst in omp-bridge.service) and gives up. At that point the
# bridge process itself is down, so it can't send its own "I'm dead" message
# — this is a standalone script, invoked with the same EnvironmentFile, that
# talks to the Telegram API directly.
set -euo pipefail

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not set}"

host="$(hostname)"
text="omp-bridge on ${host} crash-looped and systemd gave up restarting it.
Check: journalctl --user -u omp-bridge -n 50"

IFS=',' read -ra chats <<< "${OMP_BRIDGE_ALLOWED:-}"
for chat in "${chats[@]}"; do
  chat="$(echo "$chat" | xargs)"
  [ -z "$chat" ] || [ "$chat" = "*" ] && continue
  curl -fsS -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${text}" \
    >/dev/null || echo "notify_failure.sh: failed to notify chat ${chat}" >&2
done
