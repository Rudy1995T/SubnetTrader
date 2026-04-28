#!/usr/bin/env bash
# ─────────────────────────────────────────────
# SubnetTrader — one-command installer
# Usage: ./install.sh [--yes|-y]
# ─────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Non-interactive mode ─────────────────────
AUTO_YES=false
if [[ "$1" == "--yes" ]] || [[ "$1" == "-y" ]]; then
    AUTO_YES=true
fi

# ── Step 1: Detect OS / arch ────────────────
OS="$(uname -s)"    # Linux, Darwin
ARCH="$(uname -m)"  # aarch64, arm64, x86_64

case "${OS}-${ARCH}" in
    Linux-aarch64)  LABEL="Pi (arm64)" ;;
    Linux-x86_64)   LABEL="Linux (x86)" ;;
    Darwin-arm64)   LABEL="macOS (Apple)" ;;
    Darwin-x86_64)  LABEL="macOS (Intel)" ;;
    *)              LABEL="${OS} ${ARCH} (unrecognized)"
                    echo "WARNING: Unsupported OS/arch ${OS}/${ARCH} — continuing anyway"
                    ;;
esac

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SubnetTrader Installer"
echo "  Platform: ${LABEL}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Step 2: Check system prerequisites ──────
echo "[1/7] Checking system prerequisites..."

check_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "ERROR: $1 is required but not found."
        if [ "$OS" = "Linux" ]; then
            echo "  Install with: sudo apt-get install $2"
        elif [ "$OS" = "Darwin" ]; then
            echo "  Install with: brew install $2"
        fi
        exit 1
    }
}

check_cmd git git
check_cmd curl curl

# Check Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required but not found."
    if [ "$OS" = "Linux" ]; then
        echo "  Install with: sudo apt-get install python3 python3-venv python3-pip"
    elif [ "$OS" = "Darwin" ]; then
        echo "  Install with: brew install python@3.13  (or download from python.org)"
    fi
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "ERROR: Python 3.11+ required (found $PY_VERSION)"
    exit 1
fi

# Check pip
if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
    echo "ERROR: pip is required but not found."
    if [ "$OS" = "Linux" ]; then
        echo "  Install with: sudo apt-get install python3-pip"
    elif [ "$OS" = "Darwin" ]; then
        echo "  pip should come with Python from Homebrew"
    fi
    exit 1
fi

echo "  Python $PY_VERSION, git, curl — OK"

# ── Step 3: Create Python venv + install deps ──
if [ ! -d ".venv" ]; then
    echo "[2/7] Creating Python virtual environment..."
    python3 -m venv .venv
else
    echo "[2/7] Python venv already exists — skipping creation"
fi

source .venv/bin/activate
pip install --upgrade pip -q

echo "  Installing Python dependencies..."
if ! pip install -r requirements.txt; then
    echo ""
    echo "NOTE: Some packages require Rust. Install it with:"
    echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    echo "Then re-run ./install.sh"
    exit 1
fi
echo "  Python dependencies installed"

# ── Step 4: Install Node.js (if missing) + npm install ──
install_node() {
    if [ "$OS" = "Linux" ]; then
        echo "  Installing Node.js 20.x via NodeSource..."
        if ! command -v sudo >/dev/null 2>&1; then
            echo "ERROR: sudo is required to install Node.js. Install Node.js 20+ manually:"
            echo "  https://nodejs.org/en/download/"
            exit 1
        fi
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt-get install -y nodejs
    elif [ "$OS" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            brew install node@20
        else
            echo "ERROR: Install Node.js 20+ from https://nodejs.org or via Homebrew"
            exit 1
        fi
    fi
}

if ! command -v node >/dev/null 2>&1; then
    echo "[3/7] Node.js not found — installing..."
    install_node
else
    NODE_VERSION=$(node -v | sed 's/v//' | cut -d. -f1)
    if [ "$NODE_VERSION" -lt 18 ]; then
        echo "[3/7] Node.js $NODE_VERSION found but 18+ required — installing..."
        install_node
    else
        echo "[3/7] Node.js $(node -v) found"
    fi
fi

echo "[4/7] Installing frontend dependencies..."
cd frontend
npm install
cd "$SCRIPT_DIR"
echo "  Frontend dependencies installed"

# ── Step 5: Copy .env.example → .env ────────
if [ ! -f ".env" ]; then
    echo "[5/7] Creating .env from template..."
    cp .env.example .env
    echo "  Edit .env or use the setup wizard at http://localhost:3000/setup"
else
    echo "[5/7] .env already exists — keeping existing configuration"
fi

# ── Step 6: Create data directory ────────────
echo "[6/7] Setting up data directory..."
mkdir -p data/logs data/exports

# ── Step 7: Optionally install watchdog cron ─
echo ""
if [ "$AUTO_YES" = true ]; then
    echo "[7/7] Skipped watchdog cron (non-interactive mode)"
else
    read -p "Install watchdog cron job (restarts bot if it crashes)? [y/N] " INSTALL_CRON
    if [[ "$INSTALL_CRON" =~ ^[Yy]$ ]]; then
        CRON_LINE="*/5 * * * * $(pwd)/watchdog.sh >> $(pwd)/data/watchdog.log 2>&1"
        # Check if already installed
        if crontab -l 2>/dev/null | grep -qF "watchdog.sh"; then
            echo "  Watchdog cron already installed — skipping"
        else
            (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
            echo "  Watchdog cron installed (every 5 minutes)"
        fi
    else
        echo "[7/7] Skipped watchdog cron (you can install it later — see watchdog.sh header)"
    fi
fi

# ── Done ─────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SubnetTrader installed successfully!"
echo ""
echo "  To start:     ./start.sh"
echo "  To configure:  open http://localhost:3000/setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
