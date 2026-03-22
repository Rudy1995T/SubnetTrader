# SubnetTrader Wallet Management — Specification

## Overview

Add wallet creation and validation capabilities to the setup wizard and settings page.
New users who don't yet have a Bittensor wallet can create one directly from the UI.
Users with an existing wallet get deeper validation: filesystem checks, hotkey existence,
coldkey unlock test, and on-chain balance display.

**Goal:** a brand-new user who has never touched Bittensor can go from zero to a funded
wallet without leaving the SubnetTrader UI.

---

## File Layout

```
SubnetTrader/
├── app/
│   ├── config_api.py              ← update: add /api/config/wallet/* endpoints
│   └── config.py                  ← unchanged
├── frontend/src/
│   ├── components/
│   │   └── SetupWizard.tsx        ← update: replace Step 1 (Wallet) with expanded flow
│   └── app/
│       └── settings/
│           └── page.tsx           ← update: wallet section gains create + validate UI
└── spec/
    └── wallet-management.md       ← this file
```

---

## Backend: Wallet Endpoints

All new endpoints live under the existing `config_api.py` router (`/api/config`).

### `GET /api/config/wallet/detect`

Check whether a wallet already exists at the given (or default) path.

**Query params:**

| Param | Default | Notes |
|-------|---------|-------|
| `wallet_name` | `default` | Wallet directory name |
| `hotkey` | `default` | Hotkey name |
| `wallet_path` | `~/.bittensor/wallets` | Base wallet directory |

**Response:**

```json
{
  "wallet_path_exists": true,
  "coldkey_exists": true,
  "hotkey_exists": true,
  "coldkey_encrypted": true,
  "coldkey_ss58": "5CJVx..."
}
```

**Behaviour:**

1. Expand `~` in `wallet_path` using `os.path.expanduser()`.
2. Check `{wallet_path}/{wallet_name}/` directory exists → `wallet_path_exists`.
3. Check `{wallet_path}/{wallet_name}/coldkey` file exists → `coldkey_exists`.
4. Check `{wallet_path}/{wallet_name}/hotkeys/{hotkey}` file exists → `hotkey_exists`.
5. If coldkey exists, attempt to read it and determine if encrypted:
   - Use `bittensor.Wallet(name, hotkey, path)` and try accessing `.coldkeypub`.
   - If the public key is readable, return the `coldkey_ss58` address.
   - Check if the coldkey file is encrypted (try `wallet.coldkey` — if it throws a
     decryption error, `coldkey_encrypted = true`; if it loads without password,
     `coldkey_encrypted = false`).
6. If coldkey doesn't exist, `coldkey_ss58` is `null` and `coldkey_encrypted` is `null`.

This endpoint is **read-only** and never modifies the filesystem.

---

### `POST /api/config/wallet/create`

Creates a new Bittensor wallet (coldkey + hotkey) by shelling out to `btcli`.

**Request body:**

```json
{
  "wallet_name": "trader_wallet",
  "hotkey": "trader_hotkey",
  "wallet_path": "~/.bittensor/wallets",
  "password": "optional-encryption-password"
}
```

**Response (success):**

```json
{
  "success": true,
  "coldkey_ss58": "5Dxq...",
  "mnemonic": "word1 word2 word3 ... word12",
  "message": "Wallet created successfully. SAVE YOUR MNEMONIC — it cannot be recovered."
}
```

**Response (failure):**

```json
{
  "success": false,
  "error": "Wallet 'trader_wallet' already exists at ~/.bittensor/wallets"
}
```

**Behaviour:**

1. **Pre-check:** if `{wallet_path}/{wallet_name}/coldkey` already exists, return error
   immediately — never overwrite an existing wallet.
2. **Create coldkey** using the Bittensor SDK directly (not subprocess):
   ```python
   import bittensor as bt
   wallet = bt.Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
   wallet.create_new_coldkey(use_password=bool(password), overwrite=False)
   ```
   If `password` is provided, the SDK encrypts the coldkey with it. If empty, the coldkey
   is stored unencrypted (simpler for automated bots, but less secure — the UI warns about
   this).
