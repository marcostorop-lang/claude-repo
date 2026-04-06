import { getPositions } from "@/lib/data";

export const dynamic = "force-dynamic";

export default function PositionsPage() {
  const data = getPositions();

  if (!data.positions.length) {
    return <div className="card text-center text-gray-500 py-8">No open positions.</div>;
  }

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">Open Positions ({data.positions.length})</h2>
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
              <th className="px-4 py-3 text-left">Opened</th>
            </tr>
          </thead>
          <tbody>
            {data.positions.map((p, i) => (
              <tr key={i} className="border-b border-surface-border/50 hover:bg-surface-hover/50">
                <td className="px-4 py-3 max-w-[220px] truncate" title={String(p.question)}>
                  {p.question as string}
                </td>
                <td className="px-4 py-3">
                  <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400">
                    {p.side as string}
                  </span>
                </td>
                <td className="px-4 py-3 text-right font-mono">{(p.entry_price as number).toFixed(4)}</td>
                <td className="px-4 py-3 text-right font-mono">{(p.current_price as number).toFixed(4)}</td>
                <td className="px-4 py-3 text-right font-mono">{(p.size as number).toFixed(2)}</td>
                <td className={`px-4 py-3 text-right font-mono ${
                  (p.unrealised_pnl as number) >= 0 ? "text-green-400" : "text-red-400"
                }`}>
                  ${(p.unrealised_pnl as number).toFixed(2)}
                </td>
                <td className="px-4 py-3 text-gray-300">{p.strategy as string}</td>
                <td className="px-4 py-3 text-gray-400 whitespace-nowrap">
                  {new Date(p.entry_time as string).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
