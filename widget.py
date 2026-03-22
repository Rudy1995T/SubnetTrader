#!/usr/bin/env python3
"""SubnetTrader Desktop Widget — live dual-strategy portfolio display.

Standalone Tkinter + Matplotlib widget that polls the local FastAPI
backend and shows both Scalper and Trend strategy positions, sparklines,
and signals.  No browser needed.

Usage:
    source .venv/bin/activate
    python widget.py
"""

import os
import threading
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure                              # noqa: E402
from matplotlib.ticker import MaxNLocator                         # noqa: E402

import tkinter as tk      # noqa: E402
from tkinter import font as tkfont  # noqa: E402

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────
API_BASE    = os.getenv("WIDGET_API", "http://localhost:8081").rstrip("/")
REFRESH_SEC = int(os.getenv("WIDGET_REFRESH_SEC", "30"))
OPACITY     = float(os.getenv("WIDGET_OPACITY", "0.92"))
WIN_W, WIN_H = 480, 760

# ── Colour palette ─────────────────────────────────────────────────────────
BG        = "#0d1117"
BG_CARD   = "#161b22"
BG_HEADER = "#1c2128"
WHITE     = "#e6edf3"
GREY      = "#8b949e"
GREEN     = "#3fb950"
RED       = "#f85149"
YELLOW    = "#d29922"
SKY       = "#58a6ff"
ORANGE    = "#d18616"
PURPLE    = "#bc8cff"


# ═══════════════════════════════════════════════════════════════════════════
#  Thread-safe data store
# ═══════════════════════════════════════════════════════════════════════════

