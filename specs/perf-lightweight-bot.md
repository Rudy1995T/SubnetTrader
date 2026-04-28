# Spec: Lightweight Bot — Trim Memory & CPU on Pi 5 (4GB)

## Goal
Reduce SubnetTrader's resident memory and CPU pressure on the 4GB Raspberry Pi 5 without
degrading trading performance, signal accuracy, or exit responsiveness.

## Non-goals
- No changes to trading math, EMA strategy logic, or stop/take-profit thresholds.
- No removal of Strategy A vs Strategy B feature parity (only gate by config flag).
- No restructuring of `ema_manager.py` or `main.py` (too risky for the win).

---

## Findings (from codebase scan)

### High impact
1. **Unbounded JSONL logs** — `app/logging/logger.py` writes daily files with no rotation
   or retention. Observed ~63 MB across 5 days. Each write opens the file fresh.
2. **Frontend running in dev mode** — `npm run dev` keeps the Next.js dev server + HMR +
   `node_modules` (408 MB) hot. Production build (`.next/`) is ~284 KB.
3. **Dead Strategy B allocation** — even when `EMA_B_ENABLED=false`, the second
   `EmaManager` and its caches/watchers may still init. Need to verify and gate.

### Low impact / cleanup
4. **Dead deps** — `matplotlib` and `requests` are in `requirements.txt` but never
   imported in `app/`. Removing them slims venv install size; no runtime change.
5. **`tsconfig.tsbuildinfo`** committed/untracked in `frontend/` — build artifact, add
   to `.gitignore`.

---

## Changes

### 1. Log rotation & retention (highest ROI)
**File:** `app/logging/logger.py`

- Replace per-write `open(..., "a")` with a long-lived file handle, reopened only on
  date rollover.
- Add a startup task in `app/main.py` that, on boot and once per 24h, deletes JSONL files
  in `data/logs/` older than `LOG_RETENTION_DAYS` (default 3).
- Add `LOG_RETENTION_DAYS: int = 3` to `app/config.py`.
- Cap individual file size at 10 MB; if exceeded, suffix with `.1`, `.2` and start fresh.

**Expected:** ~30–50 MB recovered on disk; fewer syscalls per log line.

### 2. Frontend → production build
**File:** `start.sh` (and docs in `MEMORY.md` later)

- Replace `npm run dev` with `npm run build && npm run start` (or `next start -p 3000`).
- Confirm `.next/` exists; if not, run build first.
- Document tradeoff: rebuild needed after frontend edits.

**Expected:** ~150–250 MB RSS reduction (no dev server, no HMR, no TS watcher).

### 3. Gate Strategy B fully behind `EMA_B_ENABLED`
**Files:** `app/main.py`, `app/portfolio/ema_manager.py` (init paths)

- At startup, if `EMA_B_ENABLED=false`: skip `EmaManager` instantiation for B, skip
  registering its watchers, and skip its API routes (or have them 503).
- Same symmetric check for Strategy A under `EMA_ENABLED`.
- Add an assertion log line on boot: `Active strategies: [A]` so it's visible.

**Expected:** ~5–15 MB if B was idle but instantiated.

### 4. Dependency cleanup
**File:** `requirements.txt` (or `pyproject.toml`)

- Remove `matplotlib`, `requests`. Run `pip uninstall` after.
- Verify nothing imports them: `grep -rn "import matplotlib\|import requests" app/`.

**Expected:** Smaller venv on disk; no runtime change.

### 5. Misc
- Add `frontend/tsconfig.tsbuildinfo` to `.gitignore`.
- Add `data/logs/*.jsonl` retention note to README/MEMORY.

---

## Validation
1. `pgrep -f "python.*app.main"` running, `/api/ema/portfolio` returns 200.
2. `ps -o rss= -p $(pgrep -f app.main)` — record before/after; expect ≥30 MB drop.
3. Frontend reachable on `:3000` after switching to `next start`.
4. Open one EMA position end-to-end in dry-run; confirm exit watcher fires within 60s
   of crossing TP/SL.
5. After 24h: `du -sh data/logs/` should be < 30 MB; old files pruned.
6. Confirm only the enabled strategy's watchers appear in logs.

## Rollout
- One PR per section. Section 1 (logs) and 2 (frontend) first — they are highest impact
  and lowest risk.
- After each merge: restart bot, capture RSS, watch for 1 hour.

## Risks
- **Frontend prod build** requires rebuild after edits — document clearly.
- **Strategy gating** — risk of accidentally disabling both; assertion log on boot.

## Estimated total savings
~200–300 MB RSS (mostly from frontend prod build) + 30–50 MB disk (logs) + bounded
long-term growth.
