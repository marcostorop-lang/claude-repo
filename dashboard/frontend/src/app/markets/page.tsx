import { getMarkets } from "@/lib/data";

export const dynamic = "force-dynamic";

export default function MarketsPage() {
  const data = getMarkets();

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">Markets ({data.markets.length})</h2>

      {!data.markets.length ? (
        <div className="card text-center text-gray-500 py-8">No markets found.</div>
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
                  <td className={`px-4 py-3 text-right font-mono ${
                    m.pnl >= 0 ? "text-green-400" : "text-red-400"
                  }`}>
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
