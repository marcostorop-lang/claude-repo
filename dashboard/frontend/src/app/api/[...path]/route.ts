import { NextRequest, NextResponse } from "next/server";

const BACKEND = "http://127.0.0.1:8000";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(req: NextRequest) {
  // Extract the API path from the URL: /api/overview → overview, /api/trades?page=1 → trades
  const { pathname, search } = req.nextUrl;
  const apiPath = pathname.replace(/^\/api\//, "");
  const backendUrl = `${BACKEND}/api/${apiPath}${search}`;

  try {
    const res = await fetch(backendUrl, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    const text = await res.text();

    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : "Unknown error";
    console.error("[API Proxy] Error fetching", backendUrl, message);
    return NextResponse.json(
      { error: "Backend unreachable", detail: message },
      { status: 502 }
    );
  }
}
