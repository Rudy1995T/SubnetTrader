"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import WalletCreator from "./WalletCreator";

const API =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8081`
    : "http://localhost:8081");

// ── Types ───────────────────────────────────────────────────────────────────

export interface ConfigValues {
  BT_WALLET_NAME: string;
  BT_WALLET_HOTKEY: string;
  BT_WALLET_PATH: string;
  BT_WALLET_PASSWORD: string;
  TAOSTATS_API_KEY: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID: string;
  EMA_DRY_RUN: boolean;
  EMA_POT_TAO: number;
  EMA_MAX_POSITIONS: number;
  EMA_POSITION_SIZE_PCT: number;
  MAX_SLIPPAGE_PCT: number;
  EMA_STOP_LOSS_PCT: number;
  EMA_TAKE_PROFIT_PCT: number;
  EMA_TRAILING_STOP_PCT: number;
  EMA_MAX_HOLDING_HOURS: number;
  EMA_COOLDOWN_HOURS: number;
  EMA_PERIOD: number;
  EMA_FAST_PERIOD: number;
  EMA_CONFIRM_BARS: number;
  EMA_CANDLE_TIMEFRAME_HOURS: number;
  EMA_DRAWDOWN_BREAKER_PCT: number;
  EMA_DRAWDOWN_PAUSE_HOURS: number;
  SCAN_INTERVAL_MIN: number;
  LOG_LEVEL: string;
  [key: string]: string | number | boolean;
}

export interface SetupWizardProps {
  mode: "wizard" | "settings";
  initialValues?: Partial<ConfigValues>;
  onComplete?: () => void;
}

const DEFAULT_VALUES: ConfigValues = {
  BT_WALLET_NAME: "default",
  BT_WALLET_HOTKEY: "default",
  BT_WALLET_PATH: "~/.bittensor/wallets",
  BT_WALLET_PASSWORD: "",
  TAOSTATS_API_KEY: "",
  TELEGRAM_BOT_TOKEN: "",
  TELEGRAM_CHAT_ID: "",
  EMA_DRY_RUN: true,
  EMA_POT_TAO: 10.0,
  EMA_MAX_POSITIONS: 5,
  EMA_POSITION_SIZE_PCT: 0.2,
  MAX_SLIPPAGE_PCT: 5.0,
  EMA_STOP_LOSS_PCT: 8.0,
  EMA_TAKE_PROFIT_PCT: 20.0,
  EMA_TRAILING_STOP_PCT: 5.0,
  EMA_MAX_HOLDING_HOURS: 168,
  EMA_COOLDOWN_HOURS: 4.0,
  EMA_PERIOD: 18,
  EMA_FAST_PERIOD: 6,
  EMA_CONFIRM_BARS: 3,
  EMA_CANDLE_TIMEFRAME_HOURS: 4,
  EMA_DRAWDOWN_BREAKER_PCT: 15.0,
  EMA_DRAWDOWN_PAUSE_HOURS: 6.0,
  SCAN_INTERVAL_MIN: 15,
  LOG_LEVEL: "INFO",
};

const PRESETS = {
  conservative: {
    EMA_DRY_RUN: true,
    EMA_POT_TAO: 5.0,
    EMA_MAX_POSITIONS: 3,
    MAX_SLIPPAGE_PCT: 3.0,
    EMA_STOP_LOSS_PCT: 5.0,
    EMA_TAKE_PROFIT_PCT: 15.0,
    EMA_TRAILING_STOP_PCT: 3.0,
  },
  moderate: {
    EMA_DRY_RUN: false,
    EMA_POT_TAO: 10.0,
    EMA_MAX_POSITIONS: 5,
    MAX_SLIPPAGE_PCT: 5.0,
    EMA_STOP_LOSS_PCT: 8.0,
    EMA_TAKE_PROFIT_PCT: 20.0,
    EMA_TRAILING_STOP_PCT: 5.0,
  },
  aggressive: {
    EMA_DRY_RUN: false,
    EMA_POT_TAO: 20.0,
    EMA_MAX_POSITIONS: 8,
    MAX_SLIPPAGE_PCT: 8.0,
    EMA_STOP_LOSS_PCT: 12.0,
    EMA_TAKE_PROFIT_PCT: 30.0,
    EMA_TRAILING_STOP_PCT: 8.0,
  },
};

const STEP_LABELS = ["Wallet", "API Keys", "Telegram", "Trading", "Review"];

// ── Subcomponents ───────────────────────────────────────────────────────────

function PasswordInput({
  value,
  onChange,
  placeholder,
  id,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  id?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative">
      <input
        id={id}
        type={show ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-gray-800 border border-gray-700 text-gray-100 placeholder-gray-500 rounded-lg px-3 py-2 pr-10 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
      />
      <button
        type="button"
        onClick={() => setShow(!show)}
        className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-200 text-sm"
      >
        {show ? "Hide" : "Show"}
      </button>
    </div>
  );
}

function NumberInput({
  value,
  onChange,
  suffix,
  min,
  max,
  step,
  id,
}: {
  value: number;
  onChange: (v: number) => void;
  suffix?: string;
  min?: number;
  max?: number;
  step?: number;
  id?: string;
}) {
  return (
    <div className="relative">
      <input
        id={id}
        type="number"
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
        min={min}
        max={max}
        step={step || 1}
        className="w-full bg-gray-800 border border-gray-700 text-gray-100 placeholder-gray-500 rounded-lg px-3 py-2 pr-14 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
      />
      {suffix && (
        <span className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 text-sm pointer-events-none">
          {suffix}
        </span>
      )}
    </div>
  );
}

function HelpTip({ text }: { text: string }) {
  const [show, setShow] = useState(false);
  return (
    <span className="relative inline-block ml-1 align-middle">
      <button
        type="button"
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        onClick={() => setShow(!show)}
        className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-gray-700 text-gray-400 hover:bg-gray-600 hover:text-gray-200 text-[10px] font-bold leading-none cursor-help"
        aria-label="Help"
      >
        ?
      </button>
      {show && (
        <div className="absolute z-40 bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 px-3 py-2 rounded-lg bg-gray-800 border border-gray-700 text-xs text-gray-300 shadow-lg pointer-events-none">
          {text}
          <div className="absolute top-full left-1/2 -translate-x-1/2 -mt-px border-4 border-transparent border-t-gray-800" />
        </div>
      )}
    </span>
  );
}

function ExternalLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-indigo-400 hover:text-indigo-300 hover:underline"
    >
      {children}
    </a>
  );
}

function FieldLabel({ htmlFor, children, tip }: { htmlFor?: string; children: React.ReactNode; tip?: string }) {
  return (
    <label htmlFor={htmlFor} className="block text-sm font-medium text-gray-300 mb-1">
      {children}
      {tip && <HelpTip text={tip} />}
    </label>
  );
}

function StepIndicator({
  steps,
  current,
  onJump,
}: {
  steps: string[];
  current: number;
  onJump: (i: number) => void;
}) {
  return (
    <div className="flex items-center justify-center gap-0 mb-8">
      {steps.map((label, i) => {
        const isComplete = i < current;
        const isCurrent = i === current;
        return (
          <div key={label} className="flex items-center">
            <button
              type="button"
              onClick={() => (i <= current ? onJump(i) : undefined)}
              disabled={i > current}
              className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold transition-colors ${
                isComplete
                  ? "bg-emerald-500 text-white cursor-pointer"
                  : isCurrent
                  ? "bg-indigo-500 text-white"
                  : "bg-gray-700 text-gray-400 cursor-not-allowed"
              }`}
            >
              {isComplete ? "\u2713" : i + 1}
            </button>
            {i < steps.length - 1 && (
              <div
                className={`w-8 md:w-16 h-0.5 ${
                  i < current ? "bg-emerald-500" : "bg-gray-700"
                }`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Main Component ──────────────────────────────────────────────────────────

export default function SetupWizard({ mode, initialValues, onComplete }: SetupWizardProps) {
  const [step, setStep] = useState(0);
  const [values, setValues] = useState<ConfigValues>(() => {
    const merged = { ...DEFAULT_VALUES };
    if (initialValues) {
      for (const [k, v] of Object.entries(initialValues)) {
        if (v !== undefined) merged[k] = v;
      }
    }
    return merged;
  });
  // (wallet verification state is in validateResult / validating below)
  const [telegramResult, setTelegramResult] = useState<{ success: boolean; message?: string; error?: string } | null>(null);
  const [telegramTesting, setTelegramTesting] = useState(false);
  const [taostatsResult, setTaostatsResult] = useState<{ success: boolean; message?: string; error?: string } | null>(null);
  const [taostatsTesting, setTaostatsTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<{ success: boolean; error?: string; errors?: Record<string, string> } | null>(null);
  const [selectedPreset, setSelectedPreset] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  // Safety gate modal state
  const [showSafetyGate, setShowSafetyGate] = useState(false);
  const [safetyGateChecks, setSafetyGateChecks] = useState<Record<string, { ok: boolean; detail: string; optional?: boolean }> | null>(null);
  const [safetyGateLoading, setSafetyGateLoading] = useState(false);
  const [safetyGateConfirmed, setSafetyGateConfirmed] = useState(false);
  const [safetyGateCanGoLive, setSafetyGateCanGoLive] = useState(false);
  const [pendingPreset, setPendingPreset] = useState<string | null>(null);

  // Wallet step phases: detect → choice → create/existing
  const [walletPhase, setWalletPhase] = useState<"detecting" | "choice" | "create" | "existing">("detecting");
  const [walletDetectDone, setWalletDetectDone] = useState(false);
  const [walletJustCreated, setWalletJustCreated] = useState(false);
  const [walletCreatedBanner, setWalletCreatedBanner] = useState(false);

  // Validation result (for the enhanced validate endpoint)
  type ValidateResult = {
    success: boolean;
    checks?: {
      coldkey_exists: boolean;
      hotkey_exists: boolean;
      coldkey_unlockable: boolean;
      coldkey_ss58: string | null;
      balance_tao: number | null;
      balance_sufficient: boolean;
    };
    warnings?: string[];
    error?: string;
  };
  const [validateResult, setValidateResult] = useState<ValidateResult | null>(null);
  const [validating, setValidating] = useState(false);

  // Auto-detect wallet on mount
  useEffect(() => {
    if (walletDetectDone) return;
    (async () => {
      try {
        const params = new URLSearchParams({
          wallet_name: values.BT_WALLET_NAME || "default",
          hotkey: values.BT_WALLET_HOTKEY || "default",
          wallet_path: values.BT_WALLET_PATH || "~/.bittensor/wallets",
        });
        const resp = await fetch(`${API}/api/config/wallet/detect?${params}`);
        const data = await resp.json();
        setWalletDetectDone(true);
        if (data.coldkey_exists) {
          // Pre-populate SS58 if available
          setWalletPhase("existing");
        } else {
          setWalletPhase("choice");
        }
      } catch {
        setWalletDetectDone(true);
        setWalletPhase("choice");
      }
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const set = useCallback(
    (field: string, value: string | number | boolean) => {
      setValues((prev) => ({ ...prev, [field]: value }));
      // Clear field error on change
      setFieldErrors((prev) => {
        if (prev[field]) {
          const next = { ...prev };
          delete next[field];
          return next;
        }
        return prev;
      });
    },
    []
  );

  const positionSize = useMemo(
    () => (values.EMA_POT_TAO / values.EMA_MAX_POSITIONS).toFixed(2),
    [values.EMA_POT_TAO, values.EMA_MAX_POSITIONS]
  );

  // Step 1 validation: wallet name & hotkey must be non-empty
  const step1Valid =
    values.BT_WALLET_NAME.trim() !== "" &&
    values.BT_WALLET_HOTKEY.trim() !== "" &&
    values.BT_WALLET_PATH.trim() !== "";

  const canNext = step === 0 ? step1Valid : true;

  // ── API calls ──

  const verifyWallet = async () => {
    setValidating(true);
    setValidateResult(null);
    try {
      const resp = await fetch(`${API}/api/config/wallet/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          wallet_name: values.BT_WALLET_NAME,
          hotkey: values.BT_WALLET_HOTKEY,
          wallet_path: values.BT_WALLET_PATH,
          password: values.BT_WALLET_PASSWORD,
        }),
      });
      setValidateResult(await resp.json());
    } catch {
      setValidateResult({ success: false, error: "Cannot connect to backend" });
    }
    setValidating(false);
  };

  const handleWalletCreated = (wallet: {
    wallet_name: string;
    hotkey: string;
    wallet_path: string;
    password: string;
    coldkey_ss58: string;
  }) => {
    set("BT_WALLET_NAME", wallet.wallet_name);
    set("BT_WALLET_HOTKEY", wallet.hotkey);
    set("BT_WALLET_PATH", wallet.wallet_path);
    if (wallet.password) set("BT_WALLET_PASSWORD", wallet.password);
    setWalletJustCreated(true);
    setWalletCreatedBanner(true);
    setValidateResult({
      success: true,
      checks: {
        coldkey_exists: true,
        hotkey_exists: true,
        coldkey_unlockable: true,
        coldkey_ss58: wallet.coldkey_ss58,
        balance_tao: 0,
        balance_sufficient: false,
      },
      warnings: ["Balance is 0 TAO — fund your wallet before trading"],
    });
    setWalletPhase("existing");
  };

  const testTelegram = async () => {
    setTelegramTesting(true);
    setTelegramResult(null);
    try {
      const resp = await fetch(`${API}/api/config/test-telegram`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_token: values.TELEGRAM_BOT_TOKEN,
          chat_id: values.TELEGRAM_CHAT_ID,
        }),
      });
      setTelegramResult(await resp.json());
    } catch {
      setTelegramResult({ success: false, error: "Cannot connect to backend" });
    }
    setTelegramTesting(false);
  };

  const testTaostats = async () => {
    setTaostatsTesting(true);
    setTaostatsResult(null);
    try {
      const resp = await fetch(`${API}/api/config/test-taostats`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: values.TAOSTATS_API_KEY }),
      });
      setTaostatsResult(await resp.json());
    } catch {
      setTaostatsResult({ success: false, error: "Cannot connect to backend" });
    }
    setTaostatsTesting(false);
  };

  const saveConfig = async () => {
    setSaving(true);
    setSaveResult(null);
    setFieldErrors({});

    // Derive position size from max positions for wizard mode
    const submitValues: Record<string, unknown> = { ...values };
    if (mode === "wizard") {
      submitValues.EMA_POSITION_SIZE_PCT = parseFloat(
        (1 / values.EMA_MAX_POSITIONS).toFixed(4)
      );
    }

    // Remove masked password values that weren't changed
    for (const key of Object.keys(submitValues)) {
      if (submitValues[key] === "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022") {
        delete submitValues[key];
      }
    }

    try {
      const resp = await fetch(`${API}/api/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: submitValues, restart: true }),
      });
      const data = await resp.json();
      if (data.success) {
        setSaveResult({ success: true });
        if (onComplete) {
          setTimeout(onComplete, 2000);
        }
      } else {
        setSaveResult({ success: false, errors: data.errors, error: data.error });
        if (data.errors) {
          setFieldErrors(data.errors);
        }
      }
    } catch {
      setSaveResult({ success: false, error: "Cannot connect to backend" });
    }
    setSaving(false);
  };

  const openSafetyGate = async (presetName?: string) => {
    setPendingPreset(presetName || null);
    setShowSafetyGate(true);
    setSafetyGateConfirmed(false);
    setSafetyGateLoading(true);
    setSafetyGateChecks(null);
    setSafetyGateCanGoLive(false);

    try {
      const resp = await fetch(`${API}/api/config/go-live`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          wallet_name: values.BT_WALLET_NAME,
          hotkey: values.BT_WALLET_HOTKEY,
          wallet_path: values.BT_WALLET_PATH,
          password: values.BT_WALLET_PASSWORD,
          pot_tao: values.EMA_POT_TAO,
        }),
      });
      const data = await resp.json();
      setSafetyGateChecks(data.checks);
      setSafetyGateCanGoLive(data.can_go_live);
    } catch {
      setSafetyGateChecks({
        wallet_configured: { ok: false, detail: "Cannot reach backend" },
      });
    }
    setSafetyGateLoading(false);
  };

  const confirmSafetyGate = () => {
    setShowSafetyGate(false);
    if (pendingPreset) {
      const preset = PRESETS[pendingPreset as keyof typeof PRESETS];
      if (preset) {
        setSelectedPreset(pendingPreset);
        setValues((prev) => ({ ...prev, ...preset }));
      }
    } else {
      set("EMA_DRY_RUN", false);
    }
    setPendingPreset(null);
    setSafetyGateConfirmed(false);
  };

  const cancelSafetyGate = () => {
    setShowSafetyGate(false);
    setPendingPreset(null);
    setSafetyGateConfirmed(false);
    // Reset preset selection to conservative if user was clicking moderate/aggressive
    if (selectedPreset === "moderate" || selectedPreset === "aggressive") {
      setSelectedPreset("conservative");
      setValues((prev) => ({ ...prev, ...PRESETS.conservative }));
    }
  };

  const applyPreset = (name: string) => {
    const preset = PRESETS[name as keyof typeof PRESETS];
    if (!preset) return;

    // If the preset enables live trading, show the safety gate
    if (!preset.EMA_DRY_RUN) {
      openSafetyGate(name);
      return;
    }

    setSelectedPreset(name);
    setValues((prev) => ({ ...prev, ...preset }));
  };

  const handleDryRunToggle = () => {
    if (values.EMA_DRY_RUN) {
      // Turning OFF paper mode = enabling live trading → show safety gate
      openSafetyGate();
    } else {
      // Turning ON paper mode = safe, no gate needed
      set("EMA_DRY_RUN", true);
    }
  };

  // ── Render step content ──

  const renderValidationChecklist = () => {
    if (!validateResult) return null;
    if (!validateResult.success && validateResult.error) {
      return (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-red-400 text-sm">
          {validateResult.error}
        </div>
      );
    }
    const c = validateResult.checks;
    if (!c) return null;
    return (
      <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-4 space-y-2">
        <h4 className="text-sm font-semibold text-gray-300 mb-2">Verification Results</h4>
        <div className="flex items-center gap-2 text-sm">
          <span className={c.coldkey_exists ? "text-emerald-400" : "text-red-400"}>
            {c.coldkey_exists ? "\u2713" : "\u2717"}
          </span>
          <span className="text-gray-300">
            {c.coldkey_exists ? "Coldkey found" : "Coldkey not found"}
          </span>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <span className={c.hotkey_exists ? "text-emerald-400" : "text-red-400"}>
            {c.hotkey_exists ? "\u2713" : "\u2717"}
          </span>
          <span className="text-gray-300">
            {c.hotkey_exists ? "Hotkey found" : `Hotkey not found`}
          </span>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <span className={c.coldkey_unlockable ? "text-emerald-400" : "text-red-400"}>
            {c.coldkey_unlockable ? "\u2713" : "\u2717"}
          </span>
          <span className="text-gray-300">
            {c.coldkey_unlockable ? "Coldkey unlocked successfully" : "Could not unlock coldkey"}
          </span>
        </div>
        {c.coldkey_ss58 && (
          <div className="text-sm text-gray-400">
            Address: <span className="font-mono text-gray-300">{c.coldkey_ss58}</span>
          </div>
        )}
        <div className="flex items-center gap-2 text-sm">
          {c.balance_tao !== null ? (
            <>
              <span className={c.balance_tao === 0 ? "text-amber-400" : "text-emerald-400"}>
                {c.balance_tao === 0 ? "\u26A0" : "\u2713"}
              </span>
              <span className="text-gray-300">Balance: {c.balance_tao} TAO</span>
            </>
          ) : (
            <>
              <span className="text-amber-400">{"\u26A0"}</span>
              <span className="text-gray-300">Balance: unavailable</span>
            </>
          )}
        </div>
        {validateResult.warnings && validateResult.warnings.length > 0 && (
          <div className="mt-2 space-y-1">
            {validateResult.warnings.map((w, i) => (
              <div key={i} className="flex items-center gap-2 text-sm text-amber-400">
                <span>{"\u26A0"}</span> {w}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };

  const renderStep1 = () => {
    // Phase A: Detecting
    if (walletPhase === "detecting") {
      return (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold text-white mb-1">Connect Your Wallet</h2>
          <div className="flex items-center gap-3 py-8 justify-center text-gray-400">
            <span className="h-5 w-5 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin" />
            <span>Checking for existing wallet...</span>
          </div>
        </div>
      );
    }

    // Phase: Choice (no wallet found)
    if (walletPhase === "choice") {
      return (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold text-white mb-1">Connect Your Wallet</h2>
          <p className="text-gray-400 text-sm">
            No wallet found at {values.BT_WALLET_PATH}/{values.BT_WALLET_NAME}
          </p>

          <div className="grid grid-cols-2 gap-4">
            <button
              type="button"
              onClick={() => setWalletPhase("create")}
              className="bg-gray-800 hover:bg-gray-750 border border-gray-700 hover:border-indigo-500 rounded-xl p-6 text-left transition-colors cursor-pointer"
            >
              <div className="text-lg mb-1">Create New</div>
              <div className="text-sm text-gray-400">
                Create a Bittensor wallet right here
              </div>
            </button>
            <button
              type="button"
              onClick={() => setWalletPhase("existing")}
              className="bg-gray-800 hover:bg-gray-750 border border-gray-700 hover:border-indigo-500 rounded-xl p-6 text-left transition-colors cursor-pointer"
            >
              <div className="text-lg mb-1">I Have a Wallet</div>
              <div className="text-sm text-gray-400">
                Enter your existing wallet credentials
              </div>
            </button>
          </div>

          <p className="text-gray-500 text-xs">
            New to Bittensor?{" "}
            <a
              href="https://docs.bittensor.com/getting-started"
              target="_blank"
              rel="noopener noreferrer"
              className="text-indigo-400 hover:underline"
            >
              Learn more
            </a>
          </p>
        </div>
      );
    }

    // Phase C: Create wallet
    if (walletPhase === "create") {
      return (
        <WalletCreator
          onCreated={handleWalletCreated}
          onCancel={() => setWalletPhase("choice")}
        />
      );
    }

    // Phase B: Existing wallet (validate)
    return (
      <div className="space-y-4">
        <div>
          <h2 className="text-xl font-semibold text-white mb-1">Connect Your Wallet</h2>
          <p className="text-gray-400 text-sm">
            Enter the Bittensor wallet credentials stored on this machine.
          </p>
        </div>

        {walletCreatedBanner && (
          <div className="bg-emerald-900/20 border border-emerald-800 rounded-lg p-3 text-emerald-400 text-sm font-medium">
            Wallet created and verified
          </div>
        )}

        <div>
          <FieldLabel htmlFor="wallet-name" tip="The directory name of your wallet under the wallet path. Created with 'btcli wallet create' or via the wizard.">Wallet Name</FieldLabel>
          <input
            id="wallet-name"
            type="text"
            value={values.BT_WALLET_NAME}
            onChange={(e) => set("BT_WALLET_NAME", e.target.value)}
            placeholder="default"
            className="w-full bg-gray-800 border border-gray-700 text-gray-100 placeholder-gray-500 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
          />
        </div>

        <div>
          <FieldLabel htmlFor="hotkey-name" tip="The hotkey used for signing transactions. Each wallet can have multiple hotkeys. 'default' works for most setups.">Hotkey Name</FieldLabel>
          <input
            id="hotkey-name"
            type="text"
            value={values.BT_WALLET_HOTKEY}
            onChange={(e) => set("BT_WALLET_HOTKEY", e.target.value)}
            placeholder="default"
            className="w-full bg-gray-800 border border-gray-700 text-gray-100 placeholder-gray-500 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
          />
        </div>

        <div>
          <FieldLabel htmlFor="wallet-path" tip="Directory where Bittensor wallets are stored. The default (~/.bittensor/wallets) is standard for btcli.">Wallet Path</FieldLabel>
          <input
            id="wallet-path"
            type="text"
            value={values.BT_WALLET_PATH}
            onChange={(e) => set("BT_WALLET_PATH", e.target.value)}
            placeholder="~/.bittensor/wallets"
            className="w-full bg-gray-800 border border-gray-700 text-gray-100 placeholder-gray-500 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
          />
        </div>

        <div>
          <FieldLabel htmlFor="wallet-password" tip="If your coldkey is encrypted, enter the password here. The bot needs it to sign transactions. Leave blank for unencrypted keys.">Coldkey Password</FieldLabel>
          <PasswordInput
            id="wallet-password"
            value={values.BT_WALLET_PASSWORD}
            onChange={(v) => set("BT_WALLET_PASSWORD", v)}
            placeholder="Leave empty if unencrypted"
          />
        </div>

        <div>
          <button
            type="button"
            onClick={verifyWallet}
            disabled={validating || !step1Valid}
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {validating ? "Verifying..." : "Verify Wallet"}
          </button>
        </div>

        {renderValidationChecklist()}

        {/* Funding guidance after creation */}
        {walletJustCreated && validateResult?.checks?.balance_tao === 0 && (
          <div className="bg-sky-900/20 border border-sky-800 rounded-lg p-4 space-y-2">
            <h4 className="text-sm font-semibold text-sky-300">Fund Your Wallet</h4>
            <p className="text-sm text-gray-400">
              Your wallet is empty. To start trading, send TAO to your coldkey address:
            </p>
            {validateResult.checks?.coldkey_ss58 && (
              <div className="bg-gray-800 border border-gray-700 rounded-lg p-2 flex items-center justify-between gap-2">
                <code className="text-xs text-gray-200 font-mono break-all">
                  {validateResult.checks.coldkey_ss58}
                </code>
                <button
                  type="button"
                  onClick={() => navigator.clipboard.writeText(validateResult.checks!.coldkey_ss58!)}
                  className="bg-gray-700 hover:bg-gray-600 text-xs px-2 py-1 rounded text-gray-200 shrink-0"
                >
                  Copy
                </button>
              </div>
            )}
            <ul className="text-xs text-gray-500 space-y-0.5">
              <li>From an exchange (Binance, MEXC, Gate.io, etc.)</li>
              <li>From another Bittensor wallet</li>
            </ul>
            <p className="text-xs text-gray-500">
              You can continue setup now and fund later. The bot won&apos;t trade until your balance
              covers at least one position.
            </p>
          </div>
        )}

        {!walletCreatedBanner && !validateResult && (
          <p className="text-xs text-gray-500">
            Wallet not verified — you can verify later from Settings.
          </p>
        )}
      </div>
    );
  };

  const renderStep2 = () => (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-white mb-1">API Keys (Optional)</h2>
      </div>

      <div>
        <FieldLabel htmlFor="taostats-key" tip="Taostats provides subnet pool data and price history. The free tier (no key) allows 30 requests/min, which is enough for most setups.">Taostats API Key</FieldLabel>
        <p className="text-gray-500 text-xs mb-2">
          Provides subnet price data and pool metrics. Without a key, the free tier is used
          (30 requests/min).{" "}
          <ExternalLink href="https://taostats.io">Get a key at taostats.io</ExternalLink>
        </p>
        <PasswordInput
          id="taostats-key"
          value={values.TAOSTATS_API_KEY}
          onChange={(v) => set("TAOSTATS_API_KEY", v)}
          placeholder="Optional"
        />
        <button
          type="button"
          onClick={testTaostats}
          disabled={taostatsTesting}
          className="mt-2 px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium disabled:opacity-50 transition-colors"
        >
          {taostatsTesting ? "Testing..." : "Test Connection"}
        </button>
        {taostatsResult && (
          <div className={`mt-2 text-sm ${taostatsResult.success ? "text-emerald-400" : "text-red-400"}`}>
            {taostatsResult.success ? `\u2713 ${taostatsResult.message}` : taostatsResult.error}
          </div>
        )}
      </div>
    </div>
  );

  const renderStep3 = () => (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold text-white mb-1">Telegram Alerts (Optional)</h2>
        <p className="text-gray-400 text-sm">
          Receive trade alerts, position updates, and daily summaries via Telegram. You can set
          this up later from the Settings page.
        </p>
      </div>

      <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-3 text-xs text-gray-400 space-y-1">
        <p>1. Message <ExternalLink href="https://t.me/BotFather">@BotFather</ExternalLink> on Telegram and send <code className="bg-gray-700 px-1 rounded">/newbot</code>.</p>
        <p>2. Copy the bot token it gives you.</p>
        <p>3. Send any message to your new bot, then message <ExternalLink href="https://t.me/userinfobot">@userinfobot</ExternalLink> to get your chat ID.</p>
      </div>

      <div>
        <FieldLabel htmlFor="tg-token" tip="The token @BotFather gives you after creating a bot. Looks like 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11.">Bot Token</FieldLabel>
        <PasswordInput
          id="tg-token"
          value={values.TELEGRAM_BOT_TOKEN}
          onChange={(v) => set("TELEGRAM_BOT_TOKEN", v)}
          placeholder="123456:ABC-DEF..."
        />
      </div>

      <div>
        <FieldLabel htmlFor="tg-chat" tip="Your numeric Telegram user ID. Send any message to @userinfobot on Telegram to find it.">Chat ID</FieldLabel>
        <input
          id="tg-chat"
          type="text"
          value={values.TELEGRAM_CHAT_ID}
          onChange={(e) => set("TELEGRAM_CHAT_ID", e.target.value)}
          placeholder="12345678"
          className="w-full bg-gray-800 border border-gray-700 text-gray-100 placeholder-gray-500 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
        />
      </div>

      <div>
        <button
          type="button"
          onClick={testTelegram}
          disabled={telegramTesting || !values.TELEGRAM_BOT_TOKEN || !values.TELEGRAM_CHAT_ID}
          className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {telegramTesting ? "Sending..." : "Send Test Message"}
        </button>
        {telegramResult && (
          <div className={`mt-2 text-sm ${telegramResult.success ? "text-emerald-400" : "text-red-400"}`}>
            {telegramResult.success ? "\u2713 Message sent! Check your Telegram." : telegramResult.error}
          </div>
        )}
      </div>
    </div>
  );

  const renderStep4 = () => (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-white mb-1">Trading Configuration</h2>
      </div>

      {/* Presets */}
      <div className="grid grid-cols-3 gap-3">
        {(Object.keys(PRESETS) as Array<keyof typeof PRESETS>).map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => applyPreset(name)}
            className={`p-3 rounded-xl border text-left transition-colors ${
              selectedPreset === name
                ? "border-indigo-500 bg-indigo-500/10"
                : "border-gray-700 bg-gray-800 hover:border-gray-600"
            }`}
          >
            <div className="text-sm font-semibold text-white capitalize">{name}</div>
            <div className="text-xs text-gray-400 mt-1">
              {PRESETS[name].EMA_DRY_RUN ? "Paper" : "Live"} | {PRESETS[name].EMA_POT_TAO} TAO |{" "}
              {PRESETS[name].EMA_MAX_POSITIONS} slots
            </div>
          </button>
        ))}
      </div>

      {/* Dry run toggle */}
      <div>
        <div className="flex items-center gap-3">
          <FieldLabel>Paper Trading Mode</FieldLabel>
          <button
            type="button"
            onClick={handleDryRunToggle}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
              values.EMA_DRY_RUN ? "bg-indigo-500" : "bg-gray-600"
            }`}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                values.EMA_DRY_RUN ? "translate-x-6" : "translate-x-1"
              }`}
            />
          </button>
          <span className="text-sm text-gray-400">{values.EMA_DRY_RUN ? "ON" : "OFF"}</span>
        </div>
        {!values.EMA_DRY_RUN && (
          <div className="mt-2 bg-red-900/20 border border-red-800 rounded-lg p-3 text-red-400 text-sm font-medium">
            Live trading is enabled. The bot will execute real trades with your TAO.
          </div>
        )}
      </div>

      {/* Trading fields 2-column grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <FieldLabel htmlFor="pot-tao" tip="Total TAO allocated for trading. Divided equally among max positions. Independent of your wallet balance.">Trading Pot</FieldLabel>
          <NumberInput
            id="pot-tao"
            value={values.EMA_POT_TAO}
            onChange={(v) => set("EMA_POT_TAO", v)}
            suffix="TAO"
            min={0.1}
            step={0.5}
          />
        </div>
        <div>
          <FieldLabel htmlFor="max-pos" tip="Maximum number of subnet positions open at once. Each gets an equal share of the pot (pot / positions).">Max Open Positions</FieldLabel>
          <NumberInput
            id="max-pos"
            value={values.EMA_MAX_POSITIONS}
            onChange={(v) => set("EMA_MAX_POSITIONS", Math.max(1, Math.min(20, Math.round(v))))}
            min={1}
            max={20}
            step={1}
          />
        </div>
        <div>
          <FieldLabel htmlFor="slippage" tip="Maximum price slippage allowed on entry. If the actual price differs by more than this %, the trade is rejected. Higher = more fills but worse prices.">Max Slippage</FieldLabel>
          <NumberInput
            id="slippage"
            value={values.MAX_SLIPPAGE_PCT}
            onChange={(v) => set("MAX_SLIPPAGE_PCT", v)}
            suffix="%"
            min={0.1}
            max={50}
            step={0.5}
          />
        </div>
        <div>
          <FieldLabel htmlFor="stop-loss" tip="Exit if a position drops this % below its entry price. Protects against large losses on bad trades.">Stop Loss</FieldLabel>
          <NumberInput
            id="stop-loss"
            value={values.EMA_STOP_LOSS_PCT}
            onChange={(v) => set("EMA_STOP_LOSS_PCT", v)}
            suffix="%"
            min={1}
            max={50}
            step={0.5}
          />
        </div>
        <div>
          <FieldLabel htmlFor="take-profit" tip="Exit when a position gains this % above entry price. Locks in profit at your target level.">Take Profit</FieldLabel>
          <NumberInput
            id="take-profit"
            value={values.EMA_TAKE_PROFIT_PCT}
            onChange={(v) => set("EMA_TAKE_PROFIT_PCT", v)}
            suffix="%"
            min={1}
            max={100}
            step={1}
          />
        </div>
        <div>
          <FieldLabel htmlFor="trailing" tip="Once a position is profitable, exit if it drops this % from its peak. Lets winners run while protecting gains.">Trailing Stop</FieldLabel>
          <NumberInput
            id="trailing"
            value={values.EMA_TRAILING_STOP_PCT}
            onChange={(v) => set("EMA_TRAILING_STOP_PCT", v)}
            suffix="%"
            min={1}
            max={50}
            step={0.5}
          />
        </div>
        <div>
          <FieldLabel htmlFor="max-hold" tip="Automatically exit a position after this many hours regardless of P&L. Prevents capital from being stuck in stale trades.">Max Holding Time</FieldLabel>
          <NumberInput
            id="max-hold"
            value={values.EMA_MAX_HOLDING_HOURS}
            onChange={(v) => set("EMA_MAX_HOLDING_HOURS", Math.round(v))}
            suffix="hours"
            min={1}
            max={720}
            step={1}
          />
        </div>
        <div>
          <FieldLabel htmlFor="cooldown" tip="Wait this long before re-entering the same subnet after an exit. Prevents immediate re-entry into a failing position.">Re-entry Cooldown</FieldLabel>
          <NumberInput
            id="cooldown"
            value={values.EMA_COOLDOWN_HOURS}
            onChange={(v) => set("EMA_COOLDOWN_HOURS", v)}
            suffix="hours"
            min={0}
            max={48}
            step={0.5}
          />
        </div>
      </div>

      {/* Position size helper */}
      <div className="text-sm text-gray-400 bg-gray-800/50 border border-gray-700 rounded-lg p-3">
        Each position: {positionSize} TAO (
        {((1 / values.EMA_MAX_POSITIONS) * 100).toFixed(0)}% of pot)
      </div>
    </div>
  );

  const renderStep5 = () => {
    const sections = [
      {
        title: "Wallet",
        step: 0,
        items: [
          ["Wallet Name", values.BT_WALLET_NAME],
          ["Hotkey", values.BT_WALLET_HOTKEY],
          ["Path", values.BT_WALLET_PATH],
          ["Password", values.BT_WALLET_PASSWORD ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" : "Not set"],
        ],
      },
      {
        title: "API Keys",
        step: 1,
        items: [
          ["Taostats", values.TAOSTATS_API_KEY ? "Set" : "Not set"],
        ],
      },
      {
        title: "Telegram",
        step: 2,
        items: [
          [
            "Status",
            values.TELEGRAM_BOT_TOKEN && values.TELEGRAM_CHAT_ID ? "Configured" : "Not configured",
          ],
        ],
      },
      {
        title: "Trading",
        step: 3,
        items: [
          ["Mode", values.EMA_DRY_RUN ? "Paper Trading" : "LIVE"],
          ["Pot", `${values.EMA_POT_TAO} TAO`],
          ["Max Positions", String(values.EMA_MAX_POSITIONS)],
          ["Stop Loss", `${values.EMA_STOP_LOSS_PCT}%`],
          ["Take Profit", `${values.EMA_TAKE_PROFIT_PCT}%`],
          ["Slippage", `${values.MAX_SLIPPAGE_PCT}%`],
        ],
      },
    ];

    return (
      <div className="space-y-4">
        <div>
          <h2 className="text-xl font-semibold text-white mb-1">Review Your Configuration</h2>
        </div>

        {sections.map((s) => (
          <div key={s.title} className="bg-gray-800/50 border border-gray-700 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-semibold text-gray-300">{s.title}</h3>
              <button
                type="button"
                onClick={() => setStep(s.step)}
                className="text-indigo-400 hover:text-indigo-300 text-xs"
              >
                Edit
              </button>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
              {s.items.map(([label, val]) => (
                <div key={label} className="contents">
                  <span className="text-gray-500">{label}</span>
                  <span className={`text-gray-200 ${val === "LIVE" ? "text-red-400 font-semibold" : ""}`}>
                    {val}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}

        <button
          type="button"
          onClick={saveConfig}
          disabled={saving}
          className="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl font-semibold text-sm disabled:opacity-50 transition-colors"
        >
          {saving ? (
            <span className="flex items-center justify-center gap-2">
              <span className="h-4 w-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              Saving...
            </span>
          ) : (
            "Save & Start"
          )}
        </button>

        {saveResult && (
          <div
            className={`text-sm rounded-lg p-3 ${
              saveResult.success
                ? "bg-emerald-900/20 border border-emerald-800 text-emerald-400"
                : "bg-red-900/20 border border-red-800 text-red-400"
            }`}
          >
            {saveResult.success
              ? "Configuration saved! Redirecting..."
              : saveResult.error || "Validation errors — check the highlighted fields above."}
            {saveResult.errors && (
              <ul className="mt-2 space-y-1">
                {Object.entries(saveResult.errors).map(([k, v]) => (
                  <li key={k}>
                    <strong>{k}</strong>: {v}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    );
  };

  const stepRenderers = [renderStep1, renderStep2, renderStep3, renderStep4, renderStep5];

  // Safety gate modal — shared between wizard and settings
  const renderSafetyGateModal = () => {
    if (!showSafetyGate) return null;

    const checkOrder = [
      { key: "wallet_configured", label: "Wallet configured" },
      { key: "wallet_unlockable", label: "Wallet unlockable" },
      { key: "balance_sufficient", label: "Balance sufficient" },
      { key: "rpc_connected", label: "RPC connected" },
      { key: "taostats_reachable", label: "Taostats reachable" },
      { key: "telegram_configured", label: "Telegram configured" },
    ];

    return (
      <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center">
        <div className="bg-gray-900 border border-gray-700 rounded-xl max-w-lg w-full mx-4 p-6">
          <h3 className="text-lg font-semibold text-amber-400 flex items-center gap-2 mb-4">
            <span>{"\u26A0"}</span> Enable Live Trading?
          </h3>

          <p className="text-sm text-gray-300 mb-4">
            You are about to enable LIVE trading. The bot will execute real trades with your TAO.
            This cannot be undone by switching back to paper mode — any trades placed while live
            will remain on-chain.
          </p>

          <div className="border border-gray-700 rounded-lg p-4 mb-4">
            <h4 className="text-sm font-semibold text-gray-300 mb-3">Pre-flight Checks</h4>
            {safetyGateLoading ? (
              <div className="flex items-center gap-3 py-4 justify-center text-gray-400">
                <span className="h-5 w-5 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin" />
                <span className="text-sm">Running pre-flight checks...</span>
              </div>
            ) : (
              <div className="space-y-2">
                {checkOrder.map(({ key, label }) => {
                  const check = safetyGateChecks?.[key];
                  if (!check) return null;
                  const icon = check.ok ? "\u2713" : check.optional ? "\u2014" : "\u2717";
                  const color = check.ok
                    ? "text-emerald-400"
                    : check.optional
                    ? "text-gray-500"
                    : "text-red-400";
                  return (
                    <div key={key} className="flex items-start gap-2 text-sm">
                      <span className={color}>{icon}</span>
                      <span className="text-gray-300">{label}</span>
                      <span className="text-gray-500 ml-auto text-right text-xs truncate max-w-[200px]">
                        ({check.detail})
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <label className="flex items-center gap-2 mb-5 cursor-pointer">
            <input
              type="checkbox"
              checked={safetyGateConfirmed}
              onChange={(e) => setSafetyGateConfirmed(e.target.checked)}
              className="accent-indigo-500 w-4 h-4"
            />
            <span className="text-sm text-gray-300">
              I understand that live trading uses real funds
            </span>
          </label>

          <div className="flex gap-3 justify-end">
            <button
              type="button"
              onClick={cancelSafetyGate}
              className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={confirmSafetyGate}
              disabled={!safetyGateCanGoLive || !safetyGateConfirmed || safetyGateLoading}
              className="px-4 py-2 bg-red-700 hover:bg-red-600 text-white rounded-lg text-sm font-medium disabled:opacity-40 transition-colors"
            >
              Enable Live Trading
            </button>
          </div>
        </div>
      </div>
    );
  };

  if (mode === "wizard") {
    return (
      <>
        {renderSafetyGateModal()}
        <div className="min-h-screen bg-gray-950 flex flex-col items-center px-4 py-8">
          <div className="w-full max-w-2xl">
            {/* Header */}
            <div className="mb-6 text-left">
              <span className="font-bold text-indigo-400 text-lg">SubnetTrader</span>
            </div>

            {/* Step indicator */}
            <StepIndicator steps={STEP_LABELS} current={step} onJump={setStep} />

            {/* Step content */}
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
              {stepRenderers[step]()}
            </div>

            {/* Navigation buttons */}
            {step < 4 && (
              <div className="flex justify-between mt-4">
                <button
                  type="button"
                  onClick={() => setStep(Math.max(0, step - 1))}
                  disabled={step === 0}
                  className="px-5 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium disabled:opacity-30 transition-colors"
                >
                  Back
                </button>
                <button
                  type="button"
                  onClick={() => setStep(step + 1)}
                  disabled={!canNext}
                  className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  Next
                </button>
              </div>
            )}
            {step === 4 && step > 0 && (
              <div className="flex justify-start mt-4">
                <button
                  type="button"
                  onClick={() => setStep(step - 1)}
                  className="px-5 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium transition-colors"
                >
                  Back
                </button>
              </div>
            )}
          </div>
        </div>
      </>
    );
  }

  // Settings mode — not used directly (settings page has its own layout)
  return null;
}
