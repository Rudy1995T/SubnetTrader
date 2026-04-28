"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL || (typeof window !== "undefined" ? `http://${window.location.hostname}:8081` : "http://localhost:8081");
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface ControlStatus {
  ema_dry_run: boolean;
  flow_enabled: boolean;
  flow_dry_run: boolean;
}

const baseLinks = [
  { href: "/ema", label: "EMA" },
  { href: "/control", label: "Control" },
  { href: "/settings", label: "\u2699 Settings" },
];

export default function NavBar() {
  const pathname = usePathname();
  const { data: status } = useSWR<ControlStatus>(
    `${API}/api/control/status`,
    fetcher,
    { refreshInterval: 30000 }
  );

  const emaIsLive = status ? !status.ema_dry_run : false;
  const flowIsLive = status ? !status.flow_dry_run : false;
  const flowEnabled = status?.flow_enabled ?? false;

  const links = flowEnabled
    ? [baseLinks[0], { href: "/flow", label: "Flow" }, ...baseLinks.slice(1)]
    : baseLinks;

  return (
    <nav className="sticky top-0 z-50 bg-gray-900 border-b border-gray-800 px-4 md:px-6 py-3 flex items-center gap-4 md:gap-6 overflow-x-auto scrollbar-hide">
      <span className="font-bold text-indigo-400 text-lg shrink-0">SubnetTrader</span>
      {links.map((l) => {
        const isActive = pathname.startsWith(l.href);
        const liveHref =
          l.href === "/ema" ? emaIsLive : l.href === "/flow" ? flowIsLive : false;
        const dotColour =
          l.href === "/flow" ? "bg-orange-400" : "bg-emerald-400";
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
            {liveHref && (
              <span className="relative flex h-2 w-2">
                <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${dotColour} opacity-75`} />
                <span className={`relative inline-flex rounded-full h-2 w-2 ${dotColour}`} />
              </span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}
