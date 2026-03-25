"use client";

import { useStrategies } from "@/lib/hooks";
import Loader from "@/components/ui/Loader";
import EmptyState from "@/components/ui/EmptyState";
import { Trophy } from "lucide-react";

export default function StrategiesPage() {
  const { data, isLoading } = useStrategies();

  if (isLoading) return <Loader />;
  if (!data?.strategies.length) return <EmptyState message="No strategies data yet." />;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Strategy Comparison</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {data.strategies.map((s) => (
          <div key={s.strategy} className="card space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {s.rank === 1 && <Trophy size={16} className="text-warning" />}
                <h3 className="font-medium text-white">{s.strategy}</h3>
              </div>
              <span className="text-xs text-gray-500">Rank #{s.rank}</span>
            </div>

            <div className="grid grid-cols-2 gap-3 text-sm">
              <Metric label="Total Trades" value={s.total_trades} />
              <Metric label="Closed" value={s.closed_trades} />
              <Metric
                label="Win Rate"
                value={`${s.win_rate}%`}
                color={s.win_rate >= 50 ? "text-success" : "text-danger"}
              />
              <Metric
                label="Total PnL"
                value={`$${s.total_pnl.toFixed(2)}`}
                color={s.total_pnl >= 0 ? "text-success" : "text-danger"}
              />
              <Metric label="Winning" value={s.winning} color="text-success" />
              <Metric label="Losing" value={s.losing} color="text-danger" />
              <Metric label="Max Drawdown" value={`$${s.max_drawdown.toFixed(2)}`} color="text-danger" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  color = "text-white",
}: {
  label: string;
  value: string | number;
  color?: string;
}) {
  return (
    <div>
      <p className={`font-mono text-base ${color}`}>{value}</p>
      <p className="text-xs text-gray-500">{label}</p>
    </div>
  );
}