3. **Create hotkey:**
   ```python
   wallet.create_new_hotkey(overwrite=False)
   ```
4. **Capture mnemonic:** The SDK prints the mnemonic to stdout during creation. To capture
   it, use `contextlib.redirect_stdout` to intercept the output, then parse the 12-word
   mnemonic from it. Alternatively, use the lower-level `bt.Keypair.create_from_mnemonic()`
   approach:
   ```python
   from bittensor import Keypair
   mnemonic = Keypair.generate_mnemonic()
   keypair = Keypair.create_from_mnemonic(mnemonic)
   # Then manually save the keypair to the wallet path
   ```
   The second approach gives full control over the mnemonic. Use whichever is more reliable
   with the installed SDK version — test both paths.
5. **Return** the coldkey SS58 address and the mnemonic. The mnemonic is shown **once** in
   the UI and never stored on the server.

**Security considerations:**
- The mnemonic is transmitted over localhost only (no TLS needed for LAN-only bot).
- The response is not logged — add an explicit `logger.info("Wallet created for %s", wallet_name)`
  without including the mnemonic.
- The endpoint refuses to overwrite existing wallets (`overwrite=False`).

---

### `POST /api/config/wallet/validate`

Deep validation of an existing wallet: unlock test, balance check, hotkey registration status.

This is an enhanced version of the existing `POST /api/config/test-wallet` endpoint. The
existing endpoint is kept for backward compatibility but internally delegates to the same
logic.

**Request body:**

```json
{
  "wallet_name": "trader_wallet",
  "hotkey": "trader_hotkey",
  "wallet_path": "~/.bittensor/wallets",
  "password": "..."
}
```

**Response (success):**

```json
{
  "success": true,
  "checks": {
    "coldkey_exists": true,
    "hotkey_exists": true,
    "coldkey_unlockable": true,
    "coldkey_ss58": "5CJVx...",
    "balance_tao": 12.34,
    "balance_sufficient": true
  },
  "warnings": []
}
```

**Response (partial success — wallet exists but has issues):**

```json
{
  "success": true,
  "checks": {
    "coldkey_exists": true,
    "hotkey_exists": false,
    "coldkey_unlockable": true,
    "coldkey_ss58": "5CJVx...",
    "balance_tao": 0.0,
    "balance_sufficient": false
  },
  "warnings": [
    "Hotkey 'trader_hotkey' not found — create it or check the name",
    "Balance is 0 TAO — fund your wallet before trading"
  ]
}
```

**Response (failure):**

```json
{
  "success": false,
  "error": "Wallet not found at ~/.bittensor/wallets/trader_wallet"
}
```

**Behaviour:**

1. **Check coldkey file exists** at `{wallet_path}/{wallet_name}/coldkey`.
   If not → return `success: false` with clear error.
