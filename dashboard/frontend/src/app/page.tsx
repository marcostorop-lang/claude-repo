"use client";

import { useOverview } from "@/lib/hooks";
import StatCard from "@/components/ui/StatCard";
import Loader from "@/components/ui/Loader";
import {
  DollarSign,
  TrendingUp,
  TrendingDown,
  BarChart3,
  Crosshair,
  Store,
  Activity,
  Wallet,
} from "lucide-react";

export default function HomePage() {
  const { data, error, isLoading } = useOverview();

  if (isLoading) return <Loader />;
  if (error) return <div className="card text-danger">Failed to load overview data.</div>;
  if (!data) return null;

  const pnlTrend = data.total_pnl >= 0 ? "positive" : "negative";
  const dailyTrend = data.daily_pnl >= 0 ? "positive" : "negative";

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
          icon={<DollarSign size={20} />}
          trend={pnlTrend}
        />
        <StatCard
          label="Daily PnL"
          value={`$${data.daily_pnl.toFixed(2)}`}
          icon={data.daily_pnl >= 0 ? <TrendingUp size={20} /> : <TrendingDown size={20} />}
          trend={dailyTrend}
        />
        <StatCard
          label="Simulated Balance"
          value={`$${data.simulated_balance.toFixed(2)}`}
          icon={<Wallet size={20} />}
        />
        <StatCard
          label="Current Exposure"
          value={`$${data.current_exposure.toFixed(2)}`}
          icon={<BarChart3 size={20} />}
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Total Trades"
          value={data.total_trades}
          icon={<Activity size={20} />}
        />
        <StatCard
          label="Winning Trades"
          value={data.winning_trades}
          trend="positive"
        />
        <StatCard
          label="Losing Trades"
          value={data.losing_trades}
          trend="negative"
        />
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
        <StatCard
          label="Markets Monitored"
          value={data.markets_monitored}
          icon={<Store size={20} />}
        />
        <StatCard
          label="Open Positions"
          value={data.open_positions}
          icon={<Crosshair size={20} />}
        />
      </div>
    </div>
  );
}
