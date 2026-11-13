import type { Snapshot } from "@/lib/types"

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = body.detail || detail
    } catch {
      /* keep status text */
    }
    throw new Error(typeof detail === "string" ? detail : "Request failed")
  }
  return response.json() as Promise<T>
}

export function loadSandbox(phone: string) {
  return request<Snapshot>(`/backend/api/sandbox?phone=${encodeURIComponent(phone)}`)
}

export function sendChat(phone: string, text: string, name: string) {
  return request<Snapshot>("/backend/api/chat", {
    method: "POST",
    body: JSON.stringify({ phone, text, name }),
  })
}

export function resetSandbox() {
  return request<{ ok: boolean }>("/backend/api/sandbox/reset", { method: "POST" })
}

export function resolveEscalation(id: string) {
  return request(`/backend/api/escalations/${encodeURIComponent(id)}/resolve`, { method: "POST" })
}

export function loadHealth() {
  return request<{
    status: string
    mode: string
    shopify: string
    whatsapp: string
    vector_points: number
    llm: Snapshot["llm"]
  }>("/backend/health")
}
