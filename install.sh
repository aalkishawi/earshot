#!/usr/bin/env bash
# Earshot — one-shot installer for macOS/Linux.
#
# Usage (from any directory):
#   curl -fsSL https://raw.githubusercontent.com/aalkishawi/earshot/main/install.sh | bash
#
# Or save this file locally and run:
#   bash install.sh
#
# Creates a .venv in the current directory, installs Earshot into it from
# the GitHub main branch, and prints next-step commands.

set -euo pipefail

REPO_URL="git+https://github.com/aalkishawi/earshot.git"
VENV_DIR=".venv"

green()  { printf "\033[32m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
cyan()   { printf "\033[36m%s\033[0m\n" "$1"; }

fail() {
    echo
    red "  install failed: $1"
    exit 1
}

echo
cyan "  Earshot installer"
cyan "  -----------------"
echo

# 1. Python version check.
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 isn't on PATH. Install Python 3.11+ from https://python.org/downloads or your package manager."
fi
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=${PY_VERSION%%.*}
PY_MINOR=${PY_VERSION##*.}
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fail "Python $PY_VERSION found, but Earshot needs 3.11+."
fi
green "  [OK]  Python $PY_VERSION"

# 2. Venv guard.
if [ -e "$VENV_DIR" ]; then
    fail "$VENV_DIR already exists in $(pwd). Move or delete it, then re-run."
fi

# 3. Create venv.
echo "  [..] Creating venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    fail "venv creation failed."
fi
green "  [OK]  venv created"

# 4. Install Earshot from GitHub.
echo "  [..] Installing Earshot from $REPO_URL (takes ~30s)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$REPO_URL"
if [ ! -x "$VENV_DIR/bin/earshot" ]; then
    fail "earshot binary not found after install"
fi
INSTALLED_VERSION=$("$VENV_DIR/bin/earshot" --version)
green "  [OK]  $INSTALLED_VERSION"

# 5. Next steps.
echo
cyan "  Installed. Next:"
echo "    1. Activate the venv:"
echo "         source $VENV_DIR/bin/activate"
echo "    2. Walk the wizard (have your Anthropic API key + Yahoo app password handy):"
echo "         earshot init"
echo "    3. Verify everything is green:"
echo "         earshot doctor"
echo "    4. Try it (first run baselines existing content — no digest yet):"
echo "         earshot run"
echo
