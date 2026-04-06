import { getOverview } from "@/lib/data";
import StatCard from "@/components/ui/StatCard";

export const dynamic = "force-dynamic";

export default function HomePage() {
  const data = getOverview();

  const pnlTrend = data.total_pnl >= 0 ? "positive" : ("negative" as const);
  const dailyTrend = data.daily_pnl >= 0 ? "positive" : ("negative" as const);

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <div
          className={`w-2.5 h-2.5 rounded-full ${
            data.bot_active ? "bg-success animate-pulse" : "bg-danger"
          }`}
        />
        <h2 className="text-lg font-semibold">
          Bot {data.bot_active ? "Active" : "Stopped"}
        </h2>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Total PnL"
          value={`$${data.total_pnl.toFixed(2)}`}
          trend={pnlTrend}
        />
        <StatCard
          label="Daily PnL"
          value={`$${data.daily_pnl.toFixed(2)}`}
          trend={dailyTrend}
        />
        <StatCard
          label="Simulated Balance"
          value={`$${data.simulated_balance.toFixed(2)}`}
        />
        <StatCard
          label="Current Exposure"
          value={`$${data.current_exposure.toFixed(2)}`}
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Total Trades" value={data.total_trades} />
        <StatCard label="Winning Trades" value={data.winning_trades} trend="positive" />
        <StatCard label="Losing Trades" value={data.losing_trades} trend="negative" />
        <StatCard
          label="Win Rate"
          value={
            data.winning_trades + data.losing_trades > 0
              ? `${((data.winning_trades / (data.winning_trades + data.losing_trades)) * 100).toFixed(1)}%`
              : "N/A"
          }
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <StatCard label="Markets Monitored" value={data.markets_monitored} />
        <StatCard label="Open Positions" value={data.open_positions} />
      </div>
    </div>
  );
}
