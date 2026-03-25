import type { ReactNode } from "react";

interface Props {
  label: string;
  value: string | number;
  icon?: ReactNode;
  trend?: "positive" | "negative" | "neutral";
  sub?: string;
}

export default function StatCard({ label, value, icon, trend, sub }: Props) {
  const color =
    trend === "positive"
      ? "text-success"
      : trend === "negative"
      ? "text-danger"
      : "text-white";

  return (
    <div className="card flex items-start gap-4">
      {icon && (
        <div className="p-2 rounded-lg bg-brand-600/10 text-brand-500 shrink-0">
          {icon}
        </div>
      )}
      <div>
        <p className={`stat-value ${color}`}>{value}</p>
        <p className="stat-label">{label}</p>
        {sub && <p className="text-xs text-gray-500 mt-0.5">{sub}</p>}
      </div>
    </div>
  );
}
