# SubnetTrader Packaging & Install — Specification

## Overview

Make SubnetTrader installable on any machine with a single command. Provide three paths:
bare-metal install script, Docker, and manual. Remove the last hardcoded path references
so the repo works from any directory.

**Goal:** a new user clones the repo, runs `./install.sh` (or `docker compose up`), and
lands on `http://localhost:3000/setup` to configure the bot via the onboarding wizard
(see `spec/onboarding.md`).

---

## File Layout

```
SubnetTrader/
├── install.sh              ← new: one-command bare-metal installer
├── Dockerfile              ← update: multi-stage, backend + frontend
├── Dockerfile.backend      ← new (optional): backend-only image
├── docker-compose.yml      ← update: two-service compose (backend + frontend)
├── start.sh                ← update: minor (already portable — fix cron comment)
├── watchdog.sh             ← update: minor (fix cron install comment)
├── .dockerignore           ← new: keep images lean
├── frontend/
│   └── next.config.js      ← no changes needed (cpus=1 is fine)
└── spec/
    └── packaging.md         ← this file
```

---

## 1. Install Script (`install.sh`)

A single idempotent bash script. Safe to re-run — it skips steps that are already done.

### Usage

```bash
git clone <repo-url> SubnetTrader && cd SubnetTrader
chmod +x install.sh
./install.sh
```

### Flow

```
┌─────────────────────────────────────────────────┐
│  1. Detect OS / arch                            │
│  2. Check system prerequisites                  │
│  3. Create Python venv + install deps           │
│  4. Install Node (if missing) + npm install     │
│  5. Copy .env.example → .env (if not exists)    │
│  6. Create data/ directory structure            │
│  7. Optionally install watchdog cron            │
│  8. Print "open http://localhost:3000/setup"     │
└─────────────────────────────────────────────────┘
```

### Step Details

#### Step 1 — Detect OS / arch

```bash
OS="$(uname -s)"    # Linux, Darwin
ARCH="$(uname -m)"  # aarch64, arm64, x86_64
```

Supported matrix:

| OS    | Arch             | Label          | Notes                    |
|-------|------------------|----------------|--------------------------|
| Linux | aarch64          | Pi (arm64)     | Raspberry Pi 4/5         |
| Linux | x86_64           | Linux (x86)    | Desktop / server / VM    |
| Darwin| arm64            | macOS (Apple)  | M1/M2/M3/M4 Mac         |
| Darwin| x86_64           | macOS (Intel)  | Intel Mac                |

If the OS/arch combination is not in this table, print a warning but continue — most
steps will work regardless.

#### Step 2 — Check system prerequisites

Required tools: `python3` (≥ 3.11), `pip`, `git`, `curl`.

```bash
check_cmd() {
    command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 is required but not found."; exit 1; }
}
check_cmd python3
check_cmd pip3
check_cmd git
check_cmd curl
```

Check Python version:

```bash
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "ERROR: Python 3.11+ required (found $PY_VERSION)"
    exit 1
fi
```

If `python3` is missing, print OS-specific install instructions:

| OS     | Instruction                                        |
|--------|----------------------------------------------------|
| Linux  | `sudo apt-get install python3 python3-venv python3-pip` |
| macOS  | `brew install python@3.13` or download from python.org |

#### Step 3 — Create Python venv + install deps

