"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

const API =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8081`
    : "http://localhost:8081");

export default function Home() {
  const router = useRouter();

  useEffect(() => {
    fetch(`${API}/api/config/status`)
      .then((r) => r.json())
      .then((data) => {
        router.replace(data.setup_complete ? "/ema" : "/setup");
      })
      .catch(() => {
        // Backend unreachable — go to EMA page (will show error state)
        router.replace("/ema");
      });
  }, [router]);

  return (
    <div className="flex items-center justify-center h-64">
      <div className="h-8 w-8 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin" />
    </div>
  );
}
