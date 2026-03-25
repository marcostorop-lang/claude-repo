import useSWR from "swr";
import { apiFetch } from "./api";
import type {
  Overview,
  TradesResponse,
  Performance,
  Position,
  MarketStats,
  StrategyStats,
  LogEntry,
  BotConfig,
} from "@/types";

const REFRESH = 10_000; // 10 seconds

function fetcher<T>(path: string) {
  return apiFetch<T>(path);
}

export function useOverview() {
  return useSWR<Overview>("/api/overview", fetcher, { refreshInterval: REFRESH });
}

export function useTrades(params: Record<string, string> = {}) {
  const qs = new URLSearchParams(params).toString();
  const key = `/api/trades?${qs}`;
  return useSWR<TradesResponse>(key, () => apiFetch<TradesResponse>("/api/trades", params), {
    refreshInterval: REFRESH,
  });
}

export function usePerformance() {
  return useSWR<Performance>("/api/performance", fetcher, { refreshInterval: REFRESH });
}

export function usePositions() {
  return useSWR<{ positions: Position[] }>("/api/positions", fetcher, { refreshInterval: REFRESH });
}

export function useMarkets(search?: string) {
  const key = `/api/markets?search=${search || ""}`;
  return useSWR<{ markets: MarketStats[] }>(key, () =>
    apiFetch<{ markets: MarketStats[] }>("/api/markets", search ? { search } : {}), {
    refreshInterval: REFRESH,
  });
}

export function useStrategies() {
  return useSWR<{ strategies: StrategyStats[] }>("/api/strategies", fetcher, {
    refreshInterval: REFRESH,
  });
}

export function useLogs(level?: string) {
  const key = `/api/logs?level=${level || ""}`;
  return useSWR<{ logs: LogEntry[] }>(key, () =>
    apiFetch<{ logs: LogEntry[] }>("/api/logs", level ? { level } : {}), {
    refreshInterval: REFRESH,
  });
}

export function useConfig() {
  return useSWR<{ config: BotConfig }>("/api/config", fetcher);
}
