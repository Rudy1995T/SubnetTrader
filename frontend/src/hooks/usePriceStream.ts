"use client";

import { useState, useEffect, useRef } from "react";

export interface PriceEntry {
  price: number;
  tao_in_pool: number;
  alpha_in_pool: number;
}

export interface PriceUpdate {
  ts: string;
  prices: Record<number, PriceEntry>;
}

export function usePriceStream(): PriceUpdate | null {
  const [data, setData] = useState<PriceUpdate | null>(null);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const api =
      process.env.NEXT_PUBLIC_API_URL ||
      `http://${window.location.hostname}:8081`;
    const es = new EventSource(`${api}/api/prices`);
    esRef.current = es;

    es.onmessage = (event) => {
      try {
        setData(JSON.parse(event.data));
      } catch {
        // ignore parse errors
      }
    };

    es.onerror = () => {
      console.warn("Price stream disconnected, reconnecting...");
    };

    return () => es.close();
  }, []);

  return data;
}
