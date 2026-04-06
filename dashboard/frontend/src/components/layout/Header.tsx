import { getOverview } from "@/lib/data";

export default function Header() {
  const data = getOverview();

  return (
    <header className="h-14 shrink-0 border-b border-surface-border flex items-center justify-between px-6">
      <h1 className="text-sm font-medium text-gray-300">Dashboard</h1>
      <div className="flex items-center gap-4 text-xs">
        <span className="flex items-center gap-1.5 text-success">
          <span className="w-2 h-2 rounded-full bg-success inline-block" />
          Connected
        </span>
        {data.last_update && (
          <span className="text-gray-500">
            Last: {new Date(data.last_update).toLocaleTimeString()}
          </span>
        )}
      </div>
    </header>
  );
}
