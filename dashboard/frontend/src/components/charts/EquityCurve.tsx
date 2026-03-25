"use client";

import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import type { EquityPoint } from "@/types";

interface Props {
  data: EquityPoint[];
}

export default function EquityCurve({ data }: Props) {
  if (!data.length) return null;

  const formatted = data.map((d) => ({
    ...d,
    time: new Date(d.time).toLocaleDateString(),
  }));

  const lastEquity = data[data.length - 1]?.equity ?? 0;
  const color = lastEquity >= 0 ? "#22c55e" : "#ef4444";

  return (
    <div className="card">
      <h3 className="text-sm font-medium text-gray-300 mb-4">Equity Curve</h3>
      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={formatted}>
          <defs>
            <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={color} stopOpacity={0.3} />
              <stop offset="95%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#2a3040" />
          <XAxis dataKey="time" tick={{ fontSize: 11, fill: "#6b7280" }} />
          <YAxis tick={{ fontSize: 11, fill: "#6b7280" }} />
          <Tooltip
            contentStyle={{ backgroundColor: "#161b22", border: "1px solid #2a3040", borderRadius: 8 }}
            labelStyle={{ color: "#9ca3af" }}
          />
          <Area type="monotone" dataKey="equity" stroke={color} fill="url(#equityGrad)" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
