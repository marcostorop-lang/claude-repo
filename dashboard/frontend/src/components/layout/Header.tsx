"use client";

import { useOverview } from "@/lib/hooks";
import { Activity, Wifi, WifiOff } from "lucide-react";

export default function Header() {
  const { data, error, isValidating } = useOverview();
  const connected = !!data && !error;

  return (
    <header className="h-14 shrink-0 border-b border-surface-border flex items-center justify-between px-6">
      <h1 className="text-sm font-medium text-gray-300">Dashboard</h1>
      <div className="flex items-center gap-4 text-xs">
        {isValidating && (
          <span className="flex items-center gap-1.5 text-gray-500">
            <Activity size={12} className="animate-pulse" />
            Syncing...
          </span>
        )}
        <span
          className={`flex items-center gap-1.5 ${
            connected ? "text-success" : "text-danger"
          }`}
        >
          {connected ? <Wifi size={14} /> : <WifiOff size={14} />}
          {connected ? "Connected" : "Disconnected"}
        </span>
        {data?.last_update && (
          <span className="text-gray-500">
            Last: {new Date(data.last_update).toLocaleTimeString()}
          </span>
        )}
      </div>
    </header>
  );
}
