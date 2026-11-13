import { NextRequest } from "next/server"

export const dynamic = "force-dynamic"

function apiBase() {
  return process.env.API_INTERNAL_URL || "http://127.0.0.1:8091"
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params
  const target = `${apiBase()}/${path.join("/")}${request.nextUrl.search}`
  const headers = new Headers()
  const contentType = request.headers.get("content-type")
  if (contentType) headers.set("content-type", contentType)
  const init: RequestInit = { method: request.method, headers, cache: "no-store" }
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text()
  }
  try {
    const response = await fetch(target, init)
    const body = await response.arrayBuffer()
    return new Response(body, {
      status: response.status,
      headers: {
        "content-type": response.headers.get("content-type") || "application/json",
      },
    })
  } catch {
    return Response.json(
      { detail: "The agent API is not reachable. Start it with ./scripts/demo.sh." },
      { status: 502 },
    )
  }
}

export const GET = proxy
export const POST = proxy
