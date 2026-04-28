"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import WalletCreator from "../../components/WalletCreator";

const API =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8081`
    : "http://localhost:8081");

interface ConfigValues {
  [key: string]: string | number | boolean;
}

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
        className="w-full bg-gray-800 border border-gray-700 text-gray-100 rounded-lg px-3 py-2 pr-14 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
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

function FieldLabel({ htmlFor, children, tip }: { htmlFor?: string; children: React.ReactNode; tip?: string }) {
  return (
    <label htmlFor={htmlFor} className="block text-sm font-medium text-gray-300 mb-1">
      {children}
      {tip && <HelpTip text={tip} />}
    </label>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
      <h3 className="text-sm font-semibold text-gray-300 border-b border-gray-800 pb-2">
        {title}
      </h3>
      {children}
    </div>
  );
}

function CollapsibleSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between text-sm font-semibold text-gray-300"
      >
        <span>{title}</span>
        <span className="text-gray-500">{open ? "\u25B2" : "\u25BC"}</span>
      </button>
      {open && <div className="mt-4 space-y-4 border-t border-gray-800 pt-4">{children}</div>}
    </div>
  );
}

// ── Main Settings Page ──────────────────────────────────────────────────────

export default function SettingsPage() {
  const [values, setValues] = useState<ConfigValues>({});
  const [initialValues, setInitialValues] = useState<ConfigValues>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // Enhanced wallet validation
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
  const [walletResult, setWalletResult] = useState<ValidateResult | null>(null);
  const [walletTesting, setWalletTesting] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [mnemonicPhaseActive, setMnemonicPhaseActive] = useState(false);
  const [telegramResult, setTelegramResult] = useState<{
    success: boolean;
    message?: string;
    error?: string;
  } | null>(null);
  const [telegramTesting, setTelegramTesting] = useState(false);
  const [taostatsResult, setTaostatsResult] = useState<{
    success: boolean;
    message?: string;
    error?: string;
  } | null>(null);
  const [taostatsTesting, setTaostatsTesting] = useState(false);

  // Safety gate modal state
  const [showSafetyGate, setShowSafetyGate] = useState(false);
  const [safetyGateChecks, setSafetyGateChecks] = useState<Record<string, { ok: boolean; detail: string; optional?: boolean }> | null>(null);
  const [safetyGateLoading, setSafetyGateLoading] = useState(false);
  const [safetyGateConfirmed, setSafetyGateConfirmed] = useState(false);
  const [safetyGateCanGoLive, setSafetyGateCanGoLive] = useState(false);

  const loadConfig = useCallback(async () => {
    try {
      const resp = await fetch(`${API}/api/config`);
      const data = await resp.json();
      setValues(data);
      setInitialValues(data);
    } catch {
      setToast({ type: "error", text: "Cannot connect to backend" });
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  // Dirty check
  const isDirty = useMemo(() => {
    return Object.keys(values).some((k) => values[k] !== initialValues[k]);
  }, [values, initialValues]);

  // Warn on navigate away with dirty state
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (isDirty) {
        e.preventDefault();
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  const set = useCallback((field: string, value: string | number | boolean) => {
    setValues((prev) => ({ ...prev, [field]: value }));
    setFieldErrors((prev) => {
      if (prev[field]) {
        const next = { ...prev };
        delete next[field];
        return next;
      }
      return prev;
    });
  }, []);

  const discard = useCallback(() => {
    setValues({ ...initialValues });
    setFieldErrors({});
    setToast(null);
  }, [initialValues]);

  const save = async () => {
    setSaving(true);
    setToast(null);
    setFieldErrors({});

    // Only send changed fields
    const changed: Record<string, unknown> = {};
    for (const key of Object.keys(values)) {
      if (values[key] !== initialValues[key]) {
        // Don't send masked values back
        if (values[key] === "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022") continue;
        changed[key] = values[key];
      }
    }

    if (Object.keys(changed).length === 0) {
      setToast({ type: "success", text: "No changes to save" });
      setSaving(false);
      return;
    }

    try {
      const resp = await fetch(`${API}/api/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: changed, restart: true }),
      });
      const data = await resp.json();
      if (data.success) {
        setToast({ type: "success", text: "Settings saved" });
        if (data.full_restart_required) {
          setToast({
            type: "success",
            text: "Settings saved. Restart the bot for wallet changes to take effect.",
          });
        }
        await loadConfig();
      } else {
        if (data.errors) {
          setFieldErrors(data.errors);
          setToast({ type: "error", text: "Validation errors — check highlighted fields" });
        } else {
          setToast({ type: "error", text: data.error || "Failed to save" });
        }
      }
    } catch {
      setToast({ type: "error", text: "Cannot connect to backend" });
    }
    setSaving(false);
  };

  const testWallet = async () => {
    setWalletTesting(true);
    setWalletResult(null);
    try {
      const resp = await fetch(`${API}/api/config/wallet/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          wallet_name: values.BT_WALLET_NAME,
          hotkey: values.BT_WALLET_HOTKEY,
          wallet_path: values.BT_WALLET_PATH,
          password:
            values.BT_WALLET_PASSWORD === "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
              ? ""
              : values.BT_WALLET_PASSWORD,
        }),
      });
      setWalletResult(await resp.json());
    } catch {
      setWalletResult({ success: false, error: "Cannot connect to backend" });
    }
    setWalletTesting(false);
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
    setShowCreateModal(false);
    setMnemonicPhaseActive(false);
    setWalletResult({
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
  };

  const testTelegram = async () => {
    setTelegramTesting(true);
    setTelegramResult(null);
    try {
      const resp = await fetch(`${API}/api/config/test-telegram`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_token:
            values.TELEGRAM_BOT_TOKEN === "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
              ? ""
              : values.TELEGRAM_BOT_TOKEN,
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
        body: JSON.stringify({
          api_key:
            values.TAOSTATS_API_KEY === "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
              ? ""
              : values.TAOSTATS_API_KEY,
        }),
      });
      setTaostatsResult(await resp.json());
    } catch {
      setTaostatsResult({ success: false, error: "Cannot connect to backend" });
    }
    setTaostatsTesting(false);
  };

  const openSafetyGate = async () => {
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
          password:
            values.BT_WALLET_PASSWORD === "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
              ? ""
              : values.BT_WALLET_PASSWORD,
          pot_tao: Number(values.EMA_POT_TAO) || 10.0,
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
    set("EMA_DRY_RUN", false);
    setSafetyGateConfirmed(false);
  };

  const cancelSafetyGate = () => {
    setShowSafetyGate(false);
    setSafetyGateConfirmed(false);
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

  const errorBorder = (field: string) =>
    fieldErrors[field] ? "border-red-600" : "border-gray-700";

  const errorMsg = (field: string) =>
    fieldErrors[field] ? (
      <p className="text-red-400 text-xs mt-1">{fieldErrors[field]}</p>
    ) : null;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="h-8 w-8 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold text-white">Settings</h1>

      {/* Wallet */}
      <Section title="Wallet">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <FieldLabel htmlFor="s-wallet-name" tip="The directory name of your wallet under the wallet path. Created with 'btcli wallet create' or via the wizard.">Wallet Name</FieldLabel>
            <input
              id="s-wallet-name"
              type="text"
              value={String(values.BT_WALLET_NAME || "")}
              onChange={(e) => set("BT_WALLET_NAME", e.target.value)}
              className={`w-full bg-gray-800 border ${errorBorder("BT_WALLET_NAME")} text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none`}
            />
            {errorMsg("BT_WALLET_NAME")}
          </div>
          <div>
            <FieldLabel htmlFor="s-hotkey" tip="The hotkey used for signing transactions. Each wallet can have multiple hotkeys. 'default' works for most setups.">Hotkey Name</FieldLabel>
            <input
              id="s-hotkey"
              type="text"
              value={String(values.BT_WALLET_HOTKEY || "")}
              onChange={(e) => set("BT_WALLET_HOTKEY", e.target.value)}
              className={`w-full bg-gray-800 border ${errorBorder("BT_WALLET_HOTKEY")} text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none`}
            />
            {errorMsg("BT_WALLET_HOTKEY")}
          </div>
        </div>
        <div>
          <FieldLabel htmlFor="s-wallet-path" tip="Directory where Bittensor wallets are stored. The default (~/.bittensor/wallets) is standard for btcli.">Wallet Path</FieldLabel>
          <input
            id="s-wallet-path"
            type="text"
            value={String(values.BT_WALLET_PATH || "")}
            onChange={(e) => set("BT_WALLET_PATH", e.target.value)}
            className={`w-full bg-gray-800 border ${errorBorder("BT_WALLET_PATH")} text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none`}
          />
          {errorMsg("BT_WALLET_PATH")}
        </div>
        <div>
          <FieldLabel htmlFor="s-wallet-pw" tip="If your coldkey is encrypted, enter the password here. The bot needs it to sign transactions. Leave blank for unencrypted keys.">Coldkey Password</FieldLabel>
          <PasswordInput
            id="s-wallet-pw"
            value={String(values.BT_WALLET_PASSWORD || "")}
            onChange={(v) => set("BT_WALLET_PASSWORD", v)}
          />
        </div>
        <div>
          <button
            type="button"
            onClick={testWallet}
            disabled={walletTesting}
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium disabled:opacity-50 transition-colors"
          >
            {walletTesting ? "Verifying..." : "Verify Wallet"}
          </button>
        </div>

        {/* Validation checklist */}
        {walletResult && (
          <div>
            {!walletResult.success && walletResult.error ? (
              <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-red-400 text-sm">
                {walletResult.error}
              </div>
            ) : walletResult.checks ? (
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm">
                  <span className={walletResult.checks.coldkey_exists ? "text-emerald-400" : "text-red-400"}>
                    {walletResult.checks.coldkey_exists ? "\u2713" : "\u2717"}
                  </span>
                  <span className="text-gray-300">
                    {walletResult.checks.coldkey_exists ? "Coldkey found" : "Coldkey not found"}
                  </span>
                </div>
                <div className="flex items-center gap-2 text-sm">
                  <span className={walletResult.checks.hotkey_exists ? "text-emerald-400" : "text-red-400"}>
                    {walletResult.checks.hotkey_exists ? "\u2713" : "\u2717"}
                  </span>
                  <span className="text-gray-300">
                    {walletResult.checks.hotkey_exists ? "Hotkey found" : "Hotkey not found"}
                  </span>
                </div>
                <div className="flex items-center gap-2 text-sm">
                  <span className={walletResult.checks.coldkey_unlockable ? "text-emerald-400" : "text-red-400"}>
                    {walletResult.checks.coldkey_unlockable ? "\u2713" : "\u2717"}
                  </span>
                  <span className="text-gray-300">
                    {walletResult.checks.coldkey_unlockable ? "Coldkey unlocked" : "Could not unlock coldkey"}
                  </span>
                </div>
                {walletResult.checks.coldkey_ss58 && (
                  <div className="text-sm text-gray-400">
                    Address: <span className="font-mono text-gray-300">{walletResult.checks.coldkey_ss58}</span>
                  </div>
                )}
                <div className="flex items-center gap-2 text-sm">
                  {walletResult.checks.balance_tao !== null ? (
                    <>
                      <span className={walletResult.checks.balance_tao === 0 ? "text-amber-400" : "text-emerald-400"}>
                        {walletResult.checks.balance_tao === 0 ? "\u26A0" : "\u2713"}
                      </span>
                      <span className="text-gray-300">Balance: {walletResult.checks.balance_tao} TAO</span>
                    </>
                  ) : (
                    <>
                      <span className="text-amber-400">{"\u26A0"}</span>
                      <span className="text-gray-300">Balance: unavailable</span>
                    </>
                  )}
                </div>
                {walletResult.warnings && walletResult.warnings.length > 0 && (
                  <div className="space-y-1 mt-1">
                    {walletResult.warnings.map((w, i) => (
                      <div key={i} className="flex items-center gap-2 text-sm text-amber-400">
                        <span>{"\u26A0"}</span> {w}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : null}
          </div>
        )}

        {/* Separator + Create New Wallet */}
        <div className="border-t border-gray-800 pt-4 flex items-center justify-between">
          <span className="text-sm text-gray-500">Don&apos;t have a wallet?</span>
          <button
            type="button"
            onClick={() => setShowCreateModal(true)}
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium transition-colors"
          >
            Create New Wallet
          </button>
        </div>
      </Section>

      {/* Create Wallet Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-gray-900 border border-gray-800 rounded-xl max-w-lg mx-auto w-full p-6 relative max-h-[90vh] overflow-y-auto">
            {/* Close button — disabled during mnemonic phase */}
            <button
              type="button"
              onClick={() => {
                if (!mnemonicPhaseActive) {
                  setShowCreateModal(false);
                }
              }}
              disabled={mnemonicPhaseActive}
              className="absolute top-4 right-4 text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
            >
              &times;
            </button>
            <WalletCreator
              onCreated={handleWalletCreated}
              onCancel={() => {
                setShowCreateModal(false);
                setMnemonicPhaseActive(false);
              }}
            />
          </div>
        </div>
      )}

      {/* API Keys */}
      <Section title="API Keys">
        <div>
          <FieldLabel htmlFor="s-taostats" tip="Taostats provides subnet pool data and price history. The free tier (no key) allows 30 req/min. Get a key at taostats.io">Taostats API Key</FieldLabel>
          <div className="flex gap-2">
            <div className="flex-1">
              <PasswordInput
                id="s-taostats"
                value={String(values.TAOSTATS_API_KEY || "")}
                onChange={(v) => set("TAOSTATS_API_KEY", v)}
              />
            </div>
            <button
              type="button"
              onClick={testTaostats}
              disabled={taostatsTesting}
              className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium disabled:opacity-50 transition-colors shrink-0"
            >
              {taostatsTesting ? "Testing..." : "Test"}
            </button>
          </div>
          {taostatsResult && (
            <p className={`text-sm mt-1 ${taostatsResult.success ? "text-emerald-400" : "text-red-400"}`}>
              {taostatsResult.success ? `\u2713 ${taostatsResult.message}` : taostatsResult.error}
            </p>
          )}
        </div>
      </Section>

      {/* Telegram */}
      <Section title="Telegram">
        <div>
          <FieldLabel htmlFor="s-tg-token" tip="The token @BotFather gives you after creating a bot. Message @BotFather on Telegram and send /newbot.">Bot Token</FieldLabel>
          <PasswordInput
            id="s-tg-token"
            value={String(values.TELEGRAM_BOT_TOKEN || "")}
            onChange={(v) => set("TELEGRAM_BOT_TOKEN", v)}
          />
        </div>
        <div>
          <FieldLabel htmlFor="s-tg-chat" tip="Your numeric Telegram user ID. Send any message to @userinfobot on Telegram to find it.">Chat ID</FieldLabel>
          <div className="flex gap-2">
            <input
              id="s-tg-chat"
              type="text"
              value={String(values.TELEGRAM_CHAT_ID || "")}
              onChange={(e) => set("TELEGRAM_CHAT_ID", e.target.value)}
              className="flex-1 bg-gray-800 border border-gray-700 text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none"
            />
            <button
              type="button"
              onClick={testTelegram}
              disabled={telegramTesting}
              className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium disabled:opacity-50 transition-colors shrink-0"
            >
              {telegramTesting ? "Testing..." : "Test"}
            </button>
          </div>
          {telegramResult && (
            <p className={`text-sm mt-1 ${telegramResult.success ? "text-emerald-400" : "text-red-400"}`}>
              {telegramResult.success ? "\u2713 Message sent! Check your Telegram." : telegramResult.error}
            </p>
          )}
        </div>
      </Section>

      {/* Trading */}
      <Section title="Trading">
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
          <span className="text-sm text-gray-400">
            {values.EMA_DRY_RUN ? "ON" : "OFF"}
          </span>
        </div>
        {!values.EMA_DRY_RUN && (
          <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-red-400 text-sm font-medium">
            Live trading is enabled. The bot will execute real trades with your TAO.
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <FieldLabel htmlFor="s-pot" tip="Total TAO allocated for trading. Divided equally among max positions. Independent of your wallet balance.">Trading Pot</FieldLabel>
            <NumberInput
              id="s-pot"
              value={Number(values.EMA_POT_TAO) || 0}
              onChange={(v) => set("EMA_POT_TAO", v)}
              suffix="TAO"
              min={0.1}
              step={0.5}
            />
            {errorMsg("EMA_POT_TAO")}
          </div>
          <div>
            <FieldLabel htmlFor="s-max-pos" tip="Maximum number of subnet positions open at once. Each gets an equal share of the pot.">Max Open Positions</FieldLabel>
            <NumberInput
              id="s-max-pos"
              value={Number(values.EMA_MAX_POSITIONS) || 5}
              onChange={(v) => set("EMA_MAX_POSITIONS", Math.max(1, Math.min(20, Math.round(v))))}
              min={1}
              max={20}
              step={1}
            />
            {errorMsg("EMA_MAX_POSITIONS")}
          </div>
          <div>
            <FieldLabel htmlFor="s-slippage" tip="Maximum price slippage allowed on entry. If the actual price differs by more than this %, the trade is rejected.">Max Slippage</FieldLabel>
            <NumberInput
              id="s-slippage"
              value={Number(values.MAX_SLIPPAGE_PCT) || 5}
              onChange={(v) => set("MAX_SLIPPAGE_PCT", v)}
              suffix="%"
              min={0.1}
              max={50}
              step={0.5}
            />
            {errorMsg("MAX_SLIPPAGE_PCT")}
          </div>
          <div>
            <FieldLabel htmlFor="s-stop-loss" tip="Exit if a position drops this % below its entry price. Protects against large losses.">Stop Loss</FieldLabel>
            <NumberInput
              id="s-stop-loss"
              value={Number(values.EMA_STOP_LOSS_PCT) || 8}
              onChange={(v) => set("EMA_STOP_LOSS_PCT", v)}
              suffix="%"
              min={1}
              max={50}
              step={0.5}
            />
            {errorMsg("EMA_STOP_LOSS_PCT")}
          </div>
          <div>
            <FieldLabel htmlFor="s-take-profit" tip="Exit when a position gains this % above entry price. Locks in profit at your target.">Take Profit</FieldLabel>
            <NumberInput
              id="s-take-profit"
              value={Number(values.EMA_TAKE_PROFIT_PCT) || 20}
              onChange={(v) => set("EMA_TAKE_PROFIT_PCT", v)}
              suffix="%"
              min={1}
              max={100}
              step={1}
            />
            {errorMsg("EMA_TAKE_PROFIT_PCT")}
          </div>
          <div>
            <FieldLabel htmlFor="s-trailing" tip="Once profitable, exit if the position drops this % from its peak. Lets winners run while protecting gains.">Trailing Stop</FieldLabel>
            <NumberInput
              id="s-trailing"
              value={Number(values.EMA_TRAILING_STOP_PCT) || 5}
              onChange={(v) => set("EMA_TRAILING_STOP_PCT", v)}
              suffix="%"
              min={1}
              max={50}
              step={0.5}
            />
            {errorMsg("EMA_TRAILING_STOP_PCT")}
          </div>
          <div>
            <FieldLabel htmlFor="s-max-hold" tip="Automatically exit a position after this many hours regardless of P&L. Prevents capital stuck in stale trades.">Max Holding Time</FieldLabel>
            <NumberInput
              id="s-max-hold"
              value={Number(values.EMA_MAX_HOLDING_HOURS) || 168}
              onChange={(v) => set("EMA_MAX_HOLDING_HOURS", Math.round(v))}
              suffix="hours"
              min={1}
              max={720}
              step={1}
            />
            {errorMsg("EMA_MAX_HOLDING_HOURS")}
          </div>
          <div>
            <FieldLabel htmlFor="s-cooldown" tip="Wait this long before re-entering the same subnet after an exit. Prevents immediate re-entry into a failing position.">Re-entry Cooldown</FieldLabel>
            <NumberInput
              id="s-cooldown"
              value={Number(values.EMA_COOLDOWN_HOURS) || 4}
              onChange={(v) => set("EMA_COOLDOWN_HOURS", v)}
              suffix="hours"
              min={0}
              max={48}
              step={0.5}
            />
            {errorMsg("EMA_COOLDOWN_HOURS")}
          </div>
        </div>
      </Section>

      {/* Advanced EMA */}
      <CollapsibleSection title="Advanced EMA">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <FieldLabel htmlFor="s-ema-period" tip="Number of candles for the slow EMA line. Larger = smoother, slower to react. The crossover of fast over slow generates buy signals.">Slow EMA Period</FieldLabel>
            <NumberInput
              id="s-ema-period"
              value={Number(values.EMA_PERIOD) || 18}
              onChange={(v) => set("EMA_PERIOD", Math.round(v))}
              min={2}
              max={100}
              step={1}
            />
            {errorMsg("EMA_PERIOD")}
          </div>
          <div>
            <FieldLabel htmlFor="s-fast-period" tip="Number of candles for the fast EMA line. Smaller = more responsive. Must be less than slow EMA period.">Fast EMA Period</FieldLabel>
            <NumberInput
              id="s-fast-period"
              value={Number(values.EMA_FAST_PERIOD) || 6}
              onChange={(v) => set("EMA_FAST_PERIOD", Math.round(v))}
              min={2}
              max={100}
              step={1}
            />
            {errorMsg("EMA_FAST_PERIOD")}
          </div>
          <div>
            <FieldLabel htmlFor="s-confirm-bars" tip="How many consecutive candles the fast EMA must stay above the slow EMA before confirming a buy signal. Higher = fewer false signals but later entries.">Confirmation Bars</FieldLabel>
            <NumberInput
              id="s-confirm-bars"
              value={Number(values.EMA_CONFIRM_BARS) || 3}
              onChange={(v) => set("EMA_CONFIRM_BARS", Math.round(v))}
              min={1}
              max={10}
              step={1}
            />
            {errorMsg("EMA_CONFIRM_BARS")}
          </div>
          <div>
            <FieldLabel htmlFor="s-candle-tf" tip="Each candle represents this many hours of price data. Larger = smoother signals, fewer trades. 4h is a good balance.">Candle Timeframe</FieldLabel>
            <NumberInput
              id="s-candle-tf"
              value={Number(values.EMA_CANDLE_TIMEFRAME_HOURS) || 4}
              onChange={(v) => set("EMA_CANDLE_TIMEFRAME_HOURS", Math.round(v))}
              suffix="hours"
              min={1}
              max={24}
              step={1}
            />
            {errorMsg("EMA_CANDLE_TIMEFRAME_HOURS")}
          </div>
          <div>
            <FieldLabel htmlFor="s-drawdown" tip="If total portfolio P&L drops this % from peak, pause all new entries. A circuit breaker for bad market conditions.">Drawdown Breaker</FieldLabel>
            <NumberInput
              id="s-drawdown"
              value={Number(values.EMA_DRAWDOWN_BREAKER_PCT) || 15}
              onChange={(v) => set("EMA_DRAWDOWN_BREAKER_PCT", v)}
              suffix="%"
              min={1}
              max={50}
              step={0.5}
            />
            {errorMsg("EMA_DRAWDOWN_BREAKER_PCT")}
          </div>
          <div>
            <FieldLabel htmlFor="s-drawdown-pause" tip="How long to pause new entries after the drawdown breaker trips. Existing positions are still monitored for exits.">Drawdown Pause</FieldLabel>
            <NumberInput
              id="s-drawdown-pause"
              value={Number(values.EMA_DRAWDOWN_PAUSE_HOURS) || 6}
              onChange={(v) => set("EMA_DRAWDOWN_PAUSE_HOURS", v)}
              suffix="hours"
              min={0.5}
              max={48}
              step={0.5}
            />
            {errorMsg("EMA_DRAWDOWN_PAUSE_HOURS")}
          </div>
          <div>
            <FieldLabel htmlFor="s-pos-size" tip="Fraction of pot per position (0.20 = 20%). Usually set automatically as 1/max_positions, but can be overridden here.">Position Size</FieldLabel>
            <NumberInput
              id="s-pos-size"
              value={Number(values.EMA_POSITION_SIZE_PCT) || 0.2}
              onChange={(v) => set("EMA_POSITION_SIZE_PCT", v)}
              min={0.01}
              max={1}
              step={0.01}
            />
            {errorMsg("EMA_POSITION_SIZE_PCT")}
          </div>
          <div>
            <FieldLabel htmlFor="s-scan" tip="Minutes between full strategy scans. Each scan fetches prices, computes signals, and may open/close positions. Lower = more responsive but more API calls.">Scan Interval</FieldLabel>
            <NumberInput
              id="s-scan"
              value={Number(values.SCAN_INTERVAL_MIN) || 15}
              onChange={(v) => set("SCAN_INTERVAL_MIN", Math.round(v))}
              suffix="min"
              min={1}
              max={60}
              step={1}
            />
            {errorMsg("SCAN_INTERVAL_MIN")}
          </div>
          <div>
            <FieldLabel htmlFor="s-log-level" tip="Controls log verbosity. DEBUG shows everything (noisy). INFO is recommended for normal use. WARNING and above only show issues.">Log Level</FieldLabel>
            <select
              id="s-log-level"
              value={String(values.LOG_LEVEL || "INFO")}
              onChange={(e) => set("LOG_LEVEL", e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none"
            >
              {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </div>
        </div>
      </CollapsibleSection>

      {/* Save bar */}
      {isDirty && (
        <div className="sticky bottom-4 bg-gray-900 border border-gray-700 rounded-xl p-4 flex items-center justify-between shadow-lg">
          <span className="text-sm text-amber-400 font-medium">Unsaved changes</span>
          <div className="flex gap-3">
            <button
              type="button"
              onClick={discard}
              className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium transition-colors"
            >
              Discard
            </button>
            <button
              type="button"
              onClick={save}
              disabled={saving}
              className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-sm font-medium disabled:opacity-50 transition-colors"
            >
              {saving ? "Saving..." : "Save Changes"}
            </button>
          </div>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 px-4 py-3 rounded-lg shadow-lg text-sm font-medium animate-[ema-toast-in_220ms_ease-out] ${
            toast.type === "success"
              ? "bg-emerald-900/90 border border-emerald-700 text-emerald-300"
              : "bg-red-900/90 border border-red-700 text-red-300"
          }`}
        >
          {toast.text}
          <button
            type="button"
            onClick={() => setToast(null)}
            className="ml-3 text-gray-400 hover:text-white"
          >
            x
          </button>
        </div>
      )}

      {/* Safety gate modal */}
      {showSafetyGate && (
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
                  {[
                    { key: "wallet_configured", label: "Wallet configured" },
                    { key: "wallet_unlockable", label: "Wallet unlockable" },
                    { key: "balance_sufficient", label: "Balance sufficient" },
                    { key: "rpc_connected", label: "RPC connected" },
                    { key: "taostats_reachable", label: "Taostats reachable" },
                    { key: "telegram_configured", label: "Telegram configured" },
                  ].map(({ key, label }) => {
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
      )}
    </div>
  );
}