```bash
if [ ! -d ".venv" ]; then
    echo "[2/7] Creating Python virtual environment..."
    python3 -m venv .venv
else
    echo "[2/7] Python venv already exists — skipping creation"
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

On Raspberry Pi (arm64 Linux), `bittensor` may require Rust for building native
extensions. If `pip install` fails with a Rust-related error, print:

```
NOTE: Some packages require Rust. Install it with:
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
Then re-run ./install.sh
```

#### Step 4 — Install Node.js (if missing) + npm install

Check for `node` and `npm`:

```bash
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
```

`install_node` function — platform-specific:

| Platform       | Method                                           |
|----------------|--------------------------------------------------|
| Linux (arm64)  | NodeSource setup script → `apt-get install nodejs` |
| Linux (x86_64) | NodeSource setup script → `apt-get install nodejs` |
| macOS          | `brew install node@20` or NodeSource             |

NodeSource install (Linux):

```bash
install_node() {
    if [ "$OS" = "Linux" ]; then
        echo "Installing Node.js 20.x via NodeSource..."
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
```

**Note:** The NodeSource install requires `sudo`. If the user is not root and `sudo` is
unavailable, print instructions to install Node manually and exit.

After Node is available:

```bash
echo "[4/7] Installing frontend dependencies..."
cd frontend
npm install
cd ..
```

#### Step 5 — Copy `.env.example` → `.env`

```bash
if [ ! -f ".env" ]; then
    echo "[5/7] Creating .env from template..."
    cp .env.example .env
    echo "  → Edit .env or use the setup wizard at http://localhost:3000/setup"
else
    echo "[5/7] .env already exists — keeping existing configuration"
fi
```

Never overwrite an existing `.env` — the user may have configured it already.

#### Step 6 — Create data directory

```bash
echo "[6/7] Setting up data directory..."
mkdir -p data/logs data/exports
```

#### Step 7 — Optionally install watchdog cron

```bash
echo ""
read -p "Install watchdog cron job (restarts bot if it crashes)? [y/N] " INSTALL_CRON
if [[ "$INSTALL_CRON" =~ ^[Yy]$ ]]; then
    CRON_LINE="*/5 * * * * $(pwd)/watchdog.sh >> $(pwd)/data/watchdog.log 2>&1"
    # Check if already installed
    if crontab -l 2>/dev/null | grep -qF "watchdog.sh"; then
        echo "  Watchdog cron already installed — skipping"
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        echo "  ✓ Watchdog cron installed (every 5 minutes)"
    fi
else
    echo "  Skipped watchdog cron (you can install it later — see watchdog.sh header)"
fi
```

**Key detail:** the cron line uses `$(pwd)` — the current absolute path — instead of a
hardcoded `/home/pi/...` path. This makes it work from any install directory.

#### Step 8 — Final output

```bash
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SubnetTrader installed successfully!"
echo ""
echo "  To start:   ./start.sh"
echo "  To configure: open http://localhost:3000/setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
```

### Error Handling

- Each step checks its exit code. If a critical step fails (venv creation, pip install,
  npm install), the script prints the error and exits with a non-zero code.
- Non-critical failures (cron install, Rust warning) print a warning and continue.
- The script is idempotent: re-running it skips completed steps (venv exists, .env exists,
  node_modules exists) and only performs what's needed.

### Non-interactive mode

For CI or automated deploys, support `--yes` flag to skip prompts:

```bash
if [[ "$1" == "--yes" ]] || [[ "$1" == "-y" ]]; then
    AUTO_YES=true
fi
```

When `AUTO_YES` is set, the watchdog cron prompt defaults to "no" (skip). This avoids
hanging in automated pipelines.

---

## 2. Docker Option

### Current State

The existing `Dockerfile` and `docker-compose.yml` are backend-only:
- `Dockerfile`: Python 3.11, no frontend, port 8080, uses non-root `trader` user
- `docker-compose.yml`: single service, mounts `data/` and wallets, port 8080

### Target State

Two-service compose: `backend` (Python/FastAPI) + `frontend` (Next.js). Single
`Dockerfile` with multi-stage build, plus the compose file for orchestration.

### Updated Dockerfile (multi-stage)

```dockerfile
# ── Stage 1: Frontend build ──────────────────────────
FROM node:20-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Backend runtime ─────────────────────────
FROM python:3.11-slim AS backend

LABEL maintainer="Rudy1995T" \
      description="Bittensor Subnet Alpha Trading Bot — Backend"

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./app/
COPY .env.example ./

# Create data directories
RUN mkdir -p /app/data/logs /app/data/exports

# Non-root user
RUN useradd --create-home trader \
    && chown -R trader:trader /app
USER trader

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8081/health || exit 1

EXPOSE 8081

ENTRYPOINT ["python", "-m", "app.main"]

# ── Stage 3: Frontend runtime ────────────────────────
FROM node:20-slim AS frontend

WORKDIR /app/frontend
COPY --from=frontend-build /app/frontend/.next ./.next
COPY --from=frontend-build /app/frontend/node_modules ./node_modules
COPY frontend/package.json ./
COPY frontend/public ./public
COPY frontend/next.config.js ./

RUN useradd --create-home trader \
    && chown -R trader:trader /app
USER trader

EXPOSE 3000

CMD ["npm", "run", "start"]
```

**Key decisions:**

- **Multi-stage, multi-target:** `docker build --target backend .` or
  `docker build --target frontend .` to build each service independently. The compose
  file uses `target:` to select which stage to build.
- **Frontend uses `npm run start`** (production mode) in Docker, not `npm run dev`.
  This is faster and more stable in containers.
- **Port 8081** (not 8080): match the bare-metal config. The old Dockerfile used 8080;
  update to 8081 for consistency with `start.sh` and the frontend's API URL default.
- **curl added** to backend for healthcheck (replaces the Python-based healthcheck which
  is slower to start).
- **Only `app/` is copied** to the backend image (not the entire repo), keeping it lean.

### Platform Support (arm64 / x86)

Both `python:3.11-slim` and `node:20-slim` publish multi-arch images for `linux/amd64`
and `linux/arm64`. No special handling needed — Docker automatically pulls the correct
architecture.

For explicit multi-arch builds (e.g., publishing to a registry):

```bash
docker buildx build --platform linux/amd64,linux/arm64 --target backend -t subnettrader-backend .
docker buildx build --platform linux/amd64,linux/arm64 --target frontend -t subnettrader-frontend .
```

### Updated docker-compose.yml

```yaml
services:
  backend:
    build:
      context: .
      dockerfile: Dockerfile
      target: backend
    container_name: subnettrader-backend
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      # Persist database and logs
      - ./data:/app/data
      # Mount wallet directory (read-only for safety)
      - ${BT_WALLET_PATH:-~/.bittensor/wallets}:/home/trader/.bittensor/wallets:ro
      # Kill switch
      - .:/app/host:ro
    environment:
      - KILL_SWITCH_PATH=/app/host/KILL_SWITCH
      - DB_PATH=/app/data/ledger.db
      - JSONL_DIR=/app/data/logs
      - HEALTH_PORT=8081
    ports:
      - "8081:8081"
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8081/health"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"

  frontend:
    build:
      context: .
      dockerfile: Dockerfile
      target: frontend
    container_name: subnettrader-frontend
    restart: unless-stopped
    environment:
      - NEXT_PUBLIC_API_URL=http://backend:8081
    ports:
      - "3000:3000"
    depends_on:
      backend:
        condition: service_healthy
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

**Key decisions:**

- **`version:` key removed** — deprecated in Compose V2.
- **Two services**: `backend` and `frontend`, each built from a different stage of the
  same Dockerfile via `target:`.
- **`NEXT_PUBLIC_API_URL`**: the frontend container uses the Docker network hostname
  `backend` to reach the API. However, `NEXT_PUBLIC_` env vars are baked in at build time
  in Next.js. For server-side rendering this works, but client-side fetches run in the
  user's browser which can't resolve `backend`. **Solution:** the frontend code already
  falls back to `window.location.hostname:8081` for client-side requests (see the existing
  `API` constant pattern in the frontend). No change needed as long as the backend port
  (8081) is published on the host. The `NEXT_PUBLIC_API_URL` env var is only used for SSR.
- **`depends_on` with `condition: service_healthy`**: the frontend waits for the backend
  to pass its healthcheck before starting.
- **`.env` is only mounted on the backend** — the frontend has no direct config needs
  beyond `NEXT_PUBLIC_API_URL`.
- **Wallet volume**: same as before, mounted read-only.
- **`data/`** persisted via bind mount.

### .dockerignore

```
.venv/
node_modules/
.next/
data/
*.pyc
__pycache__/
.env
.git/
spec/
tests/
*.md
```

Keep images lean by excluding development artifacts, data, and docs.

### Docker Usage

```bash
# First time
cp .env.example .env          # or use the setup wizard after starting
docker compose up -d --build

# View logs
docker compose logs -f backend
docker compose logs -f frontend

# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build
```

---

## 3. Remove Hardcoded Paths

### Current State

Both `start.sh` and `watchdog.sh` **already use** `SCRIPT_DIR` for runtime paths:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
```

This means they work from any directory. The only hardcoded path is in the **cron install
comment** at the top of `watchdog.sh`:

```bash
#   */5 * * * * /home/pi/Desktop/SN_Bot/SubnetTrader/watchdog.sh >> /home/pi/Desktop/SN_Bot/SubnetTrader/data/watchdog.log 2>&1
```

### Changes Required

#### watchdog.sh — Update cron comment

Replace the hardcoded example with a dynamic instruction:

```bash
# Install:
#   crontab -e
#   */5 * * * * /path/to/SubnetTrader/watchdog.sh >> /path/to/SubnetTrader/data/watchdog.log 2>&1
#
# Or use install.sh which sets this up automatically with the correct paths.
```

This is the **only change** needed — both scripts are already portable.

#### start.sh — No changes needed

`start.sh` already uses `SCRIPT_DIR` throughout. The `cd "$SCRIPT_DIR"` at the top
ensures all relative paths resolve correctly regardless of where the script is invoked
from.

#### frontend/next.config.js — No changes needed

Already handles low-resource environments with `cpus: 1`. No hardcoded paths.

---

## 4. Windows Support

Native Windows is not supported (`install.sh`, `start.sh`, and `watchdog.sh` are bash
scripts). Windows users have two paths: **Docker Desktop** (recommended) or **WSL2**.

### Option A — Docker Desktop (recommended)

The simplest path. Docker Desktop for Windows runs Linux containers transparently.

#### Prerequisites

- Windows 10 (build 19041+) or Windows 11
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- WSL2 backend enabled in Docker Desktop settings (default since Docker Desktop 4.x)

#### Steps

```powershell
# PowerShell or Command Prompt
git clone <repo-url> SubnetTrader
cd SubnetTrader
copy .env.example .env        # edit .env, or use the setup wizard after starting
docker compose up -d --build
# Open http://localhost:3000/setup in your browser
```

Everything else works identically to Linux/macOS Docker — the compose file, volumes, and
port mappings are cross-platform.

#### Wallet volume on Windows

The default wallet path `~/.bittensor/wallets` resolves differently on Windows. If using
Docker Desktop with the WSL2 backend, the wallet directory must be accessible from WSL2.

Two options:

1. **Store wallets inside WSL2** (preferred): the default `~/.bittensor/wallets` in the
   WSL2 distro works automatically since Docker Desktop mounts WSL2 filesystems.

2. **Store wallets on Windows filesystem**: update `.env` to use the Windows path mounted
   in WSL2, e.g.:
   ```
   BT_WALLET_PATH=/mnt/c/Users/YourName/.bittensor/wallets
   ```

#### Windows-specific notes

- `data/` bind mount works with Docker Desktop (uses WSL2 filesystem or Windows paths).
- Port forwarding (`8081`, `3000`) works out of the box — `localhost` on Windows reaches
  Docker containers.
- The watchdog cron is not available (no cron on Windows). Docker's built-in `restart:
  unless-stopped` policy handles automatic restarts instead.

---

### Option B — WSL2 (bare-metal install inside WSL)

For users who want to run without Docker, or who need direct access to the filesystem.

#### Prerequisites

- Windows 10 (build 19041+) or Windows 11
- WSL2 with Ubuntu 22.04+ installed:
  ```powershell
  wsl --install -d Ubuntu-22.04
  ```

#### Steps

```bash
# Inside WSL2 terminal (Ubuntu)
sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip git curl

git clone <repo-url> SubnetTrader
cd SubnetTrader
chmod +x install.sh
./install.sh
```

From here, everything works exactly as documented in the Linux install path. The `install.sh`
script detects `Linux` + `x86_64` and proceeds normally.

#### Accessing from Windows browser

WSL2 services are accessible from the Windows host browser at `http://localhost:3000`
(WSL2 automatically forwards ports). If this doesn't work (older WSL2 builds), find the
WSL2 IP:

```bash
hostname -I    # inside WSL2, e.g. 172.28.123.45
```

Then open `http://172.28.123.45:3000` in the Windows browser.

#### Wallet access

If the Bittensor wallet was created inside WSL2, it's already at `~/.bittensor/wallets`
and works as-is. If the wallet is on the Windows filesystem:

```bash
# In .env
BT_WALLET_PATH=/mnt/c/Users/YourName/.bittensor/wallets
```

#### Watchdog cron in WSL2

Cron may not be running by default in WSL2. Enable it:

```bash
sudo service cron start
```

To make cron start automatically on WSL2 boot, add to `/etc/wsl.conf`:

```ini
[boot]
command = service cron start
```

Then the `install.sh` cron setup works normally.

---

### Option summary

| Scenario | Recommended path | Watchdog | Performance |
|----------|-----------------|----------|-------------|
| Windows, just want it running | Docker Desktop | Built-in restart policy | Good |
| Windows, need filesystem access | WSL2 bare-metal | Cron (needs manual enable) | Native Linux speed |
| Windows, already use WSL2 daily | WSL2 bare-metal | Cron | Native Linux speed |

---

## Validation Checklist

Before marking this phase complete, verify:

- [ ] `install.sh` runs cleanly on a fresh Pi (arm64) with no pre-existing setup
- [ ] `install.sh` runs cleanly on x86_64 Linux (Ubuntu/Debian)
- [ ] `install.sh` runs cleanly on macOS (Apple Silicon)
- [ ] `install.sh` is idempotent — second run skips completed steps
- [ ] `install.sh --yes` runs without prompts
- [ ] `docker compose up -d --build` starts both services on Linux
- [ ] `docker compose up -d --build` starts both services on Windows (Docker Desktop)
- [ ] Frontend at `http://localhost:3000` reaches backend at `http://localhost:8081`
- [ ] `data/` persists across `docker compose down` / `up` cycles
- [ ] Wallet volume mounts correctly (read-only)
- [ ] Docker images build on both arm64 and x86_64
- [ ] `watchdog.sh` cron comment no longer contains `/home/pi/...`
- [ ] `start.sh` works when repo is cloned to a non-Pi path (e.g., `/opt/subnettrader/`)
- [ ] WSL2 bare-metal install works on Windows with Ubuntu 22.04
- [ ] Windows browser can reach WSL2 services at localhost

---

## Out of Scope

- Systemd service file — could be added later but `start.sh` + watchdog cron covers
  the Pi use case. Docker covers production deployments.
- Kubernetes / Helm chart — out of scope for a single-operator bot.
- Native Windows (PowerShell `install.ps1`) — WSL2 and Docker Desktop cover all Windows
  use cases without maintaining a separate script.
- Auto-updating / self-update mechanism.
- Frontend production build for bare-metal (dev mode via `npm run dev` is acceptable
  for single-user Pi deployments; Docker uses production build).
- Publishing Docker images to a registry (users build locally).
