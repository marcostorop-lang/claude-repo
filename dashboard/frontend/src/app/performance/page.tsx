import { getPerformance } from "@/lib/data";
import StatCard from "@/components/ui/StatCard";
import EquityCurve from "@/components/charts/EquityCurve";
import DailyPnlChart from "@/components/charts/DailyPnlChart";
import WinLossDonut from "@/components/charts/WinLossDonut";

export const dynamic = "force-dynamic";

export default function PerformancePage() {
  const data = getPerformance();

  if (data.total_closed === 0) {
    return <div className="card text-center text-gray-500 py-8">No closed trades yet.</div>;
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Performance</h2>

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
