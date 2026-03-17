"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL || (typeof window !== "undefined" ? `http://${window.location.hostname}:8080` : "http://localhost:8080");
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface ControlStatus {
  ema_dry_run: boolean;
}

const links = [
  { href: "/ema", label: "EMA" },
  { href: "/control", label: "Control" },
];

export default function NavBar() {
  const pathname = usePathname();
  const { data: status } = useSWR<ControlStatus>(
    `${API}/api/control/status`,
    fetcher,
    { refreshInterval: 30000 }
  );

  const emaIsLive = status ? !status.ema_dry_run : false;

  return (
    <nav className="sticky top-0 z-50 bg-gray-900 border-b border-gray-800 px-4 md:px-6 py-3 flex items-center gap-4 md:gap-6 overflow-x-auto scrollbar-hide">
      <span className="font-bold text-indigo-400 text-lg shrink-0">EMA Live</span>
      {links.map((l) => {
        const isEma = l.href === "/ema";
        const isActive = pathname.startsWith(l.href);
        return (
          <Link
            key={l.href}
            href={l.href}
            className={`text-sm font-medium transition-colors flex items-center gap-1.5 whitespace-nowrap ${
              isActive
                ? "text-white border-b-2 border-indigo-400 pb-0.5"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {l.label}
            {isEma && emaIsLive && (
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400" />
              </span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}
