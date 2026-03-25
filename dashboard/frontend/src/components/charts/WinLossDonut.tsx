"use client";

import { ResponsiveContainer, PieChart, Pie, Cell, Legend, Tooltip } from "recharts";

interface Props {
  winning: number;
  losing: number;
}

export default function WinLossDonut({ winning, losing }: Props) {
  const data = [
    { name: "Winning", value: winning },
    { name: "Losing", value: losing },
  ];
  const COLORS = ["#22c55e", "#ef4444"];

  if (winning + losing === 0) return null;

  return (
    <div className="card">
      <h3 className="text-sm font-medium text-gray-300 mb-4">Win/Loss Distribution</h3>
      <ResponsiveContainer width="100%" height={250}>
        <PieChart>
          <Pie data={data} cx="50%" cy="50%" innerRadius={60} outerRadius={90} dataKey="value" label>
            {data.map((_, i) => (
              <Cell key={i} fill={COLORS[i]} />
            ))}
          </Pie>
          <Legend />
          <Tooltip
            contentStyle={{ backgroundColor: "#161b22", border: "1px solid #2a3040", borderRadius: 8 }}
          />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}
