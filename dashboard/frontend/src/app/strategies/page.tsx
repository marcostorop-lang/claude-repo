import { getStrategies } from "@/lib/data";

export const dynamic = "force-dynamic";

export default function StrategiesPage() {
  const strategies = getStrategies();

  if (!strategies.length) {
    return <div className="card text-center text-gray-500 py-8">No strategies data yet.</div>;
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Strategy Comparison</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {strategies.map((s) => (
          <div key={s.strategy} className="card space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {s.rank === 1 && <span className="text-yellow-400">🏆</span>}
                <h3 className="font-medium text-white">{s.strategy}</h3>
              </div>
              <span className="text-xs text-gray-500">Rank #{s.rank}</span>
            </div>

            <div className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <p className="font-mono text-base text-white">{s.total_trades}</p>
                <p className="text-xs text-gray-500">Total Trades</p>
              </div>
              <div>
                <p className="font-mono text-base text-white">{s.closed_trades}</p>
                <p className="text-xs text-gray-500">Closed</p>
              </div>
              <div>
                <p className={`font-mono text-base ${s.win_rate >= 50 ? "text-green-400" : "text-red-400"}`}>{s.win_rate}%</p>
                <p className="text-xs text-gray-500">Win Rate</p>
              </div>
              <div>
                <p className={`font-mono text-base ${s.total_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>${s.total_pnl.toFixed(2)}</p>
                <p className="text-xs text-gray-500">Total PnL</p>
              </div>
              <div>
                <p className="font-mono text-base text-green-400">{s.winning}</p>
                <p className="text-xs text-gray-500">Winning</p>
              </div>
              <div>
                <p className="font-mono text-base text-red-400">{s.losing}</p>
                <p className="text-xs text-gray-500">Losing</p>
              </div>
              <div>
                <p className="font-mono text-base text-red-400">${s.max_drawdown.toFixed(2)}</p>
                <p className="text-xs text-gray-500">Max Drawdown</p>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
