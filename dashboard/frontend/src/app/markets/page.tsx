"use client";

import { useState } from "react";
import { useMarkets } from "@/lib/hooks";
import Loader from "@/components/ui/Loader";
import EmptyState from "@/components/ui/EmptyState";
import { Search } from "lucide-react";

export default function MarketsPage() {
  const [search, setSearch] = useState("");
  const { data, isLoading } = useMarkets(search);

  if (isLoading) return <Loader />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Markets</h2>
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            placeholder="Search markets..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9 pr-4 py-1.5 bg-surface-card border border-surface-border rounded-lg text-sm focus:outline-none focus:border-brand-500"
          />
        </div>
      </div>

      {!data?.markets.length ? (
        <EmptyState message="No markets found." />
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-gray-400 text-xs uppercase">
                <th className="px-4 py-3 text-left">Market</th>
                <th className="px-4 py-3 text-right">Trades</th>
                <th className="px-4 py-3 text-right">PnL</th>
                <th className="px-4 py-3 text-right">Win Rate</th>
                <th className="px-4 py-3 text-left">Strategies</th>
              </tr>
            </thead>
            <tbody>
              {data.markets.map((m) => (
                <tr key={m.condition_id} className="border-b border-surface-border/50 hover:bg-surface-hover/50">
                  <td className="px-4 py-3 max-w-[350px] truncate" title={m.question}>
                    {m.question}
                  </td>
                  <td className="px-4 py-3 text-right font-mono">{m.total_trades}</td>
                  <td
                    className={`px-4 py-3 text-right font-mono ${
                      m.pnl >= 0 ? "pnl-positive" : "pnl-negative"
                    }`}
                  >
                    ${m.pnl.toFixed(2)}
                  </td>
                  <td className="px-4 py-3 text-right">{m.win_rate}%</td>
                  <td className="px-4 py-3 text-gray-300 text-xs">{m.strategies}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
