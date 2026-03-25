"use client";

import { usePositions } from "@/lib/hooks";
import Loader from "@/components/ui/Loader";
import EmptyState from "@/components/ui/EmptyState";

export default function PositionsPage() {
  const { data, isLoading } = usePositions();

  if (isLoading) return <Loader />;
  if (!data?.positions.length) return <EmptyState message="No open positions." />;

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">Open Positions</h2>
      <div className="card overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-surface-border text-gray-400 text-xs uppercase">
              <th className="px-4 py-3 text-left">Market</th>
              <th className="px-4 py-3 text-left">Side</th>
              <th className="px-4 py-3 text-right">Entry</th>
              <th className="px-4 py-3 text-right">Current</th>
              <th className="px-4 py-3 text-right">Size</th>
              <th className="px-4 py-3 text-right">Unrealised PnL</th>
              <th className="px-4 py-3 text-left">Strategy</th>
              <th className="px-4 py-3 text-right">SL %</th>
              <th className="px-4 py-3 text-right">TP %</th>
              <th className="px-4 py-3 text-left">Opened</th>
            </tr>
          </thead>
          <tbody>
            {data.positions.map((p, i) => (
              <tr key={i} className="border-b border-surface-border/50 hover:bg-surface-hover/50">
                <td className="px-4 py-3 max-w-[220px] truncate" title={p.question}>
                  {p.question || p.token_id.slice(0, 12)}
                </td>
                <td className="px-4 py-3">
                  <span className={p.side === "BUY" ? "badge-buy" : "badge-sell"}>{p.side}</span>
                </td>
                <td className="px-4 py-3 text-right font-mono">{p.entry_price.toFixed(4)}</td>
                <td className="px-4 py-3 text-right font-mono">{p.current_price.toFixed(4)}</td>
                <td className="px-4 py-3 text-right font-mono">{p.size.toFixed(2)}</td>
                <td
                  className={`px-4 py-3 text-right font-mono ${
                    p.unrealised_pnl >= 0 ? "pnl-positive" : "pnl-negative"
                  }`}
                >
                  ${p.unrealised_pnl.toFixed(2)}
                </td>
                <td className="px-4 py-3 text-gray-300">{p.strategy}</td>
                <td className="px-4 py-3 text-right">
                  <ProgressBar value={p.pct_to_stop_loss} color="danger" />
                </td>
                <td className="px-4 py-3 text-right">
                  <ProgressBar value={p.pct_to_take_profit} color="success" />
                </td>
                <td className="px-4 py-3 text-gray-400 whitespace-nowrap">
                  {new Date(p.entry_time).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ProgressBar({ value, color }: { value: number; color: "success" | "danger" }) {
  const bg = color === "success" ? "bg-success" : "bg-danger";
  return (
    <div className="flex items-center gap-2">
      <div className="w-16 h-1.5 bg-surface-border rounded-full overflow-hidden">
        <div className={`h-full ${bg} rounded-full`} style={{ width: `${Math.min(100, value)}%` }} />
      </div>
      <span className="text-xs text-gray-400">{value.toFixed(0)}%</span>
    </div>
  );
}
