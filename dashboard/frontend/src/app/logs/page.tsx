import { getLogs } from "@/lib/data";

export const dynamic = "force-dynamic";

export default function LogsPage() {
  const data = getLogs();

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">Logs & Events</h2>

      {!data.logs.length ? (
        <div className="card text-center text-gray-500 py-8">No log entries.</div>
      ) : (
        <div className="card p-0 max-h-[70vh] overflow-auto">
          <table className="w-full text-xs font-mono">
            <tbody>
              {data.logs.map((entry, i) => (
                <tr key={i} className="border-b border-surface-border/30 hover:bg-surface-hover/30">
                  <td className="px-3 py-2 text-gray-500 whitespace-nowrap w-40">
                    {new Date(entry.timestamp).toLocaleString()}
                  </td>
                  <td className="px-3 py-2 w-20">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                      entry.level === "ERROR"
                        ? "bg-red-500/20 text-red-400"
                        : entry.level === "WARNING"
                        ? "bg-yellow-500/20 text-yellow-400"
                        : "bg-blue-500/20 text-blue-400"
                    }`}>
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
