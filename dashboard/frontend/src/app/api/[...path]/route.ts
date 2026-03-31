import { NextRequest, NextResponse } from "next/server";
import http from "http";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const runtime = "nodejs";

function proxyGet(url: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    http
      .get(url, (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => resolve({ status: res.statusCode || 200, body: data }));
      })
      .on("error", reject);
  });
}

export async function GET(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const apiPath = pathname.replace(/^\/api\//, "");
  const backendUrl = `http://127.0.0.1:8000/api/${apiPath}${search}`;

  try {
    const { status, body } = await proxyGet(backendUrl);
    return new NextResponse(body, {
      status,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache, no-store, must-revalidate",
      },
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : "Unknown";
    console.error("[API Proxy]", backendUrl, msg);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
