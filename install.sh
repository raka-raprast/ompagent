#!/usr/bin/env bash
# omp-agent installer — fetches the bridge (if needed) and hands off to the
# interactive setup wizard.
#
#   curl -fsSL https://raw.githubusercontent.com/raka-raprast/ompagent/main/install.sh | bash
#
set -euo pipefail

REPO_URL="https://github.com/raka-raprast/ompagent.git"
SRC_DIR="${OMP_AGENT_SRC:-$HOME/.omp-agent/src}"

command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required." >&2; exit 1; }
if ! command -v omp >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/omp" ]; then
  echo "warning: 'omp' binary not found on PATH or in ~/.local/bin. Install it first" >&2
  echo "         (the wizard will let you point at a custom path)." >&2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$script_dir/bridge.py" ]; then
  # Running from a local checkout — use it as-is.
  SRC_DIR="$script_dir"
elif [ -d "$SRC_DIR/.git" ]; then
  echo "Updating existing checkout at $SRC_DIR..."
  git -C "$SRC_DIR" pull --ff-only
else
  command -v git >/dev/null 2>&1 || { echo "error: git is required to fetch omp-agent." >&2; exit 1; }
  echo "Cloning omp-agent into $SRC_DIR..."
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone --depth 1 "$REPO_URL" "$SRC_DIR"
fi

echo "omp-agent source: $SRC_DIR"
exec python3 "$SRC_DIR/bridge.py" setup
