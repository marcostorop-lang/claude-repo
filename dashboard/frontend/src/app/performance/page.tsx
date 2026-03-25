"use client";

import { usePerformance } from "@/lib/hooks";
import Loader from "@/components/ui/Loader";
import EmptyState from "@/components/ui/EmptyState";
import StatCard from "@/components/ui/StatCard";
import EquityCurve from "@/components/charts/EquityCurve";
import DailyPnlChart from "@/components/charts/DailyPnlChart";
import WinLossDonut from "@/components/charts/WinLossDonut";
import { exportCSV } from "@/lib/api";
import { Download } from "lucide-react";

export default function PerformancePage() {
  const { data, isLoading } = usePerformance();

  if (isLoading) return <Loader />;
  if (!data || data.total_closed === 0) return <EmptyState message="No closed trades yet." />;

  const handleExport = () => {
    exportCSV(
      [
        {
          metric: "Total PnL",
          value: data.total_pnl,
        },
        { metric: "Win Rate (%)", value: data.win_rate },
        { metric: "Profit Factor", value: data.profit_factor },
        { metric: "Max Drawdown", value: data.max_drawdown },
        { metric: "Avg Win", value: data.avg_win },
        { metric: "Avg Loss", value: data.avg_loss },
        { metric: "Reward/Risk", value: data.reward_risk_ratio },
        { metric: "Total Closed", value: data.total_closed },
      ],
      "performance_summary.csv"
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Performance</h2>
        <button
          onClick={handleExport}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-surface-card border border-surface-border text-sm hover:bg-surface-hover transition-colors"
        >
          <Download size={14} /> Export Summary
        </button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <StatCard
          label="Total PnL"
          value={`$${data.total_pnl.toFixed(2)}`}
          trend={data.total_pnl >= 0 ? "positive" : "negative"}
        />
        <StatCard label="Win Rate" value={`${data.win_rate}%`} />
        <StatCard label="Profit Factor" value={data.profit_factor === Infinity ? "Inf" : data.profit_factor.toFixed(2)} />
        <StatCard
          label="Max Drawdown"
          value={`$${data.max_drawdown.toFixed(2)}`}
          trend="negative"
        />
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <StatCard label="Avg Win" value={`$${data.avg_win.toFixed(2)}`} trend="positive" />
        <StatCard label="Avg Loss" value={`$${data.avg_loss.toFixed(2)}`} trend="negative" />
        <StatCard label="Reward/Risk" value={data.reward_risk_ratio.toFixed(2)} />
        <StatCard label="Closed Trades" value={data.total_closed} />
      </div>

      <EquityCurve data={data.equity_curve} />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <DailyPnlChart data={data.daily_pnl} />
        <WinLossDonut winning={data.winning} losing={data.losing} />
      </div>
    </div>
  );
}