class DataStore:
    def __init__(self):
        self.portfolio: dict | None = None
        self.signals: list[dict] = []
        self.strategies: list[dict] = []
        self.trades: list[dict] = []
        self.tao_usd: float | None = None
        self.bot_online: bool = False
        self.last_ok: str | None = None
        self.daily_trades: list[dict] = []
        self._lock = threading.Lock()

    def put(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snap(self) -> dict:
        with self._lock:
            return {
                "portfolio": self.portfolio,
                "signals": self.signals,
                "strategies": self.strategies,
                "trades": self.trades,
                "tao_usd": self.tao_usd,
                "bot_online": self.bot_online,
                "last_ok": self.last_ok,
                "daily_trades": self.daily_trades,
            }


def _fetch(store: DataStore):
    """Poll every endpoint once — called from background thread."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")

    # Health
    try:
        r = requests.get(f"{API_BASE}/health", timeout=5)
        store.put(bot_online=r.json().get("status") == "ok")
    except Exception:
        store.put(bot_online=False)

    # Portfolio (scalper + trend + combined)
    try:
        r = requests.get(f"{API_BASE}/api/ema/portfolio", timeout=5)
        store.put(portfolio=r.json())
    except Exception:
        pass

    # Signals (includes prices + EMA arrays for sparklines)
    try:
        r = requests.get(f"{API_BASE}/api/ema/signals", timeout=5)
        data = r.json()
        store.put(signals=data.get("signals", []), strategies=data.get("strategies", []))
    except Exception:
        pass

    # Recent closed trades
    try:
        r = requests.get(f"{API_BASE}/api/ema/recent-trades?limit=5", timeout=5)
        store.put(trades=r.json().get("trades", []))
    except Exception:
        pass

    # TAO / USD
    try:
        r = requests.get(f"{API_BASE}/api/price/tao-usd", timeout=5)
        store.put(tao_usd=r.json().get("usd"))
    except Exception:
        pass

    # Daily trades (larger limit to compute today's stats)
    try:
        r = requests.get(f"{API_BASE}/api/ema/recent-trades?limit=50", timeout=5)
        all_trades = r.json().get("trades", [])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = []
        for t in all_trades:
            raw_dt = t.get("exit_ts") or t.get("closed_at") or t.get("exit_time") or ""
            if raw_dt and raw_dt[:10] == today:
                daily.append(t)
        store.put(daily_trades=daily)
    except Exception:
        pass

    store.put(last_ok=now)


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

_STRAT_COLOUR = {"scalper": SKY, "trend": PURPLE}
_STRAT_TAG = {"scalper": "S", "trend": "T"}


def _strat_positions(portfolio: dict | None) -> list[dict]:
    """Merge open positions from both strategies, tagged with strategy key."""
    if portfolio is None:
        return []
    out: list[dict] = []
    for key in ("scalper", "trend"):
        strat = portfolio.get(key, {})
        if not strat.get("enabled"):
            continue
        for pos in strat.get("open_positions", []):
            pos = dict(pos)  # shallow copy
            pos["_strategy"] = key
            out.append(pos)
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Widget
# ═══════════════════════════════════════════════════════════════════════════

class Widget:
    MAX_POS_ROWS = 6

    def __init__(self):
        self.store = DataStore()
        self.selected_netuid: int | None = None
        self._drag = {"x": 0, "y": 0}
        self._prev_pos_ids: set[str] = set()      # track open position IDs
        self._prev_trade_ids: set = set()          # track closed trade IDs
        self._first_tick: bool = True              # skip flash on first load

        # ── Root window ────────────────────────────────────────
        self.root = tk.Tk()
        self.root.title("SubnetTrader")
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", OPACITY)
        self.root.attributes("-topmost", True)
        self._topmost = True
        self._place_window()

        # ── Fonts ──────────────────────────────────────────────
        self._fb = tkfont.Font(family="monospace", size=11, weight="bold")
        self._fn = tkfont.Font(family="monospace", size=10)
        self._fs = tkfont.Font(family="monospace", size=9)
        self._ft = tkfont.Font(family="monospace", size=8)

        # ── Build sections ─────────────────────────────────────
        self._build_titlebar()
        self._build_summary()
        self._build_daily_stats()
        self._build_positions()
        self._build_sparkline()
        self._build_trades()
        self._build_statusbar()

        # ── Start background poller ────────────────────────────
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(1200, self._tick)

    # ── Window placement ───────────────────────────────────────

    def _place_window(self):
        x = os.getenv("WIDGET_X")
        y = int(os.getenv("WIDGET_Y", "20"))
        if x is not None:
            self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")
        else:
            sw = self.root.winfo_screenwidth()
            self.root.geometry(f"{WIN_W}x{WIN_H}+{sw - WIN_W - 20}+{y}")

    # ── Title bar ──────────────────────────────────────────────

    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg=BG_HEADER, height=32)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)
        bar.bind("<Button-1>", self._drag_start)
        bar.bind("<B1-Motion>", self._drag_move)

        self._dot = tk.Label(bar, text="\u25cf", fg=RED, bg=BG_HEADER, font=self._fb)
        self._dot.pack(side=tk.LEFT, padx=(8, 2))

        lbl = tk.Label(bar, text="SubnetTrader", fg=WHITE, bg=BG_HEADER, font=self._fb)
        lbl.pack(side=tk.LEFT, padx=(2, 6))
        for w in (lbl,):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # Close
        c = tk.Label(bar, text=" \u2715 ", fg=GREY, bg=BG_HEADER, font=self._fb, cursor="hand2")
        c.pack(side=tk.RIGHT, padx=(0, 4))
        c.bind("<Button-1>", lambda _: self.root.destroy())

        # Refresh
        r = tk.Label(bar, text=" \u27f3 ", fg=GREY, bg=BG_HEADER, font=self._fb, cursor="hand2")
        r.pack(side=tk.RIGHT)
        r.bind("<Button-1>", lambda _: self._force_refresh())

        # Context menu
        self._ctx = tk.Menu(self.root, tearoff=0, bg=BG_CARD, fg=WHITE,
                            activebackground=BG_HEADER, activeforeground=WHITE)
        self._ctx.add_command(label="Toggle always-on-top", command=self._toggle_topmost)
        self._ctx.add_separator()
        self._ctx.add_command(label="Opacity 70%",  command=lambda: self.root.attributes("-alpha", 0.70))
        self._ctx.add_command(label="Opacity 85%",  command=lambda: self.root.attributes("-alpha", 0.85))
        self._ctx.add_command(label="Opacity 100%", command=lambda: self.root.attributes("-alpha", 1.0))
        bar.bind("<Button-3>", lambda e: self._ctx.post(e.x_root, e.y_root))

    def _drag_start(self, e):
        self._drag["x"], self._drag["y"] = e.x, e.y

    def _drag_move(self, e):
        x = self.root.winfo_x() + e.x - self._drag["x"]
        y = self.root.winfo_y() + e.y - self._drag["y"]
        self.root.geometry(f"+{x}+{y}")

    def _toggle_topmost(self):
        self._topmost = not self._topmost
        self.root.attributes("-topmost", self._topmost)

    def _force_refresh(self):
        threading.Thread(target=_fetch, args=(self.store,), daemon=True).start()
        self.root.after(1500, self._tick)

    # ── Summary row ────────────────────────────────────────────

    def _build_summary(self):
        f = tk.Frame(self.root, bg=BG, pady=6, padx=12)
        f.pack(fill=tk.X)

        # Wallet + TAO/USD row
        r1 = tk.Frame(f, bg=BG); r1.pack(fill=tk.X)
        self._lbl_wallet = tk.Label(r1, text="Wallet  \u2014", fg=SKY, bg=BG, font=self._fn, anchor="w")
        self._lbl_wallet.pack(side=tk.LEFT)
        self._lbl_usd = tk.Label(r1, text="TAO/USD  \u2014", fg=GREY, bg=BG, font=self._fn, anchor="e")
        self._lbl_usd.pack(side=tk.RIGHT)

        # Per-strategy rows
        self._strat_rows: dict[str, dict] = {}
        for key, colour, label in [("scalper", SKY, "Scalper"), ("trend", PURPLE, "Trend")]:
            row = tk.Frame(f, bg=BG)
            row.pack(fill=tk.X, pady=(3, 0))
            tag_lbl = tk.Label(row, text=f"{label}", fg=colour, bg=BG, font=self._fs, anchor="w", width=8)
            tag_lbl.pack(side=tk.LEFT)
            ema_lbl = tk.Label(row, text="", fg=GREY, bg=BG, font=self._ft, anchor="w", width=6)
            ema_lbl.pack(side=tk.LEFT)
            badge = tk.Label(row, text="[off]", fg=GREY, bg=BG, font=self._ft, anchor="w")
            badge.pack(side=tk.LEFT, padx=(2, 6))
            pot_lbl = tk.Label(row, text="", fg=WHITE, bg=BG, font=self._ft, anchor="w")
            pot_lbl.pack(side=tk.LEFT, padx=(0, 6))
            dep_lbl = tk.Label(row, text="", fg=WHITE, bg=BG, font=self._ft, anchor="w")
            dep_lbl.pack(side=tk.LEFT, padx=(0, 6))
            slots_lbl = tk.Label(row, text="", fg=GREY, bg=BG, font=self._ft, anchor="e")
            slots_lbl.pack(side=tk.RIGHT)
            self._strat_rows[key] = {
                "frame": row, "tag": tag_lbl, "ema": ema_lbl, "badge": badge,
                "pot": pot_lbl, "dep": dep_lbl, "slots": slots_lbl,
            }

        tk.Frame(self.root, bg=GREY, height=1).pack(fill=tk.X, padx=8)

    # ── Daily stats mini-row ────────────────────────────────────

    def _build_daily_stats(self):
        f = tk.Frame(self.root, bg=BG, padx=12, pady=2)
        f.pack(fill=tk.X)
        self._lbl_daily = tk.Label(f, text="Today: \u2014", fg=GREY, bg=BG,
                                   font=self._ft, anchor="w")
        self._lbl_daily.pack(fill=tk.X)

    # ── Position rows ──────────────────────────────────────────

    def _build_positions(self):
        tk.Label(self.root, text="  OPEN POSITIONS", fg=GREY, bg=BG,
                 font=self._fs, anchor="w").pack(fill=tk.X, padx=8, pady=(6, 2))

        self._pos_box = tk.Frame(self.root, bg=BG)
        self._pos_box.pack(fill=tk.X, padx=8)

        # Strategy group headers (packed dynamically in _tick)
        self._pos_headers: dict[str, dict] = {}
        for key, colour in [("scalper", SKY), ("trend", PURPLE)]:
            hf = tk.Frame(self._pos_box, bg=BG)
            accent = tk.Frame(hf, bg=colour, width=3)
            accent.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
            accent.pack_propagate(False)
            lbl = tk.Label(hf, text=key.upper(), fg=colour, bg=BG,
                           font=self._fs, anchor="w")
            lbl.pack(side=tk.LEFT)
            self._pos_headers[key] = {"frame": hf, "label": lbl, "colour": colour}

        self._rows: list[dict] = []
        for i in range(self.MAX_POS_ROWS):
            self._rows.append(self._make_row(i))

        self._no_pos_lbl = tk.Label(self._pos_box, text="  No open positions",
                                    fg=GREY, bg=BG, font=self._fs, anchor="w")

        tk.Frame(self.root, bg=GREY, height=1).pack(fill=tk.X, padx=8, pady=(6, 0))

    def _make_row(self, idx) -> dict:
        f = tk.Frame(self._pos_box, bg=BG_CARD, padx=6, pady=3)

        # Strategy tag (S/T)
        stag = tk.Label(f, text="", fg=SKY, bg=BG_CARD, font=self._ft, width=1, anchor="w")
        stag.pack(side=tk.LEFT, padx=(0, 2))

        name = tk.Label(f, text="", fg=WHITE, bg=BG_CARD, font=self._fs, width=13, anchor="w")
        name.pack(side=tk.LEFT)

        pnl = tk.Label(f, text="", fg=GREEN, bg=BG_CARD, font=self._fs, width=7, anchor="e")
        pnl.pack(side=tk.LEFT, padx=(2, 4))

        bar = tk.Canvas(f, width=60, height=12, bg=BG_CARD, highlightthickness=0)
        bar.pack(side=tk.LEFT, padx=(2, 4))

        pnl_abs = tk.Label(f, text="", fg=GREEN, bg=BG_CARD, font=self._fs, width=10, anchor="w")
        pnl_abs.pack(side=tk.LEFT, padx=(0, 2))

        close_btn = tk.Label(f, text=" \u2715 ", fg=GREY, bg=BG_CARD, font=self._fs, cursor="hand2")
        close_btn.pack(side=tk.RIGHT, padx=(4, 0))
        close_btn.bind("<Button-1>", lambda _, i=idx: self._close_position(i))
        close_btn.bind("<Enter>", lambda _, w=close_btn: w.configure(fg=RED))
        close_btn.bind("<Leave>", lambda _, w=close_btn: w.configure(fg=GREY))

        tao = tk.Label(f, text="", fg=GREY, bg=BG_CARD, font=self._fs, anchor="e")
        tao.pack(side=tk.RIGHT)

        # Click anywhere on row -> select
        for w in (f, stag, name, pnl, bar, pnl_abs, tao):
            w.bind("<Button-1>", lambda _, i=idx: self._select_pos(i))

        f.pack_forget()
        return {"frame": f, "stag": stag, "name": name, "pnl": pnl, "bar": bar,
                "pnl_abs": pnl_abs, "tao": tao, "close": close_btn}

    def _select_pos(self, idx):
        s = self.store.snap()
        positions = _strat_positions(s["portfolio"])
        if idx >= len(positions):
            return
        self.selected_netuid = positions[idx]["netuid"]
        # Highlight
        for i, row in enumerate(self._rows):
            bg = BG_HEADER if i == idx else BG_CARD
            row["frame"].configure(bg=bg)
            for k in ("stag", "name", "pnl", "pnl_abs", "tao", "close"):
                row[k].configure(bg=bg)
            row["bar"].configure(bg=bg)
        self._draw_spark(s)

    def _mbox_wrap(self, func, *args, **kwargs):
        """Show a messagebox without it hiding behind the topmost widget."""
        self.root.attributes("-topmost", False)
        try:
            return func(*args, parent=self.root, **kwargs)
        finally:
            if self._topmost:
                self.root.attributes("-topmost", True)

    def _close_position(self, idx):
        s = self.store.snap()
        positions = _strat_positions(s["portfolio"])
        if idx >= len(positions):
            return
        pos = positions[idx]
        pid = pos.get("position_id")
        name = pos.get("name", f"SN{pos['netuid']}")
        strat = pos.get("_strategy", "?")
        if pid is None:
            return

        import tkinter.messagebox as mbox
        ok = self._mbox_wrap(mbox.askyesno, "Close Position",
                             f"Close {name} (SN{pos['netuid']})?\n"
                             f"Strategy: {strat}\n"
                             f"PnL: {pos.get('pnl_pct', 0):+.1f}%")
        if not ok:
            return

        def _do_close():
            try:
                r = requests.post(f"{API_BASE}/api/ema/positions/{pid}/close", timeout=30)
                result = r.json()
                if result.get("success"):
                    self.root.after(0, lambda: self._on_close_ok(pos["netuid"], result["result"]))
                else:
                    detail = result.get("detail", "Unknown error")
                    self.root.after(0, lambda: self._mbox_wrap(mbox.showerror, "Close Failed", detail))
            except Exception as e:
                self.root.after(0, lambda: self._mbox_wrap(mbox.showerror, "Close Failed", str(e)))

        threading.Thread(target=_do_close, daemon=True).start()

    def _on_close_ok(self, netuid, result):
        import tkinter.messagebox as mbox
        pnl = result.get("pnl_pct", 0)
        tao = result.get("pnl_tao", 0)
        self._mbox_wrap(mbox.showinfo, "Position Closed",
                        f"SN{netuid} closed\nPnL: {pnl:+.1f}%  ({tao:+.3f} \u03c4)")
        if self.selected_netuid == netuid:
            self.selected_netuid = None
        self._force_refresh()

    # ── Sparkline chart ────────────────────────────────────────

    def _build_sparkline(self):
        sf = tk.Frame(self.root, bg=BG)
        sf.pack(fill=tk.X, padx=8, pady=4)

        self._fig = Figure(figsize=(4.6, 1.6), dpi=100, facecolor=BG)
        self._ax = self._fig.add_subplot(111)
        self._ax.set_facecolor(BG)
        self._fig.subplots_adjust(left=0.12, right=0.97, top=0.88, bottom=0.18)

        self._mpl = FigureCanvasTkAgg(self._fig, master=sf)
        self._mpl.get_tk_widget().configure(bg=BG, highlightthickness=0)
        self._mpl.get_tk_widget().pack(fill=tk.X)

        self._spark_placeholder()
        tk.Frame(self.root, bg=GREY, height=1).pack(fill=tk.X, padx=8)

    def _spark_placeholder(self, text="Select a position"):
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG)
        ax.text(0.5, 0.5, text, color=GREY, ha="center", va="center",
                fontsize=9, transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_visible(False)
        self._mpl.draw_idle()

    def _draw_spark(self, snap):
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG)

        if self.selected_netuid is None:
            self._spark_placeholder()
            return

        # Find matching signal
        sig = None
        for s in snap.get("signals", []):
            if s.get("netuid") == self.selected_netuid:
                sig = s
                break

        if sig is None or not sig.get("prices"):
            # Fallback: show entry/current price from position data
            positions = _strat_positions(snap.get("portfolio"))
            pos_data = None
            for pos in positions:
                if pos["netuid"] == self.selected_netuid:
                    pos_data = pos
                    break
            if pos_data and pos_data.get("current_price", 0) > 0:
                cur = pos_data["current_price"]
                ep = pos_data.get("entry_price", 0)
                name = pos_data.get("name", f"SN{self.selected_netuid}")
                ax.text(0.5, 0.5, f"Price: {cur:.6f}", color=WHITE, ha="center",
                        va="center", fontsize=9, transform=ax.transAxes)
                if ep > 0:
                    c = GREEN if cur >= ep else RED
                    ax.axhline(y=0.35, color=c, linestyle=":", linewidth=0.8,
                               alpha=0.7, transform=ax.transAxes)
                ax.set_title(f"{name}  {cur:.6f}", fontsize=9, color=WHITE, loc="left", pad=4)
                ax.set_xticks([]); ax.set_yticks([])
                for sp in ax.spines.values():
                    sp.set_visible(False)
                self._mpl.draw_idle()
            else:
                self._spark_placeholder("No price data")
            return

        prices = sig["prices"]
        ema_vals = sig.get("ema_values", [])
        fast_vals = sig.get("fast_ema_values", [])
        n = len(prices)
        xs = list(range(n))

        # Price line
        ax.plot(xs, prices, color=WHITE, linewidth=1.0, alpha=0.9)

        # Slow EMA
        if ema_vals:
            off = n - len(ema_vals)
            ax.plot(list(range(off, n)), ema_vals, color=SKY, linewidth=1.0, alpha=0.8)

        # Fast EMA
        if fast_vals:
            off = n - len(fast_vals)
            ax.plot(list(range(off, n)), fast_vals, color=ORANGE, linewidth=1.0, alpha=0.8)

        # Current price dashed
        cur = sig["price"]
        ax.axhline(y=cur, color=GREY, linestyle="--", linewidth=0.7, alpha=0.6)

        # Entry price dotted (green/red)
        positions = _strat_positions(snap.get("portfolio"))
        for pos in positions:
            if pos["netuid"] == self.selected_netuid:
                ep = pos.get("entry_price", 0)
                if ep > 0:
                    c = GREEN if cur >= ep else RED
                    ax.axhline(y=ep, color=c, linestyle=":", linewidth=0.8, alpha=0.7)
                break

        # Styling
        ax.set_xticks([])
        ax.tick_params(axis="y", labelsize=7, colors=GREY, length=0)
        ax.yaxis.set_major_locator(MaxNLocator(4))
        for sp in ax.spines.values():
            sp.set_visible(False)

        name = sig.get("name", f"SN{self.selected_netuid}")
        ax.set_title(f"{name}  {cur:.6f}", fontsize=9, color=WHITE, loc="left", pad=4)

        # Legend at bottom
        from matplotlib.lines import Line2D
        handles = [
            Line2D([], [], color=WHITE, linewidth=1.0, label="Price"),
            Line2D([], [], color=SKY, linewidth=1.0, label="Slow EMA"),
            Line2D([], [], color=ORANGE, linewidth=1.0, label="Fast EMA"),
            Line2D([], [], color=GREY, linestyle="--", linewidth=0.7, label="Current"),
        ]
        entry_colour = GREEN
        for pos in positions:
            if pos["netuid"] == self.selected_netuid:
                ep = pos.get("entry_price", 0)
                if ep > 0 and cur < ep:
                    entry_colour = RED
                break
        handles.append(Line2D([], [], color=entry_colour, linestyle=":", linewidth=0.8, label="Entry"))
        ax.legend(handles=handles, loc="lower center", ncol=5, fontsize=6,
                  frameon=False, labelcolor=GREY,
                  bbox_to_anchor=(0.5, -0.02))

        self._mpl.draw_idle()

    # ── Recent trades table ────────────────────────────────────

    def _build_trades(self):
        tk.Label(self.root, text="  RECENT TRADES", fg=GREY, bg=BG,
                 font=self._fs, anchor="w").pack(fill=tk.X, padx=8, pady=(6, 0))

        # Column headers
        hdr = tk.Frame(self.root, bg=BG, padx=14)
        hdr.pack(fill=tk.X)
        for txt, anc, w in (("", "w", 1), ("Subnet", "w", 12), ("PnL%", "center", 7),
                             ("TAO", "center", 8), ("Date", "center", 11), ("Exit", "e", 5)):
            tk.Label(hdr, text=txt, fg=GREY, bg=BG, font=self._ft,
                     anchor=anc, width=w).pack(side=tk.LEFT)

        # Trade rows
        self._trades_box = tk.Frame(self.root, bg=BG)
        self._trades_box.pack(fill=tk.X, padx=8)

        self._trade_rows: list[dict] = []
        for i in range(5):
            self._trade_rows.append(self._make_trade_row())

        self._no_trades_lbl = tk.Label(self._trades_box, text="  No closed trades yet",
                                       fg=GREY, bg=BG, font=self._fs, anchor="w")

        tk.Frame(self.root, bg=GREY, height=1).pack(fill=tk.X, padx=8, pady=(4, 0))

    def _make_trade_row(self) -> dict:
        f = tk.Frame(self._trades_box, bg=BG_CARD, padx=6, pady=2)

        stag = tk.Label(f, text="", fg=SKY, bg=BG_CARD, font=self._ft, width=1, anchor="w")
        stag.pack(side=tk.LEFT, padx=(0, 2))

        subnet = tk.Label(f, text="", fg=WHITE, bg=BG_CARD, font=self._ft, anchor="w", width=11)
        subnet.pack(side=tk.LEFT)

        pnl_bar = tk.Canvas(f, width=32, height=10, bg=BG_CARD, highlightthickness=0)
        pnl_bar.pack(side=tk.LEFT, padx=(2, 2))

        pnl = tk.Label(f, text="", fg=GREY, bg=BG_CARD, font=self._ft, anchor="center", width=7)
        pnl.pack(side=tk.LEFT)

        tao = tk.Label(f, text="", fg=GREY, bg=BG_CARD, font=self._ft, anchor="center", width=8)
        tao.pack(side=tk.LEFT)

        date_lbl = tk.Label(f, text="", fg=GREY, bg=BG_CARD, font=self._ft, anchor="center", width=10)
        date_lbl.pack(side=tk.LEFT)

        reason = tk.Label(f, text="", fg=GREY, bg=BG_CARD, font=self._ft, anchor="e", width=5)
        reason.pack(side=tk.LEFT)

        f.pack_forget()
        return {"frame": f, "stag": stag, "subnet": subnet, "pnl_bar": pnl_bar, "pnl": pnl,
                "tao": tao, "reason": reason, "date": date_lbl}

    # ── Status bar ─────────────────────────────────────────────

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=BG, padx=12, pady=4)
        bar.pack(fill=tk.X, side=tk.BOTTOM)

        self._lbl_time = tk.Label(bar, text="Last update: \u2014", fg=GREY, bg=BG,
                                  font=self._ft, anchor="w")
        self._lbl_time.pack(side=tk.LEFT)

        self._lbl_status = tk.Label(bar, text="OFFLINE", fg=RED, bg=BG,
                                    font=self._ft, anchor="e")
        self._lbl_status.pack(side=tk.RIGHT)

    # ── Background polling ─────────────────────────────────────

    def _poll_loop(self):
        while True:
            _fetch(self.store)
            time.sleep(REFRESH_SEC)

    # ── Flash animation ───────────────────────────────────────

    def _flash_row(self, row: dict, colour: str, steps: int = 6):
        """Pulse a row's background from `colour` back to BG_CARD."""
        def _hex(c):
            return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))

        src = _hex(colour)
        dst = _hex(BG_CARD)

        def _step(i):
            if i > steps:
                return
            t = i / steps
            r = int(src[0] + (dst[0] - src[0]) * t)
            g = int(src[1] + (dst[1] - src[1]) * t)
            b = int(src[2] + (dst[2] - src[2]) * t)
            bg = f"#{r:02x}{g:02x}{b:02x}"
            try:
                row["frame"].configure(bg=bg)
                for k in row:
                    if k in ("frame", "bar", "pnl_bar"):
                        continue
                    row[k].configure(bg=bg)
                if "bar" in row:
                    row["bar"].configure(bg=bg)
                if "pnl_bar" in row:
                    row["pnl_bar"].configure(bg=bg)
            except tk.TclError:
                return
            self.root.after(120, _step, i + 1)

        _step(0)

    def _pack_position_row(self, i, positions, portfolio, usd, new_pos_ids):
        """Configure and pack a single position row by flat index."""
        row = self._rows[i]
        pos = positions[i]
        netuid = pos["netuid"]
        strat_key = pos.get("_strategy", "scalper")
        name = pos.get("name", f"SN{netuid}")
        label = f"SN{netuid} {name}"
        if len(label) > 13:
            label = label[:13]

        pnl = pos.get("pnl_pct", 0)
        sign = "+" if pnl >= 0 else ""

        strat_data = (portfolio or {}).get(strat_key, {})
        stop_loss = strat_data.get("stop_loss_pct", 8.0)
        take_profit = strat_data.get("take_profit_pct", 20.0)

        if pnl >= 0:
            fg = GREEN
        elif abs(pnl) >= stop_loss - 1.0:
            fg = YELLOW
        else:
            fg = RED

        tao_amt = pos.get("amount_tao", 0)
        tao_usd_str = f"/${tao_amt * usd:,.0f}" if usd else ""

        pnl_tao = pos.get("pnl_tao")
        if pnl_tao is None:
            pnl_tao = pnl * tao_amt / 100
        pnl_abs_str = f"{sign}{pnl_tao:.2f}\u03c4"
        if usd:
            pnl_usd = pnl_tao * usd
            pnl_abs_str += f"/${abs(pnl_usd):.0f}"

        # Strategy tag
        stag_text = _STRAT_TAG.get(strat_key, "?")
        stag_colour = _STRAT_COLOUR.get(strat_key, GREY)
        row["stag"].configure(text=stag_text, fg=stag_colour)
        row["name"].configure(text=label)
        row["pnl"].configure(text=f"{sign}{pnl:.1f}%", fg=fg)
        row["pnl_abs"].configure(text=pnl_abs_str, fg=fg)
        row["tao"].configure(text=f"{tao_amt:.1f}\u03c4{tao_usd_str}")

        # PnL bar
        cv = row["bar"]
        cv.delete("all")
        bw, bh = 60, 10
        cv.create_rectangle(0, 1, bw, bh + 1, fill=BG, outline=GREY, width=1)
        if pnl >= 0:
            fw = int(min(pnl / take_profit, 1.0) * bw)
            if fw > 0:
                cv.create_rectangle(1, 2, fw, bh, fill=GREEN, outline="")
        else:
            fw = int(min(abs(pnl) / stop_loss, 1.0) * bw)
            if fw > 0:
                cv.create_rectangle(1, 2, fw, bh, fill=RED, outline="")

        # Highlight selected
        is_sel = (netuid == self.selected_netuid)
        bg = BG_HEADER if is_sel else BG_CARD
        row["frame"].configure(bg=bg)
        for k in ("stag", "name", "pnl", "pnl_abs", "tao", "close"):
            row[k].configure(bg=bg)
        cv.configure(bg=bg)

        row["frame"].pack(fill=tk.X, pady=1)

        # Flash new positions green
        pid_key = f"{strat_key}_{pos.get('position_id', netuid)}"
        if pid_key in new_pos_ids:
            self._flash_row(row, GREEN)

        # Auto-select first position
        if self.selected_netuid is None and i == 0:
            self.selected_netuid = netuid

    # ── UI refresh (runs on Tk main thread via after()) ────────

    def _tick(self):
        s = self.store.snap()

        # Title-bar indicators
        online = s["bot_online"]
        self._dot.configure(fg=GREEN if online else RED)

        # Summary
        p = s["portfolio"]
        usd = s["tao_usd"]

        def _usd(tao: float) -> str:
            return f" (${tao * usd:,.0f})" if usd else ""

        # Wallet balance from combined
        if p:
            wb = (p.get("combined") or {}).get("wallet_balance")
            if wb is not None:
                self._lbl_wallet.configure(text=f"Wallet  {wb:.2f} \u03c4{_usd(wb)}")
            else:
                self._lbl_wallet.configure(text="Wallet  \u2014")
        else:
            self._lbl_wallet.configure(text="Wallet  \u2014")

        self._lbl_usd.configure(text=f"TAO/USD  ${usd:,.2f}" if usd else "TAO/USD  \u2014")

        # Per-strategy summary rows
        for key in ("scalper", "trend"):
            row = self._strat_rows[key]
            strat = (p or {}).get(key, {})
            enabled = strat.get("enabled", False)

            if not enabled:
                row["badge"].configure(text="[off]", fg=GREY)
                row["ema"].configure(text="")
                row["pot"].configure(text="")
                row["dep"].configure(text="")
                row["slots"].configure(text="")
                continue

            dry = strat.get("dry_run", True)
            row["badge"].configure(
                text="[live]" if not dry else "[dry]",
                fg=GREEN if not dry else YELLOW,
            )

            # Show EMA periods
            fast = strat.get("signal_timeframe_hours", "")
            # Try to extract EMA periods from the data
            # The portfolio response includes confirm_bars, signal_timeframe_hours
            # but not EMA periods directly - we get those from signals endpoint
            ema_text = ""
            for sig_strat in (s.get("strategies") or []):
                if sig_strat.get("tag") == key:
                    ema_text = f"{sig_strat['fast']}/{sig_strat['slow']}"
                    break
            row["ema"].configure(text=ema_text)

            pot = strat.get("pot_tao", 0)
            dep = strat.get("deployed_tao", 0)
            oc = strat.get("open_count", 0)
            mx = strat.get("max_positions", 0)
            row["pot"].configure(text=f"Pot {pot:.1f}\u03c4")
            row["dep"].configure(text=f"Dep {dep:.1f}\u03c4")
            row["slots"].configure(text=f"{oc}/{mx}")

        # Position rows — merged from both strategies
        positions = _strat_positions(p)

        # Detect new / closed positions for flash
        cur_pos_ids = {f"{pos.get('_strategy', '')}_{pos.get('position_id', pos['netuid'])}" for pos in positions}
        new_pos_ids = cur_pos_ids - self._prev_pos_ids if not self._first_tick else set()
        self._prev_pos_ids = cur_pos_ids

        # Deselect if selected position closed
        if self.selected_netuid and not any(
            pos["netuid"] == self.selected_netuid for pos in positions
        ):
            self.selected_netuid = None

        has_positions = len(positions) > 0

        # Unpack everything so we can repack in strategy-grouped order
        for hdr in self._pos_headers.values():
            hdr["frame"].pack_forget()
        for row in self._rows:
            row["frame"].pack_forget()
        self._no_pos_lbl.pack_forget()

        if has_positions:
            # Split positions by strategy for grouped headers
            scalper_indices = [i for i, pos in enumerate(positions) if pos.get("_strategy", "scalper") == "scalper"]
            trend_indices = [i for i, pos in enumerate(positions) if pos.get("_strategy") == "trend"]

            # Update strategy header labels with count
            for key, indices in [("scalper", scalper_indices), ("trend", trend_indices)]:
                hdr = self._pos_headers[key]
                ema_text = ""
                for sig_strat in (s.get("strategies") or []):
                    if sig_strat.get("tag") == key:
                        ema_text = f" ({sig_strat['fast']}/{sig_strat['slow']})"
                        break
                hdr["label"].configure(text=f"{key.upper()}{ema_text}  \u2014  {len(indices)} open")

            # Pack scalper group then trend group
            for key, indices in [("scalper", scalper_indices), ("trend", trend_indices)]:
                if not indices:
                    continue
                self._pos_headers[key]["frame"].pack(fill=tk.X, pady=(6, 2))
                for i in indices:
                    self._pack_position_row(i, positions, p, usd, new_pos_ids)

        else:
            self._no_pos_lbl.pack(fill=tk.X, pady=4)

        # Sparkline
        self._draw_spark(s)

        # Recent trades
        trades = s.get("trades", [])
        has_trades = len(trades) > 0

        # Detect new trades for flash
        cur_trade_ids = set()
        for t in trades:
            tid = t.get("position_id") or t.get("id") or f"{t.get('netuid',0)}_{t.get('exit_ts','')}"
            cur_trade_ids.add(tid)
        new_trade_ids = cur_trade_ids - self._prev_trade_ids if not self._first_tick else set()
        self._prev_trade_ids = cur_trade_ids

        # Find max abs PnL for bar scaling
        max_abs_pnl = max((abs(t.get("pnl_pct") or 0) for t in trades), default=1.0) or 1.0

        for i, row in enumerate(self._trade_rows):
            if i < len(trades):
                t = trades[i]
                netuid = t.get("netuid", 0)
                name = t.get("name", f"SN{netuid}")
                label = f"SN{netuid} {name}"
                if len(label) > 11:
                    label = label[:11]

                trade_strat = t.get("strategy", "")
                stag_text = _STRAT_TAG.get(trade_strat, "?")
                stag_colour = _STRAT_COLOUR.get(trade_strat, GREY)

                pnl_pct = t.get("pnl_pct") or 0
                pnl_tao = t.get("pnl_tao") or 0
                sign = "+" if pnl_pct >= 0 else ""
                fg = GREEN if pnl_pct >= 0 else RED

                reason = t.get("exit_reason", "") or ""
                _reason_map = {
                    "TRAILING_STOP": "trail",
                    "STOP_LOSS":     "SL",
                    "TAKE_PROFIT":   "TP",
                    "TIME_STOP":     "time",
                    "EMA_CROSS":     "EMA\u00d7",
                    "MANUAL_CLOSE":  "man",
                }
                reason_short = _reason_map.get(reason, reason[:5])

                raw_dt = t.get("exit_ts") or t.get("closed_at") or t.get("exit_time") or ""
                if raw_dt:
                    try:
                        dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                        dt_str = dt.strftime("%m-%d %H:%M")
                    except Exception:
                        dt_str = raw_dt[:11]
                else:
                    dt_str = ""

                row["stag"].configure(text=stag_text, fg=stag_colour)
                row["subnet"].configure(text=label)
                row["pnl"].configure(text=f"{sign}{pnl_pct:.1f}%", fg=fg)
                row["tao"].configure(text=f"{pnl_tao:+.2f}\u03c4", fg=fg)
                row["reason"].configure(text=reason_short, fg=GREY)
                row["date"].configure(text=dt_str, fg=GREY)

                # PnL magnitude bar
                cv = row["pnl_bar"]
                cv.delete("all")
                bw, bh = 32, 8
                ratio = min(abs(pnl_pct) / max_abs_pnl, 1.0)
                fw = max(int(ratio * bw), 2)
                bar_col = GREEN if pnl_pct >= 0 else RED
                cv.create_rectangle(0, 1, fw, bh + 1, fill=bar_col, outline="")

                row["frame"].pack(fill=tk.X, pady=1)

                # Flash new trades red (closed position)
                tid = t.get("position_id") or t.get("id") or f"{netuid}_{raw_dt}"
                if tid in new_trade_ids:
                    self._flash_row(row, RED)
            else:
                row["frame"].pack_forget()

        if has_trades:
            self._no_trades_lbl.pack_forget()
        else:
            self._no_trades_lbl.pack(fill=tk.X, pady=4)

        # Daily stats
        daily = s.get("daily_trades", [])
        if daily:
            count = len(daily)
            total_pnl = sum(t.get("pnl_tao") or 0 for t in daily)
            wins = sum(1 for t in daily if (t.get("pnl_tao") or 0) > 0)
            wr = int(wins / count * 100) if count else 0
            fg_pnl = GREEN if total_pnl >= 0 else RED
            txt = f"Today: {count} trade{'s' if count != 1 else ''}  {total_pnl:+.2f}\u03c4  {wr}% win"
            self._lbl_daily.configure(text=txt, fg=fg_pnl)
        else:
            self._lbl_daily.configure(text="Today: no trades", fg=GREY)

        # Mark first tick done
        self._first_tick = False

        # Status bar
        self._lbl_time.configure(text=f"Last update: {s['last_ok'] or '\u2014'}")
        if online:
            self._lbl_status.configure(text="BOT RUNNING", fg=GREEN)
        else:
            self._lbl_status.configure(text="OFFLINE", fg=RED)

        # Schedule next tick
        self.root.after(REFRESH_SEC * 1000, self._tick)

    # ── Run ────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
