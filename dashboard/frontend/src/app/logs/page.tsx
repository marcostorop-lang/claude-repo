"use client";

import { useState } from "react";
import { useLogs } from "@/lib/hooks";
import Loader from "@/components/ui/Loader";
import EmptyState from "@/components/ui/EmptyState";

const LEVELS = ["", "INFO", "WARNING", "ERROR"];

export default function LogsPage() {
  const [level, setLevel] = useState("");
  const { data, isLoading } = useLogs(level || undefined);

  if (isLoading) return <Loader />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Logs & Events</h2>
        <div className="flex gap-2">
          {LEVELS.map((l) => (
            <button
              key={l}
              onClick={() => setLevel(l)}
              className={`px-3 py-1 rounded-lg text-xs border transition-colors ${
                level === l
                  ? "border-brand-500 bg-brand-600/20 text-brand-500"
                  : "border-surface-border text-gray-400 hover:text-gray-200"
              }`}
            >
              {l || "All"}
            </button>
          ))}
        </div>
      </div>

      {!data?.logs.length ? (
        <EmptyState message="No log entries." />
      ) : (
        <div className="card p-0 max-h-[70vh] overflow-auto">
          <table className="w-full text-xs font-mono">
            <tbody>
              {data.logs.map((entry, i) => (
                <tr key={i} className="border-b border-surface-border/30 hover:bg-surface-hover/30">
                  <td className="px-3 py-2 text-gray-500 whitespace-nowrap w-40">
                    {entry.timestamp}
                  </td>
                  <td className="px-3 py-2 w-20">
                    <span
                      className={
                        entry.level === "ERROR"
                          ? "badge-error"
                          : entry.level === "WARNING"
                          ? "badge-warning"
                          : "badge-info"
                      }
                    >
                      {entry.level}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-gray-500 w-48 truncate">{entry.source}</td>
                  <td className="px-3 py-2 text-gray-300">{entry.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
