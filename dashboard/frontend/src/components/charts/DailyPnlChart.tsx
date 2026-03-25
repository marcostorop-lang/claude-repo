"use client";

import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Cell,
} from "recharts";
import type { DailyPnl } from "@/types";

interface Props {
  data: DailyPnl[];
}

export default function DailyPnlChart({ data }: Props) {
  if (!data.length) return null;

  return (
    <div className="card">
      <h3 className="text-sm font-medium text-gray-300 mb-4">Daily PnL</h3>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#2a3040" />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#6b7280" }} />
          <YAxis tick={{ fontSize: 11, fill: "#6b7280" }} />
          <Tooltip
            contentStyle={{ backgroundColor: "#161b22", border: "1px solid #2a3040", borderRadius: 8 }}
            labelStyle={{ color: "#9ca3af" }}
          />
          <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
            {data.map((entry, i) => (
              <Cell key={i} fill={entry.pnl >= 0 ? "#22c55e" : "#ef4444"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