2. **Check hotkey file exists** at `{wallet_path}/{wallet_name}/hotkeys/{hotkey}`.
   If not → `hotkey_exists: false`, add warning (don't fail — user might be creating it).
3. **Test coldkey unlock:**
   - If `password` is provided: call `wallet.unlock_coldkey(password=password)`.
   - If no password and coldkey is encrypted: `coldkey_unlockable: false`, add warning.
   - If no password and coldkey is unencrypted: loads fine, `coldkey_unlockable: true`.
   - On wrong password: `coldkey_unlockable: false`, add warning "Wrong password".
4. **Read SS58 address** from `wallet.coldkeypub.ss58_address`.
5. **Query TAO balance** via `bt.Subtensor().get_balance(coldkey_ss58)`.
   - If balance >= `EMA_POT_TAO` (from current settings or 10.0 default): `balance_sufficient: true`.
   - If balance < pot size: `balance_sufficient: false`, add warning with the shortfall.
   - If chain query fails (timeout, network issue): set `balance_tao: null` and add warning
     "Could not query balance — chain unreachable".
6. **Timeout:** 20 seconds total (chain query can be slow on first connection).

---

## Frontend: Setup Wizard Step 1 (Wallet) — Updated

The current Step 1 ("Connect Your Wallet") is replaced with a two-phase flow within the
same step. The step indicator still shows as Step 1 — the sub-phases are internal.

### Phase A — Wallet Detection

On entering Step 1, the wizard automatically calls `GET /api/config/wallet/detect` with the
current field values (defaulting to `default` / `default` / `~/.bittensor/wallets`).

**UI while detecting:**

```
┌───────────────────────────────────────────────────────┐
│  Step 1: Connect Your Wallet                          │
│                                                       │
│  Checking for existing wallet...  [spinner]           │
│                                                       │
└───────────────────────────────────────────────────────┘
```

**If wallet IS found** (`coldkey_exists: true`):

Proceed directly to Phase B (Existing Wallet) with fields pre-populated.

**If wallet is NOT found** (`coldkey_exists: false`):

Show two options:

```
┌───────────────────────────────────────────────────────┐
│  Step 1: Connect Your Wallet                          │
│                                                       │
│  No wallet found at ~/.bittensor/wallets/default      │
│                                                       │
│  ┌──────────────────────┐  ┌────────────────────────┐ │
│  │  🔑 Create New       │  │  📂 I Have a Wallet    │ │
│  │                      │  │                        │ │
│  │  Create a Bittensor  │  │  Enter your existing   │ │
│  │  wallet right here   │  │  wallet credentials    │ │
│  └──────────────────────┘  └────────────────────────┘ │
│                                                       │
│  New to Bittensor? Learn more:                        │
│  https://docs.bittensor.com/getting-started           │
│                                                       │
└───────────────────────────────────────────────────────┘
```

- **"Create New"** → goes to Phase C (Create Wallet)
- **"I Have a Wallet"** → goes to Phase B (Existing Wallet) with empty fields

---

### Phase B — Existing Wallet (validate)

This is the current Step 1 with enhanced validation. Fields:

| Field | Label | Type | Required | Default |
|-------|-------|------|----------|---------|
| `BT_WALLET_NAME` | Wallet Name | text | yes | `default` |
| `BT_WALLET_HOTKEY` | Hotkey Name | text | yes | `default` |
| `BT_WALLET_PATH` | Wallet Path | text | yes | `~/.bittensor/wallets` |
| `BT_WALLET_PASSWORD` | Coldkey Password | password | no | (empty) |

**"Verify Wallet" button:**

Calls `POST /api/config/wallet/validate` and displays results inline:

```
┌───────────────────────────────────────────────────────┐
│  Step 1: Connect Your Wallet                          │
│                                                       │
│  Wallet Name      [ trader_wallet     ]               │
│  Hotkey Name      [ trader_hotkey     ]               │
│  Wallet Path      [ ~/.bittensor/...  ]               │
│  Password         [ ••••••••          ] 👁             │
│                                                       │
│  [ Verify Wallet ]                                    │
│                                                       │
│  ┌─ Verification Results ──────────────────────────┐  │
│  │  ✓ Coldkey found                                │  │
│  │  ✓ Hotkey found                                 │  │
│  │  ✓ Coldkey unlocked successfully                │  │
│  │  Address: 5CJVxybdv6kcwMwnegJw...              │  │
│  │  Balance: 12.34 TAO                             │  │
│  │                                                 │  │
│  │  ⚠ Balance (12.34 TAO) is close to your        │  │
│  │    trading pot (10 TAO) — consider adding more  │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  [Back]                              [Next →]         │
│                                                       │
└───────────────────────────────────────────────────────┘
```

**Verification result states:**

| Check | Pass | Fail |
|-------|------|------|
| Coldkey exists | ✓ Coldkey found | ✗ Coldkey not found at {path} |
| Hotkey exists | ✓ Hotkey found | ✗ Hotkey '{name}' not found |
| Coldkey unlock | ✓ Coldkey unlocked successfully | ✗ Wrong password / ✗ Password required |
| Balance | Balance: X.XX TAO | ⚠ Balance: 0 TAO — fund wallet before trading |

Colour coding: ✓ = `text-emerald-400`, ✗ = `text-red-400`, ⚠ = `text-amber-400`.

The **"Next"** button is always enabled (verification is optional) but shows a subtle
warning if verification failed or wasn't attempted: "Wallet not verified — you can verify
later from Settings."

---

### Phase C — Create Wallet

A guided wallet creation flow within Step 1.

```
┌───────────────────────────────────────────────────────┐
│  Step 1: Create Your Wallet                           │
│                                                       │
│  A Bittensor wallet consists of a coldkey (for        │
│  holding funds) and a hotkey (for staking operations).│
│  We'll create both for you.                           │
│                                                       │
│  Wallet Name      [ trader_wallet     ]               │
│  Hotkey Name      [ trader_hotkey     ]               │
│  Wallet Path      [ ~/.bittensor/...  ]               │
│                                                       │
│  Encrypt coldkey?  [ ON ]                             │
│  Password         [ ••••••••          ] 👁             │
│  Confirm Password [ ••••••••          ] 👁             │
│                                                       │
│  ⚠ If you encrypt your coldkey, you'll need this     │
│    password every time the bot starts. If you lose    │
│    it, your funds are recoverable only via the        │
│    mnemonic seed phrase.                              │
│                                                       │
│  [ Create Wallet ]                                    │
│                                                       │
└───────────────────────────────────────────────────────┘
```

**Fields:**

| Field | Label | Type | Required | Default |
|-------|-------|------|----------|---------|
| Wallet Name | Wallet Name | text | yes | `trader_wallet` |
| Hotkey Name | Hotkey Name | text | yes | `trader_hotkey` |
| Wallet Path | Wallet Path | text | yes | `~/.bittensor/wallets` |
| Encrypt toggle | Encrypt Coldkey | toggle | no | ON |
| Password | Password | password | if encrypt ON | (empty) |
| Confirm Password | Confirm Password | password | if encrypt ON | (empty) |

**Client-side validation:**
- Wallet name / hotkey: non-empty, alphanumeric + underscore, max 64 chars.
- If encrypt is ON: password and confirm must match, minimum 8 characters.
- If encrypt is OFF: show warning "Your coldkey will be stored unencrypted. Anyone with
  access to this machine can use your wallet."

**On "Create Wallet" click:**

1. Disable the button, show spinner: "Creating wallet..."
2. Call `POST /api/config/wallet/create` with the form values.
3. On success → show the **Mnemonic Display** sub-view (see below).
4. On failure → show red error inline (e.g., "Wallet already exists").

---

### Mnemonic Display (after successful creation)

This is the most critical UI in the entire wizard — the user must save their mnemonic.

```
┌───────────────────────────────────────────────────────┐
│  ⚠  SAVE YOUR RECOVERY PHRASE                        │
│                                                       │
│  This is the ONLY way to recover your wallet if you   │
│  lose access. Write it down and store it securely.    │
│  It will NOT be shown again.                          │
│                                                       │
│  ┌─────────────────────────────────────────────────┐  │
│  │  1. word    2. word    3. word    4. word       │  │
│  │  5. word    6. word    7. word    8. word       │  │
│  │  9. word   10. word   11. word   12. word       │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  [ Copy to Clipboard ]                                │
│                                                       │
│  Your coldkey address (for funding):                  │
│  ┌─────────────────────────────────────────────────┐  │
│  │  5Dxq...full-ss58-address...                    │  │
│  └─────────────────────────────────────────────────┘  │
│  [ Copy Address ]                                     │
│                                                       │
│  ☐ I have saved my recovery phrase securely           │
│                                                       │
│  [Continue →]  (disabled until checkbox is checked)   │
│                                                       │
└───────────────────────────────────────────────────────┘
```

**Key design decisions:**

- The mnemonic is shown in a grid with numbered words (4 columns × 3 rows).
- Background: `bg-amber-900/20 border-amber-700` to convey urgency.
- "Copy to Clipboard" button uses `navigator.clipboard.writeText()`.
- The SS58 address is shown below with its own copy button — the user needs this to fund
  their wallet from an exchange or another wallet.
- The **"Continue"** button is disabled until the user checks "I have saved my recovery
  phrase securely." This is a friction gate to prevent users from skipping past the mnemonic.
- After clicking Continue, the wizard:
  1. Clears the mnemonic from React state (set to `null`).
  2. Auto-fills Step 1 fields with the just-created wallet name, hotkey, and path.
  3. Stores `BT_WALLET_PASSWORD` in form state if encryption was used.
  4. Proceeds to Phase B (Existing Wallet) in a verified state — skipping the verify
     button since we just created it, and showing a green "Wallet created and verified"
     banner.

---

### Funding Guidance (inline in Phase B after creation)

After wallet creation, Phase B shows an additional info card:

```
┌─ Fund Your Wallet ──────────────────────────────────┐
│                                                     │
│  Your wallet is empty. To start trading, send TAO   │
│  to your coldkey address:                           │
│                                                     │
│  5Dxq...full-ss58-address...   [ Copy ]             │
│                                                     │
│  You can fund your wallet from:                     │
│  • An exchange (Binance, MEXC, Gate.io, etc.)       │
│  • Another Bittensor wallet                         │
│  • The Bittensor faucet (testnet only)              │
│                                                     │
│  You can continue setup now and fund later.         │
│  The bot won't trade until your balance covers      │
│  at least one position.                             │
│                                                     │
└─────────────────────────────────────────────────────┘
```

This card is shown only when `balance_tao === 0` and the wallet was just created (tracked
via local state `walletJustCreated: boolean`).

---

## Frontend: Settings Page — Wallet Section Updates

The wallet section on `/settings` gains two new capabilities:

### 1. Expanded Validation Display

The existing "Verify Wallet" button now calls `POST /api/config/wallet/validate` (instead
of the simpler `POST /api/config/test-wallet`) and shows the full checklist:

```
┌─ Wallet ──────────────────────────────────────────────┐
│  Wallet Name    [trader_wallet    ]                   │
│  Hotkey Name    [trader_hotkey    ]                   │
│  Wallet Path    [~/.bittensor/... ]                   │
│  Password       [••••••••         ] 👁                │
│                                                       │
│  [Verify Wallet]                                      │
│                                                       │
│  ✓ Coldkey found                                      │
│  ✓ Hotkey found                                       │
│  ✓ Coldkey unlocked                                   │
│  Address: 5CJVxybdv6kcwMwnegJw...                     │
│  Balance: 12.34 TAO                                   │
│                                                       │
│  ─── or ───                                           │
│                                                       │
│  Don't have a wallet?  [ Create New Wallet ]          │
│                                                       │
└───────────────────────────────────────────────────────┘
```

### 2. "Create New Wallet" Button

Shown at the bottom of the Wallet section. Clicking it opens a **modal** (not a separate
page) containing the same Phase C (Create Wallet) form and mnemonic display described above.

After creation, the modal closes and the wallet fields on the settings page are auto-filled
with the new wallet's name, hotkey, and path. The verification results update automatically.

The modal uses the same styling as the wizard:
- Overlay: `bg-black/60 backdrop-blur-sm`
- Modal: `bg-gray-900 border border-gray-800 rounded-xl max-w-lg mx-auto`
- Close button (X) in the top-right corner — but **disabled** while the mnemonic is being
  shown (user must check the "I have saved" checkbox and click Continue first).

---

## Shared Component: `WalletCreator`

Extract the wallet creation + mnemonic display flow into a reusable component used by both
the setup wizard and the settings modal.

```tsx
// frontend/src/components/WalletCreator.tsx

interface WalletCreatorProps {
  onCreated: (wallet: {
    wallet_name: string;
    hotkey: string;
    wallet_path: string;
    password: string;
    coldkey_ss58: string;
  }) => void;
  onCancel?: () => void;  // only in settings modal (has a back button)
}
```

**Internal state machine:**

```
[Form] → "Create Wallet" → [Creating...] → [Mnemonic Display] → "Continue" → onCreated()
                                ↓ error
                           [Form with error]
```

---

## State Flow Summary

### Setup Wizard (first-run)

```
Step 1 → detect()
  ├── wallet exists → Phase B (validate)
  └── wallet not found → choice
        ├── "Create New" → Phase C → mnemonic → Phase B (verified)
        └── "I Have a Wallet" → Phase B (manual entry)

Step 1 → "Next" → Step 2 (API Keys) → Step 3 (Telegram) → Step 4 (Trading) → Step 5 (Review)
```

### Settings Page

```
Wallet section shows current values + verify button
  └── "Create New Wallet" → modal (Phase C) → mnemonic → auto-fill fields
```

---

## Backend Implementation Details

### Wallet detect — `config_api.py` additions

```python
@router.get("/wallet/detect")
async def wallet_detect(
    wallet_name: str = "default",
    hotkey: str = "default",
    wallet_path: str = "~/.bittensor/wallets",
):
    expanded = os.path.expanduser(wallet_path)
    wallet_dir = Path(expanded) / wallet_name
    coldkey_path = wallet_dir / "coldkey"
    hotkey_path = wallet_dir / "hotkeys" / hotkey

    result = {
        "wallet_path_exists": wallet_dir.is_dir(),
        "coldkey_exists": coldkey_path.exists(),
        "hotkey_exists": hotkey_path.exists(),
        "coldkey_encrypted": None,
        "coldkey_ss58": None,
    }

    if result["coldkey_exists"]:
        try:
            import bittensor as bt
            w = bt.Wallet(name=wallet_name, hotkey=hotkey, path=expanded)
            result["coldkey_ss58"] = w.coldkeypub.ss58_address
            # Test if encrypted
            try:
                _ = w.coldkey
                result["coldkey_encrypted"] = False
            except Exception:
                result["coldkey_encrypted"] = True
        except Exception:
            pass

    return JSONResponse(content=result)
```

### Wallet create — `config_api.py` additions

```python
@router.post("/wallet/create")
async def wallet_create(body: dict[str, str]):
    wallet_name = body.get("wallet_name", "").strip()
    hotkey_name = body.get("hotkey", "").strip()
    wallet_path = body.get("wallet_path", "~/.bittensor/wallets").strip()
    password = body.get("password", "")

    if not wallet_name or not hotkey_name:
        return JSONResponse(content={
            "success": False,
            "error": "Wallet name and hotkey name are required",
        })

    # Validate names (same rules as config fields)
    import re
    for name, label in [(wallet_name, "Wallet name"), (hotkey_name, "Hotkey name")]:
        if not re.match(r"^[A-Za-z0-9_]+$", name) or len(name) > 64:
            return JSONResponse(content={
                "success": False,
                "error": f"{label}: only letters, numbers, underscores (max 64 chars)",
            })

    expanded = os.path.expanduser(wallet_path)
    coldkey_path = Path(expanded) / wallet_name / "coldkey"
    if coldkey_path.exists():
        return JSONResponse(content={
            "success": False,
            "error": f"Wallet '{wallet_name}' already exists at {wallet_path}",
        })

    try:
        import bittensor as bt

        # Generate mnemonic + keypair for coldkey
        mnemonic = bt.Keypair.generate_mnemonic(words=12)
        keypair = bt.Keypair.create_from_mnemonic(mnemonic)

        # Create wallet and write keys
        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=expanded)

        # Create coldkey from our mnemonic
        wallet.create_new_coldkey(
            mnemonic=mnemonic,
            use_password=bool(password),
            overwrite=False,
            suppress=True,
        )

        # Create hotkey
        wallet.create_new_hotkey(overwrite=False, suppress=True)

        coldkey_ss58 = wallet.coldkeypub.ss58_address

        # Log creation (without mnemonic!)
        import logging
        logging.getLogger(__name__).info(
            "Wallet created: name=%s hotkey=%s address=%s",
            wallet_name, hotkey_name, coldkey_ss58,
        )

        return JSONResponse(content={
            "success": True,
            "coldkey_ss58": coldkey_ss58,
            "mnemonic": mnemonic,
            "message": "Wallet created successfully. SAVE YOUR MNEMONIC — it cannot be recovered.",
        })

    except ImportError:
        return JSONResponse(content={
            "success": False,
            "error": "bittensor SDK not installed",
        })
    except Exception as exc:
        return JSONResponse(content={
            "success": False,
            "error": str(exc),
        })
```

**Note on `create_new_coldkey` parameters:**
- The `mnemonic` and `suppress` parameters may vary across Bittensor SDK versions.
  During implementation, check the actual SDK method signature in the installed version
  (`pip show bittensor` → check version, then inspect `bt.Wallet.create_new_coldkey`).
- If the SDK doesn't accept a `mnemonic` param, use the lower-level keypair approach:
  generate mnemonic → create keypair → serialize to JSON → write to the wallet path
  manually (matching the SDK's file format).
- If `suppress` is not available, redirect stdout/stderr during creation to prevent the
  mnemonic from being printed to the bot's log.

### Wallet validate — `config_api.py` additions

```python
@router.post("/wallet/validate")
async def wallet_validate(body: dict[str, str]):
    wallet_name = body.get("wallet_name", "").strip()
    hotkey_name = body.get("hotkey", "").strip()
    wallet_path = body.get("wallet_path", "~/.bittensor/wallets").strip()
    password = body.get("password", "")

    if not wallet_name:
        return JSONResponse(content={"success": False, "error": "Wallet name is required"})

    expanded = os.path.expanduser(wallet_path)
    wallet_dir = Path(expanded) / wallet_name
    coldkey_path = wallet_dir / "coldkey"
    hotkey_path = wallet_dir / "hotkeys" / hotkey_name

    checks = {
        "coldkey_exists": coldkey_path.exists(),
        "hotkey_exists": hotkey_path.exists(),
        "coldkey_unlockable": False,
        "coldkey_ss58": None,
        "balance_tao": None,
        "balance_sufficient": False,
    }
    warnings = []

    if not checks["coldkey_exists"]:
        return JSONResponse(content={
            "success": False,
            "error": f"Wallet not found at {wallet_path}/{wallet_name}",
        })

    if not checks["hotkey_exists"]:
        warnings.append(f"Hotkey '{hotkey_name}' not found — create it or check the name")

    try:
        import bittensor as bt
        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=expanded)

        # Try unlocking
        try:
            _ = wallet.coldkey  # Will fail if encrypted and no password
            checks["coldkey_unlockable"] = True
        except Exception as exc:
            err = str(exc).lower()
            if "decrypt" in err or "password" in err:
                if password:
                    try:
                        wallet.unlock_coldkey(password=password)
                        checks["coldkey_unlockable"] = True
                    except Exception:
                        warnings.append("Wrong password — could not unlock coldkey")
                else:
                    warnings.append("Coldkey is encrypted — provide password to verify")
            else:
                warnings.append(f"Could not load coldkey: {exc}")

        # Get SS58 address (from public key — doesn't need unlock)
        try:
            checks["coldkey_ss58"] = wallet.coldkeypub.ss58_address
        except Exception:
            pass

        # Check balance
        try:
            sub = bt.Subtensor()
            balance = sub.get_balance(checks["coldkey_ss58"])
            checks["balance_tao"] = round(float(balance), 4)

            # Compare against pot size
            from app.config import settings
            pot = settings.EMA_POT_TAO
            checks["balance_sufficient"] = checks["balance_tao"] >= pot
            if checks["balance_tao"] == 0:
                warnings.append("Balance is 0 TAO — fund your wallet before trading")
            elif not checks["balance_sufficient"]:
                warnings.append(
                    f"Balance ({checks['balance_tao']} TAO) is less than "
                    f"trading pot ({pot} TAO)"
                )
        except Exception:
            warnings.append("Could not query balance — chain unreachable")

    except ImportError:
        return JSONResponse(content={
            "success": False,
            "error": "bittensor SDK not installed",
        })

    return JSONResponse(content={
        "success": True,
        "checks": checks,
        "warnings": warnings,
    })
```

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| `bittensor` not installed | All wallet endpoints return `"error": "bittensor SDK not installed"` |
| Wallet path doesn't exist | `detect` returns all `false`; `validate` returns clear error |
| Coldkey encrypted, no password | `validate` warns; `detect` reports `coldkey_encrypted: true` |
| Wrong password | `validate` warns "Wrong password"; does not lock out |
| Chain unreachable | Balance fields return `null`; warning added |
| Wallet creation interrupted | Partial files may exist; subsequent `create` returns "already exists" — user should delete the partial wallet directory manually |
| Mnemonic capture fails | Return error; do not create a wallet the user can't recover |

---

## Styling Notes

All new UI follows the existing dark theme from `onboarding.md`:

- Mnemonic grid: `bg-amber-900/20 border border-amber-700 rounded-lg p-4 font-mono`
- Word cells: `bg-gray-800 rounded px-2 py-1 text-sm` with the index number in `text-gray-500`
- Copy buttons: `bg-gray-700 hover:bg-gray-600 text-sm px-3 py-1 rounded`
- Verification checklist: flex column, each item `flex items-center gap-2`
- Check icons: `✓` in `text-emerald-400`, `✗` in `text-red-400`, `⚠` in `text-amber-400`
- "Create New" / "I Have a Wallet" choice cards: `bg-gray-800 hover:bg-gray-750 border
  border-gray-700 hover:border-indigo-500 rounded-xl p-6 cursor-pointer transition-colors`
- Funding info card: `bg-sky-900/20 border border-sky-800 rounded-lg p-4`

---

## Validation Checklist

Before marking this phase complete, verify:

- [ ] `GET /wallet/detect` correctly identifies existing wallets on the machine
- [ ] `GET /wallet/detect` returns all `false` for non-existent wallet names
- [ ] `POST /wallet/create` creates a valid wallet that `btcli` can recognize
- [ ] `POST /wallet/create` refuses to overwrite existing wallets
- [ ] `POST /wallet/create` returns a valid 12-word mnemonic
- [ ] The mnemonic can restore the wallet via `btcli wallet regen_coldkey`
- [ ] `POST /wallet/validate` checks coldkey, hotkey, unlock, and balance
- [ ] `POST /wallet/validate` handles encrypted coldkeys correctly
- [ ] `POST /wallet/validate` works when chain is unreachable (graceful degradation)
- [ ] Setup wizard detects existing wallet and skips creation flow
- [ ] Setup wizard shows mnemonic once and clears it from state after acknowledge
- [ ] Mnemonic is never written to logs or persistent storage
- [ ] Settings page "Create New Wallet" modal works end-to-end
- [ ] Wallet fields auto-populate after creation in both wizard and settings
- [ ] Copy-to-clipboard works for both mnemonic and SS58 address
- [ ] Password confirmation match is enforced on the create form
- [ ] The "Continue" button is disabled until the mnemonic checkbox is checked

---

## Out of Scope

- Importing a wallet from mnemonic (recovery) — use `btcli wallet regen_coldkey` directly.
- Multiple wallet support — the bot uses one wallet at a time.
- Hotkey registration on subnets — the bot doesn't need its own registered hotkey; it
  stakes to existing validators.
- Hardware wallet (Ledger) support.
- Remote wallet creation (wallet must be on the same machine as the bot).
- Wallet backup / export to file.
