#!/usr/bin/env bash
# omp-agent installer — bootstraps omp itself (if it isn't already on this
# machine), fetches the bridge, and hands off to the interactive setup
# wizard.
#
#   curl -fsSL https://raw.githubusercontent.com/raka-raprast/ompagent/main/install.sh | bash
#
set -euo pipefail

REPO_URL="https://github.com/raka-raprast/ompagent.git"
SRC_DIR="${OMP_AGENT_SRC:-$HOME/.omp-agent/src}"
OMP_INSTALL_URL="https://omp.sh/install"

# ── Output helpers ───────────────────────────────────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; RED=$'\033[0;31m'; GREEN=$'\033[0;32m'
  YELLOW=$'\033[0;33m'; CYAN=$'\033[0;36m'; NC=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
fi

log_info()    { printf '%s\xe2\x86\x92%s %s\n' "$CYAN" "$NC" "$1"; }
log_success() { printf '%s\xe2\x9c\x93%s %s\n' "$GREEN" "$NC" "$1"; }
log_warn()    { printf '%s\xe2\x9a\xa0%s %s\n' "$YELLOW" "$NC" "$1" >&2; }
log_error()   { printf '%s\xe2\x9c\x97%s %s\n' "$RED" "$NC" "$1" >&2; }

print_banner() {
  printf '\n%s%s' "$BOLD" "$CYAN"
  cat <<'EOF'
+-----------------------------------------------------------+
|                  omp-agent installer                      |
|         omp <-> Telegram bridge, one command away         |
+-----------------------------------------------------------+
EOF
  printf '%s\n' "$NC"
}

print_banner

# ── 1. omp itself ─────────────────────────────────────────────────────────────
# The bridge is a thin frontend over the omp binary; without it there's
# nothing to run a conversation through, so it comes first.

if command -v omp >/dev/null 2>&1; then
  log_success "omp found: $(command -v omp)"
elif [ -x "$HOME/.local/bin/omp" ]; then
  log_success "omp found: $HOME/.local/bin/omp"
  export PATH="$HOME/.local/bin:$PATH"
else
  log_info "omp not found — installing it (curl -fsSL $OMP_INSTALL_URL | sh)"
  if ! curl -fsSL "$OMP_INSTALL_URL" | sh; then
    log_error "omp install failed. Install it manually from https://omp.sh, then re-run this script."
    exit 1
  fi
  export PATH="$HOME/.local/bin:$PATH"
  if command -v omp >/dev/null 2>&1; then
    log_success "omp installed: $(command -v omp)"
  else
    log_warn "omp installed but not on PATH in this shell — the setup wizard will let you point at a custom path."
  fi
fi

# ── 2. Prerequisites ──────────────────────────────────────────────────────────

command -v python3 >/dev/null 2>&1 || { log_error "python3 is required."; exit 1; }
log_success "python3 found: $(command -v python3)"

# ── 3. omp-agent source ───────────────────────────────────────────────────────

script_dir=""
if [ -n "${BASH_SOURCE[0]:-}" ]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$script_dir" ] && [ -f "$script_dir/bridge.py" ]; then
  # Running from a local checkout — use it as-is.
  SRC_DIR="$script_dir"
  log_success "using local checkout: $SRC_DIR"
elif [ -d "$SRC_DIR/.git" ]; then
  log_info "updating existing checkout at $SRC_DIR"
  git -C "$SRC_DIR" pull --ff-only
  log_success "updated: $SRC_DIR"
else
  command -v git >/dev/null 2>&1 || { log_error "git is required to fetch omp-agent."; exit 1; }
  log_info "cloning omp-agent into $SRC_DIR"
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone --depth 1 "$REPO_URL" "$SRC_DIR"
  log_success "cloned: $SRC_DIR"
fi

echo ""
log_info "handing off to the setup wizard..."
echo ""
exec python3 "$SRC_DIR/bridge.py" setup
