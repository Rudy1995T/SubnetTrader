"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/positions", label: "Positions" },
  { href: "/equity", label: "Equity" },
  { href: "/signals", label: "Signals" },
  { href: "/explore", label: "Explore" },
  { href: "/backtest", label: "Backtest" },
  { href: "/terminal", label: "Terminal" },
  { href: "/tuner", label: "Tuner" },
  { href: "/correlation", label: "Correlations" },
  { href: "/control", label: "Control" },
];

export default function NavBar() {
  const pathname = usePathname();

  return (
    <nav className="sticky top-0 z-50 bg-gray-900 border-b border-gray-800 px-6 py-3 flex items-center gap-6">
      <span className="font-bold text-indigo-400 text-lg mr-4">⚡ SubnetTrader</span>
      {links.map((l) => (
        <Link
          key={l.href}
          href={l.href}
          className={`text-sm font-medium transition-colors ${
            pathname.startsWith(l.href)
              ? "text-white border-b-2 border-indigo-400 pb-0.5"
              : "text-gray-400 hover:text-gray-200"
          }`}
        >
          {l.label}
        </Link>
      ))}
    </nav>
  );
}
