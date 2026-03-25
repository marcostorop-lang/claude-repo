"use client";

import { useConfig } from "@/lib/hooks";
import Loader from "@/components/ui/Loader";
import EmptyState from "@/components/ui/EmptyState";
import { Lock } from "lucide-react";

const LABELS: Record<string, string> = {
  TRADING_MODE: "Trading Mode",
  ALLOW_LIVE_TRADING: "Live Trading Enabled",
  POLL_INTERVAL_SECONDS: "Poll Interval (s)",
  STRATEGY: "Active Strategy",
  MIN_VOLUME: "Min Volume",
  MIN_LIQUIDITY: "Min Liquidity",
  MAX_SPREAD: "Max Spread",
  MAX_MARKETS: "Max Markets",
  MAX_POSITION_SIZE: "Max Position Size ($)",
  MAX_TOTAL_EXPOSURE: "Max Total Exposure ($)",
  STOP_LOSS_PCT: "Stop Loss (%)",
  TAKE_PROFIT_PCT: "Take Profit (%)",
  MAX_OPEN_POSITIONS: "Max Open Positions",
  MOMENTUM_WINDOW: "Momentum Window",
  MOMENTUM_THRESHOLD: "Momentum Threshold",
  MEAN_REVERSION_WINDOW: "Mean Reversion Window",
  MEAN_REVERSION_ENTRY_Z: "MR Entry Z-Score",
  MEAN_REVERSION_EXIT_Z: "MR Exit Z-Score",
  LOG_LEVEL: "Log Level",
};

const GROUPS: Record<string, string[]> = {
  "Trading": ["TRADING_MODE", "ALLOW_LIVE_TRADING"],
  "Bot Loop": ["POLL_INTERVAL_SECONDS", "STRATEGY", "LOG_LEVEL"],
  "Market Filters": ["MIN_VOLUME", "MIN_LIQUIDITY", "MAX_SPREAD", "MAX_MARKETS"],
  "Risk Management": ["MAX_POSITION_SIZE", "MAX_TOTAL_EXPOSURE", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "MAX_OPEN_POSITIONS"],
  "Momentum Strategy": ["MOMENTUM_WINDOW", "MOMENTUM_THRESHOLD"],
  "Mean Reversion Strategy": ["MEAN_REVERSION_WINDOW", "MEAN_REVERSION_ENTRY_Z", "MEAN_REVERSION_EXIT_Z"],
};

export default function ConfigPage() {
  const { data, isLoading } = useConfig();

  if (isLoading) return <Loader />;
  if (!data) return <EmptyState message="Config not available." />;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <h2 className="text-lg font-semibold">Configuration</h2>
        <Lock size={14} className="text-gray-500" />
        <span className="text-xs text-gray-500">Read-only</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {Object.entries(GROUPS).map(([group, keys]) => (
          <div key={group} className="card space-y-3">
            <h3 className="text-sm font-medium text-gray-300 border-b border-surface-border pb-2">
              {group}
            </h3>
            {keys.map((k) => (
              <div key={k} className="flex justify-between text-sm">
                <span className="text-gray-400">{LABELS[k] || k}</span>
                <span className="font-mono text-white">
                  {highlight(k, data.config[k] ?? "—")}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function highlight(key: string, value: string) {
  if (key === "TRADING_MODE") {
    return (
      <span className={value === "paper" ? "text-blue-400" : "text-danger"}>
        {value.toUpperCase()}
      </span>
    );
  }
  if (key === "ALLOW_LIVE_TRADING") {
    return (
      <span className={value === "true" ? "text-danger" : "text-success"}>
        {value}
      </span>
    );
  }
  return value;
}
