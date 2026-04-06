import { getTrades } from "@/lib/data";

export const dynamic = "force-dynamic";

export default function TradesPage() {
  const data = getTrades(1, 50);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Trades ({data.total} total)</h2>
      </div>

      {!data.trades.length ? (
        <div className="card text-center text-gray-500 py-8">No trades found.</div>
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border text-gray-400 text-xs uppercase">
                <th className="px-4 py-3 text-left">Time</th>
                <th className="px-4 py-3 text-left">Market</th>
                <th className="px-4 py-3 text-left">Side</th>
                <th className="px-4 py-3 text-left">Strategy</th>
                <th className="px-4 py-3 text-right">Price</th>
                <th className="px-4 py-3 text-right">Size</th>
                <th className="px-4 py-3 text-left">Mode</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t, i) => (
                <tr key={i} className="border-b border-surface-border/50 hover:bg-surface-hover/50">
                  <td className="px-4 py-3 whitespace-nowrap text-gray-400">
                    {new Date(t.timestamp as string).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 max-w-[250px] truncate" title={String(t.question || t.condition_id)}>
                    {(t.question as string) || (t.condition_id as string).slice(0, 12)}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                      t.side === "BUY" ? "bg-green-500/20 text-green-400" : "bg-red-500/20 text-red-400"
                    }`}>{t.side as string}</span>
                  </td>
                  <td className="px-4 py-3 text-gray-300">{t.strategy as string}</td>
                  <td className="px-4 py-3 text-right font-mono">{(t.price as number).toFixed(4)}</td>
                  <td className="px-4 py-3 text-right font-mono">{(t.size as number).toFixed(2)}</td>
                  <td className="px-4 py-3">
                    <span className="px-2 py-0.5 rounded text-xs bg-blue-500/20 text-blue-400">{t.mode as string}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
