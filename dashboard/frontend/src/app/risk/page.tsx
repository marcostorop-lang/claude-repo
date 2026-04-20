import { getRiskRejections, getSlippageOverview, getFeatureAttribution } from "@/lib/data";
import StatCard from "@/components/ui/StatCard";

export const dynamic = "force-dynamic";

export default function RiskPage() {
  const rejections = getRiskRejections(50);
  const slippage = getSlippageOverview(500);
  const attribution = getFeatureAttribution(20);

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Risk & Observability</h2>

      {/* Slippage overview */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 mb-3">Execution Slippage</h3>
        {slippage.n_entries === 0 ? (
          <div className="card text-center text-gray-500 py-4">No entry fills recorded yet.</div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard label="Entry Fills" value={slippage.n_entries} />
            <StatCard label="Predicted (bps)" value={slippage.avg_predicted_bps.toFixed(1)} />
            <StatCard label="Realised (bps)" value={slippage.avg_realised_bps.toFixed(1)} />
            <StatCard
              label="Drift (bps)"
              value={slippage.avg_drift_bps.toFixed(1)}
              trend={slippage.avg_drift_bps > 0 ? "negative" : slippage.avg_drift_bps < 0 ? "positive" : "neutral"}
            />
          </div>
        )}
      </div>

      {/* Feature attribution */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Feature Attribution
          <span className="ml-2 text-xs text-gray-600">
            ({attribution.total_trades} closed trades, {(attribution.overall_winrate * 100).toFixed(1)}% win-rate)
          </span>
        </h3>
        {attribution.features.length === 0 ? (
          <div className="card text-center text-gray-500 py-4">
            Not enough closed calibration data yet (need {">"}= 20 trades).
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 text-xs border-b border-surface-border">
                  <th className="pb-2 pr-4">Feature</th>
                  <th className="pb-2 pr-4 text-right">N</th>
                  <th className="pb-2 pr-4 text-right">Cohen&apos;s d</th>
                  <th className="pb-2 pr-4 text-right">Win Mean</th>
                  <th className="pb-2 pr-4 text-right">Loss Mean</th>
                  <th className="pb-2 pr-4 text-right">WR &ge; Median</th>
                  <th className="pb-2 text-right">WR &lt; Median</th>
                </tr>
              </thead>
              <tbody>
                {attribution.features.slice(0, 20).map((f) => (
                  <tr key={f.feature} className="border-b border-surface-border/50 hover:bg-surface-hover">
                    <td className="py-1.5 pr-4 font-mono text-white">{f.feature}</td>
                    <td className="py-1.5 pr-4 text-right text-gray-400">{f.n_total}</td>
                    <td className={`py-1.5 pr-4 text-right font-mono ${Math.abs(f.cohens_d) > 0.5 ? "text-yellow-400" : "text-gray-300"}`}>
                      {f.cohens_d > 0 ? "+" : ""}{f.cohens_d.toFixed(3)}
                    </td>
                    <td className="py-1.5 pr-4 text-right font-mono text-green-400">{f.win_mean.toFixed(4)}</td>
                    <td className="py-1.5 pr-4 text-right font-mono text-red-400">{f.loss_mean.toFixed(4)}</td>
                    <td className="py-1.5 pr-4 text-right font-mono">{(f.winrate_above_median * 100).toFixed(1)}%</td>
                    <td className="py-1.5 text-right font-mono">{(f.winrate_below_median * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Risk rejections log */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 mb-3">Recent Risk Rejections</h3>
        {rejections.length === 0 ? (
          <div className="card text-center text-gray-500 py-4">No risk rejections recorded.</div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 text-xs border-b border-surface-border">
                  <th className="pb-2 pr-4">Time</th>
                  <th className="pb-2 pr-4">Token</th>
                  <th className="pb-2 pr-4">Strategy</th>
                  <th className="pb-2 pr-4">Conf</th>
                  <th className="pb-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {rejections.map((r, i) => (
                  <tr key={i} className="border-b border-surface-border/50 hover:bg-surface-hover">
                    <td className="py-1.5 pr-4 text-gray-400 whitespace-nowrap">{r.timestamp.replace("T", " ").slice(0, 19)}</td>
                    <td className="py-1.5 pr-4 font-mono text-white">{r.token_id.slice(0, 12)}</td>
                    <td className="py-1.5 pr-4 text-gray-300">{r.strategy}</td>
                    <td className="py-1.5 pr-4 text-right font-mono text-gray-300">{r.confidence.toFixed(2)}</td>
                    <td className="py-1.5 text-gray-400 text-xs">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
