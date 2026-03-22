"use client";

import { useState } from "react";

const API =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8081`
    : "http://localhost:8081");

interface WalletCreatorProps {
  onCreated: (wallet: {
    wallet_name: string;
    hotkey: string;
    wallet_path: string;
    password: string;
    coldkey_ss58: string;
  }) => void;
  onCancel?: () => void;
}

type Phase = "form" | "creating" | "mnemonic";

export default function WalletCreator({ onCreated, onCancel }: WalletCreatorProps) {
  const [phase, setPhase] = useState<Phase>("form");
  const [walletName, setWalletName] = useState("trader_wallet");
  const [hotkeyName, setHotkeyName] = useState("trader_hotkey");
  const [walletPath, setWalletPath] = useState("~/.bittensor/wallets");
  const [encrypt, setEncrypt] = useState(true);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [error, setError] = useState("");

  // Mnemonic phase state
  const [mnemonic, setMnemonic] = useState<string | null>(null);
  const [coldkeySs58, setColdkeySs58] = useState("");
  const [mnemonicSaved, setMnemonicSaved] = useState(false);
  const [copied, setCopied] = useState<"mnemonic" | "address" | null>(null);

  // Validation
  const nameValid = /^[A-Za-z0-9_]+$/.test(walletName) && walletName.length <= 64;
  const hotkeyValid = /^[A-Za-z0-9_]+$/.test(hotkeyName) && hotkeyName.length <= 64;
  const passwordsMatch = password === confirmPassword;
  const passwordLong = password.length >= 8;
  const formValid =
    walletName.trim() !== "" &&
    hotkeyName.trim() !== "" &&
    walletPath.trim() !== "" &&
    nameValid &&
    hotkeyValid &&
    (!encrypt || (passwordsMatch && passwordLong));

  const handleCreate = async () => {
    setError("");
    setPhase("creating");

    try {
      const resp = await fetch(`${API}/api/config/wallet/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          wallet_name: walletName,
          hotkey: hotkeyName,
          wallet_path: walletPath,
          password: encrypt ? password : "",
        }),
      });
      const data = await resp.json();
      if (data.success) {
        setMnemonic(data.mnemonic);
        setColdkeySs58(data.coldkey_ss58);
        setPhase("mnemonic");
      } else {
        setError(data.error || "Failed to create wallet");
        setPhase("form");
      }
    } catch {
      setError("Cannot connect to backend");
      setPhase("form");
    }
  };

  const handleCopy = async (text: string, which: "mnemonic" | "address") => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(which);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      // fallback
    }
  };

  const handleContinue = () => {
    // Clear mnemonic from state
    const result = {
      wallet_name: walletName,
      hotkey: hotkeyName,
      wallet_path: walletPath,
      password: encrypt ? password : "",
      coldkey_ss58: coldkeySs58,
    };
    setMnemonic(null);
    onCreated(result);
  };

  // ── Mnemonic display ──
  if (phase === "mnemonic" && mnemonic) {
    const words = mnemonic.split(" ");
    return (
      <div className="space-y-5">
        <div className="bg-amber-900/20 border border-amber-700 rounded-lg p-4">
          <h3 className="text-lg font-semibold text-amber-400 mb-2">
            SAVE YOUR RECOVERY PHRASE
          </h3>
          <p className="text-sm text-amber-300/80">
            This is the ONLY way to recover your wallet if you lose access.
            Write it down and store it securely. It will NOT be shown again.
          </p>
        </div>

        <div className="bg-amber-900/20 border border-amber-700 rounded-lg p-4">
          <div className="grid grid-cols-4 gap-2">
            {words.map((word, i) => (
              <div
                key={i}
                className="bg-gray-800 rounded px-2 py-1 text-sm font-mono flex items-center gap-1.5"
              >
                <span className="text-gray-500 text-xs w-5 text-right">{i + 1}.</span>
                <span className="text-gray-100">{word}</span>
              </div>
            ))}
          </div>
        </div>

        <button
          type="button"
          onClick={() => handleCopy(mnemonic, "mnemonic")}
          className="bg-gray-700 hover:bg-gray-600 text-sm px-3 py-1 rounded text-gray-200 transition-colors"
        >
          {copied === "mnemonic" ? "Copied!" : "Copy to Clipboard"}
        </button>

        <div>
          <p className="text-sm text-gray-400 mb-2">Your coldkey address (for funding):</p>
          <div className="bg-gray-800 border border-gray-700 rounded-lg p-3 flex items-center justify-between gap-2">
            <code className="text-sm text-gray-200 font-mono break-all">{coldkeySs58}</code>
            <button
              type="button"
              onClick={() => handleCopy(coldkeySs58, "address")}
              className="bg-gray-700 hover:bg-gray-600 text-sm px-3 py-1 rounded text-gray-200 shrink-0 transition-colors"
            >
              {copied === "address" ? "Copied!" : "Copy"}
            </button>
          </div>
        </div>

        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={mnemonicSaved}
            onChange={(e) => setMnemonicSaved(e.target.checked)}
            className="w-4 h-4 rounded border-gray-600 bg-gray-800 text-indigo-500 focus:ring-indigo-500"
          />
          <span className="text-sm text-gray-300">I have saved my recovery phrase securely</span>
        </label>

        <button
          type="button"
          onClick={handleContinue}
          disabled={!mnemonicSaved}
          className="w-full py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg font-semibold text-sm disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          Continue
        </button>
      </div>
    );
  }

  // ── Creation form ──
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold text-white mb-1">Create Your Wallet</h2>
        <p className="text-gray-400 text-sm">
          A Bittensor wallet consists of a coldkey (for holding funds) and a hotkey (for staking
          operations). We&apos;ll create both for you.
        </p>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">Wallet Name</label>
        <input
          type="text"
          value={walletName}
          onChange={(e) => setWalletName(e.target.value)}
          className={`w-full bg-gray-800 border ${
            walletName && !nameValid ? "border-red-600" : "border-gray-700"
          } text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none`}
        />
        {walletName && !nameValid && (
          <p className="text-red-400 text-xs mt-1">Only letters, numbers, underscores (max 64)</p>
        )}
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">Hotkey Name</label>
        <input
          type="text"
          value={hotkeyName}
          onChange={(e) => setHotkeyName(e.target.value)}
          className={`w-full bg-gray-800 border ${
            hotkeyName && !hotkeyValid ? "border-red-600" : "border-gray-700"
          } text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none`}
        />
        {hotkeyName && !hotkeyValid && (
          <p className="text-red-400 text-xs mt-1">Only letters, numbers, underscores (max 64)</p>
        )}
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">Wallet Path</label>
        <input
          type="text"
          value={walletPath}
          onChange={(e) => setWalletPath(e.target.value)}
          className="w-full bg-gray-800 border border-gray-700 text-gray-100 rounded-lg px-3 py-2 focus:ring-2 focus:ring-indigo-500 outline-none"
        />
      </div>

      {/* Encrypt toggle */}
      <div className="flex items-center gap-3">
        <label className="text-sm font-medium text-gray-300">Encrypt Coldkey</label>
        <button
          type="button"
          onClick={() => setEncrypt(!encrypt)}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
            encrypt ? "bg-indigo-500" : "bg-gray-600"
          }`}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
              encrypt ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
        <span className="text-sm text-gray-400">{encrypt ? "ON" : "OFF"}</span>
      </div>

      {encrypt ? (
        <>
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">Password</label>
            <div className="relative">
              <input
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={`w-full bg-gray-800 border ${
                  password && !passwordLong ? "border-red-600" : "border-gray-700"
                } text-gray-100 rounded-lg px-3 py-2 pr-10 focus:ring-2 focus:ring-indigo-500 outline-none`}
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-200 text-sm"
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
            {password && !passwordLong && (
              <p className="text-red-400 text-xs mt-1">Minimum 8 characters</p>
            )}
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">Confirm Password</label>
            <div className="relative">
              <input
                type={showConfirm ? "text" : "password"}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className={`w-full bg-gray-800 border ${
                  confirmPassword && !passwordsMatch ? "border-red-600" : "border-gray-700"
                } text-gray-100 rounded-lg px-3 py-2 pr-10 focus:ring-2 focus:ring-indigo-500 outline-none`}
              />
              <button
                type="button"
                onClick={() => setShowConfirm(!showConfirm)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-200 text-sm"
              >
                {showConfirm ? "Hide" : "Show"}
              </button>
            </div>
            {confirmPassword && !passwordsMatch && (
              <p className="text-red-400 text-xs mt-1">Passwords do not match</p>
            )}
          </div>
          <div className="bg-amber-900/20 border border-amber-700 rounded-lg p-3 text-amber-400 text-sm">
            If you encrypt your coldkey, you&apos;ll need this password every time the bot starts.
            If you lose it, your funds are recoverable only via the mnemonic seed phrase.
          </div>
        </>
      ) : (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-red-400 text-sm">
          Your coldkey will be stored unencrypted. Anyone with access to this machine can use your
          wallet.
        </div>
      )}

      {error && (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      <div className="flex gap-3">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm font-medium transition-colors"
          >
            Back
          </button>
        )}
        <button
          type="button"
          onClick={handleCreate}
          disabled={!formValid || phase === "creating"}
          className="flex-1 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg font-semibold text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {phase === "creating" ? (
            <span className="flex items-center justify-center gap-2">
              <span className="h-4 w-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              Creating wallet...
            </span>
          ) : (
            "Create Wallet"
          )}
        </button>
      </div>
    </div>
  );
}
