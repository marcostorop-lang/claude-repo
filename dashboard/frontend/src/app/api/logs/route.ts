import { NextRequest, NextResponse } from "next/server";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const level = req.nextUrl.searchParams.get("level") || "";
  const templates = [
    { level: "INFO", source: "src.main", message: "Bot started | mode=PAPER | strategy=simple_momentum" },
    { level: "INFO", source: "src.polymarket.market_data", message: "Fetched 142 raw markets from Gamma API." },
    { level: "INFO", source: "src.main", message: "Evaluating 18 market snapshots." },
    { level: "INFO", source: "src.polymarket.execution", message: "[PAPER] BUY 25.00 of tok_001a @ 0.4500" },
    { level: "WARNING", source: "src.risk.manager", message: "Reduced size to stay within exposure limit." },
    { level: "INFO", source: "src.main", message: "Stop-loss triggered for tok_003a" },
    { level: "ERROR", source: "src.polymarket.client", message: "Connection timeout — retrying (attempt 2/3)." },
    { level: "INFO", source: "src.main", message: "Portfolio: open=3, exposure=$87.50, pnl=$12.34" },
    { level: "INFO", source: "src.main", message: "Sleeping 60s..." },
  ];

  const now = Date.now();
  const logs = [];
  for (let i = 0; i < 100; i++) {
    const t = templates[i % templates.length];
    if (level && t.level !== level) continue;
    const ts = new Date(now - i * 120000);
    logs.push({ timestamp: ts.toISOString().replace("T", " ").slice(0, 19), ...t });
  }
  return NextResponse.json({ logs });
}
