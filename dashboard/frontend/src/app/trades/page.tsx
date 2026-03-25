"use client";

import { useState } from "react";
import { useTrades } from "@/lib/hooks";
import { apiFetch, exportCSV } from "@/lib/api";
import Loader from "@/components/ui/Loader";
import EmptyState from "@/components/ui/EmptyState";
import { Download, ChevronLeft, ChevronRight } from "lucide-react";
import type { Trade } from "@/types";

export default function TradesPage() {
  const [page, setPage] = useState(1);
  const [strategy, setStrategy] = useState("");
  const [side, setSide] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [sortBy, setSortBy] = useState("timestamp");
  const [sortDir, setSortDir] = useState("desc");

  const params: Record<string, string> = {
    page: String(page),
    per_page: "20",
  };
  if (strategy) params.strategy = strategy;
  if (side) params.side = side;
  if (dateFrom) params.date_from = dateFrom;
  if (dateTo) params.date_to = dateTo;
  params.sort_by = sortBy;
  params.sort_dir = sortDir;

  const { data, isLoading } = useTrades(params);

  const toggleSort = (col: string) => {
    if (sortBy === col) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortBy(col);
      setSortDir("desc");
    }
  };

  const handleExport = async () => {
    const resp = await apiFetch<{ trades: Trade[] }>("/api/trades/export");
    exportCSV(resp.trades, "polymarket_trades.csv");
  };

  if (isLoading) return <Loader />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Trades</h2>
        <button
          onClick={handleExport}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-surface-card border border-surface-border text-sm hover:bg-surface-hover transition-colors"
        >
          <Download size={14} /> Export CSV
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 text-sm">
        <select
          value={strategy}
          onChange={(e) => { setStrategy(e.target.value); setPage(1); }}
          className="bg-surface-card border border-surface-border rounded-lg px-3 py-1.5"
        >
          <option value="">All Strategies</option>
          <option value="simple_momentum">Momentum</option>
          <option value="mean_reversion">Mean Reversion</option>
        </select>
        <select
          value={side}
          onChange={(e) => { setSide(e.target.value); setPage(1); }}
          className="bg-surface-card border border-surface-border rounded-lg px-3 py-1.5"
        >
          <option value="">All Sides</option>
          <option value="BUY">BUY</option>
          <option value="SELL">SELL</option>
        </select>
        <input
          type="date"
          value={dateFrom}
          onChange={(e) => { setDateFrom(e.target.value); setPage(1); }}
          className="bg-surface-card border border-surface-border rounded-lg px-3 py-1.5"
          placeholder="From"
        />
        <input
          type="date"
          value={dateTo}
          onChange={(e) => { setDateTo(e.target.value); setPage(1); }}
          className="bg-surface-card border border-surface-border rounded-lg px-3 py-1.5"
          placeholder="To"
        />
      </div>

      {/* Table */}
      {!data?.trades.length ? (
        <EmptyState message="No trades found." />
      ) : (
        <>
          <div className="card overflow-x-auto p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-surface-border text-gray-400 text-xs uppercase">
                  {[
                    { key: "timestamp", label: "Time" },
                    { key: "question", label: "Market" },
                    { key: "side", label: "Side" },
                    { key: "strategy", label: "Strategy" },
                    { key: "price", label: "Price" },
                    { key: "size", label: "Size" },
                    { key: "mode", label: "Mode" },
                  ].map(({ key, label }) => (
                    <th
                      key={key}
                      onClick={() => toggleSort(key)}
                      className="px-4 py-3 text-left cursor-pointer hover:text-gray-200 whitespace-nowrap"
                    >
                      {label} {sortBy === key ? (sortDir === "asc" ? "^" : "v") : ""}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.trades.map((t) => (
                  <tr key={t.id} className="border-b border-surface-border/50 hover:bg-surface-hover/50">
                    <td className="px-4 py-3 whitespace-nowrap text-gray-400">
                      {new Date(t.timestamp).toLocaleString()}
                    </td>
                    <td className="px-4 py-3 max-w-[250px] truncate" title={t.question}>
                      {t.question || t.condition_id.slice(0, 12)}
                    </td>
                    <td className="px-4 py-3">
                      <span className={t.side === "BUY" ? "badge-buy" : "badge-sell"}>{t.side}</span>
                    </td>
                    <td className="px-4 py-3 text-gray-300">{t.strategy}</td>
                    <td className="px-4 py-3 font-mono">{t.price.toFixed(4)}</td>
                    <td className="px-4 py-3 font-mono">{t.size.toFixed(2)}</td>
                    <td className="px-4 py-3">
                      <span className="badge-info">{t.mode}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-between text-sm text-gray-400">
            <span>
              Page {data.page} of {data.pages} ({data.total} trades)
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
                className="p-1.5 rounded border border-surface-border disabled:opacity-30 hover:bg-surface-hover"
              >
                <ChevronLeft size={16} />
              </button>
              <button
                disabled={page >= data.pages}
                onClick={() => setPage(page + 1)}
                className="p-1.5 rounded border border-surface-border disabled:opacity-30 hover:bg-surface-hover"
              >
                <ChevronRight size={16} />
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
