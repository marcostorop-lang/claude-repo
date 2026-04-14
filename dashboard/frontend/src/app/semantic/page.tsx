import {
  getSemanticSummary,
  getSemanticSignals,
  getSemanticCalibration,
} from "@/lib/data";
import StatCard from "@/components/ui/StatCard";

export const dynamic = "force-dynamic";

// Method colouring: structural wins > textual, so we hint the operator
// with a subtle tint rather than a prominent trend indicator.  The ops
// signal in the runbook is "structural_complement should dominate".
function methodBadgeClass(method: string): string {
  if (method.startsWith("structural")) return "bg-success/10 text-success";
  if (method.startsWith("weighted_avg") || method.startsWith("equivalent"))
    return "bg-brand-600/20 text-brand-500";
  if (method.startsWith("temporal")) return "bg-yellow-500/10 text-yellow-400";
  return "bg-gray-500/10 text-gray-300";
}

function ratioTone(ratio: number | null): string {
  if (ratio === null) return "text-gray-400";
  if (ratio >= 0.7) return "text-success";
  if (ratio >= 0.4) return "text-yellow-400";
  return "text-danger";
}

export default function SemanticPage() {
  const summary = getSemanticSummary(7);
  const calibration = getSemanticCalibration(30, 24);
  const recent = getSemanticSignals(20);

  if (!summary.table_exists) {
    return (
      <div className="card text-center text-gray-400 py-10 space-y-2">
        <h2 className="text-lg font-semibold text-white">Semantic Engine</h2>
        <p>
          The <code className="text-brand-500">semantic_signals</code> table
          hasn&apos;t been created yet.
        </p>
        <p className="text-xs">
          Set <code>SEMANTIC_ENGINE_ENABLED=true</code> and run at least one
          tick, then refresh.
        </p>
      </div>
    );
  }

  const shareStructural = summary.by_method.length
    ? summary.by_method
        .filter((m) => m.method.startsWith("structural"))
        .reduce((s, m) => s + m.count, 0) / Math.max(summary.total, 1)
    : 0;
  const structuralTrend =
    shareStructural >= 0.6 ? "positive" : shareStructural >= 0.3 ? "neutral" : "negative";

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-white">Semantic Engine</h2>
        <p className="text-xs text-gray-500 mt-1">
          Closed-loop calibration: detected edge (semantic_signals) vs.
          realised edge (trades). Window:{" "}
          <span className="font-mono">{summary.window_days}d summary</span> /{" "}
          <span className="font-mono">{calibration.window_days}d calibration</span>.
        </p>
      </div>

      {/* --- Row 1: 7-day summary counters -------------------------------- */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Signals (7d)"
          value={summary.total}
          sub={`${summary.by_side.BUY} BUY · ${summary.by_side.SELL} SELL`}
        />
        <StatCard
          label="Avg. net edge"
          value={`${(summary.avg_net_edge * 100).toFixed(2)}%`}
        />
        <StatCard
          label="Avg. score"
          value={summary.avg_score.toFixed(2)}
        />
        <StatCard
          label="Structural share"
          value={`${(shareStructural * 100).toFixed(0)}%`}
          trend={structuralTrend as "positive" | "neutral" | "negative"}
          sub={
            shareStructural >= 0.6
              ? "Healthy — structural dominates"
              : shareStructural < 0.3
              ? "Low — textual/temporal noise"
              : "Mixed — monitor"
          }
        />
      </div>

      {/* --- Row 2: Calibration table (the point of the page) ------------ */}
      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-medium text-white">
            Calibration by method{" "}
            <span className="text-xs text-gray-500 ml-2">
              ({calibration.n_matched} matched / {calibration.n_signals} signals)
            </span>
          </h3>
          {calibration.warnings.length > 0 && (
            <span className="text-xs text-yellow-400">
              {calibration.warnings.join(", ")}
            </span>
          )}
        </div>
        {calibration.per_method.length === 0 ? (
          <p className="text-sm text-gray-500 py-4 text-center">
            No matched signals yet. Run the bot with{" "}
            <code>STRATEGY=semantic_mispricing</code> to populate this.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-gray-500 uppercase border-b border-surface-border">
                <tr>
                  <th className="text-left pb-2 pr-4">Method</th>
                  <th className="text-right pb-2 pr-4">Signals</th>
                  <th className="text-right pb-2 pr-4">Matched</th>
                  <th className="text-right pb-2 pr-4">Win rate</th>
                  <th className="text-right pb-2 pr-4">Detected</th>
                  <th className="text-right pb-2 pr-4">Realised</th>
                  <th className="text-right pb-2">Ratio</th>
                </tr>
              </thead>
              <tbody>
                {calibration.per_method.map((m) => (
                  <tr key={m.method} className="border-b border-surface-border/50">
                    <td className="py-2 pr-4">
                      <span
                        className={`px-2 py-0.5 rounded text-xs font-mono ${methodBadgeClass(m.method)}`}
                      >
                        {m.method}
                      </span>
                    </td>
                    <td className="text-right py-2 pr-4 font-mono">{m.n_signals}</td>
                    <td className="text-right py-2 pr-4 font-mono">{m.n_matched}</td>
                    <td className="text-right py-2 pr-4 font-mono">
                      {(m.win_rate * 100).toFixed(0)}%
                    </td>
                    <td className="text-right py-2 pr-4 font-mono text-gray-300">
                      {(m.avg_detected_edge * 100).toFixed(2)}%
                    </td>
                    <td className={`text-right py-2 pr-4 font-mono ${m.avg_realised_edge >= 0 ? "text-success" : "text-danger"}`}>
                      {(m.avg_realised_edge * 100).toFixed(2)}%
                    </td>
                    <td className={`text-right py-2 font-mono ${ratioTone(m.realisation_ratio)}`}>
                      {m.realisation_ratio === null ? "—" : m.realisation_ratio.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-gray-500 leading-relaxed pt-1">
          <strong className="text-gray-400">How to read:</strong> ratio ≥ 0.7
          means the engine&apos;s detected edge translated honestly into PnL.
          Values &lt; 0.4 suggest raising{" "}
          <code>SEMANTIC_MIN_NET_EDGE</code> for that method.
          Per <code>docs/runbook.md</code>,{" "}
          <code>structural_complement</code> should dominate before promoting
          to live.
        </p>
      </div>

      {/* --- Row 3: Recent signals --------------------------------------- */}
      <div className="card space-y-3">
        <h3 className="font-medium text-white">
          Recent signals{" "}
          <span className="text-xs text-gray-500 ml-2">
            (latest {recent.signals.length})
          </span>
        </h3>
        {recent.signals.length === 0 ? (
          <p className="text-sm text-gray-500 py-4 text-center">
            No signals yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-gray-500 uppercase border-b border-surface-border">
                <tr>
                  <th className="text-left pb-2 pr-4">Time</th>
                  <th className="text-left pb-2 pr-4">Market</th>
                  <th className="text-left pb-2 pr-4">Method</th>
                  <th className="text-left pb-2 pr-4">Mode</th>
                  <th className="text-right pb-2 pr-4">Side</th>
                  <th className="text-right pb-2 pr-4">Mid</th>
                  <th className="text-right pb-2 pr-4">Fair</th>
                  <th className="text-right pb-2 pr-4">Edge</th>
                  <th className="text-right pb-2">Score</th>
                </tr>
              </thead>
              <tbody>
                {recent.signals.map((s) => (
                  <tr key={s.id} className="border-b border-surface-border/50">
                    <td className="py-2 pr-4 font-mono text-xs text-gray-400">
                      {s.timestamp.slice(5, 16).replace("T", " ")}
                    </td>
                    <td className="py-2 pr-4 max-w-xs truncate" title={s.question}>
                      {s.question || s.token_id.slice(0, 12)}
                    </td>
                    <td className="py-2 pr-4">
                      <span
                        className={`px-1.5 py-0.5 rounded text-xs font-mono ${methodBadgeClass(s.synthetic_method)}`}
                      >
                        {s.synthetic_method}
                      </span>
                    </td>
                    <td className="py-2 pr-4">
                      <span
                        className={`text-xs uppercase font-mono ${s.mode === "live" ? "text-brand-500" : "text-gray-500"}`}
                      >
                        {s.mode}
                      </span>
                    </td>
                    <td
                      className={`text-right py-2 pr-4 font-mono ${s.side === "BUY" ? "text-success" : "text-danger"}`}
                    >
                      {s.side}
                    </td>
                    <td className="text-right py-2 pr-4 font-mono">
                      {s.midpoint.toFixed(3)}
                    </td>
                    <td className="text-right py-2 pr-4 font-mono">
                      {s.synthetic_fair.toFixed(3)}
                    </td>
                    <td
                      className={`text-right py-2 pr-4 font-mono ${s.net_edge >= 0 ? "text-success" : "text-danger"}`}
                    >
                      {(s.net_edge * 100).toFixed(2)}%
                    </td>
                    <td className="text-right py-2 font-mono">
                      {s.score.toFixed(2)}
                    </td>
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
