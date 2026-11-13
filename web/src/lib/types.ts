export type Variant = {
  id: string
  title: string
  sku: string
  price_cents: number
  inventory_qty: number
}

export type Product = {
  id: string
  title: string
  description: string
  tags: string[]
  variants: Variant[]
}

export type Line = {
  variant_id: string
  title: string
  quantity: number
  unit_price_cents: number
}

export type Draft = {
  id: string
  token: string
  lines: Line[]
  total_cents: number
  currency: string
  status: string
}

export type Order = {
  id: string
  number: string
  phone: string
  status: string
  financial_status: string
  total_cents: number
  currency: string
  lines: Line[]
  duplicate?: boolean
  idempotency_key?: string
}

export type Customer = {
  id: string
  name: string
  phone: string
  email: string
}

export type Audit = {
  id: number
  ts: string
  phone: string | null
  action: string
  outcome: string
  detail: Record<string, unknown>
}

export type Escalation = {
  id: string
  phone: string
  reason: string
  status: string
  created_at: string
}

export type ChatMessage = {
  role: "user" | "assistant"
  content: string
  created_at?: string
}

export type Source = {
  title: string
  excerpt: string
  kind: string
  source: string
}

export type LlmInfo = {
  provider: string
  model: string
  base_url: string
  active: string
  reason: string
  embeddings: { provider: string; model: string; device: string }
}

export type Snapshot = {
  reply: string
  phone: string
  sources: Source[]
  draft: Draft | null
  order: Order | null
  escalation: Escalation | null
  safeguard: { code: string; message: string } | null
  customer: Customer | null
  orders: Order[]
  audit: Audit[]
  escalations: Escalation[]
  products: Product[]
  messages: ChatMessage[]
  llm: LlmInfo
  limits: { max_order_value_cents: number; currency: string }
}

export type Persona = {
  name: string
  phone: string
  note: string
}

export const personas: Persona[] = [
  { name: "Avery Chen", phone: "+15035550142", note: "New to the shop" },
  { name: "Samira Okonkwo", phone: "+14155550198", note: "Open order #10421" },
  { name: "Jordan Hale", phone: "+12065550144", note: "New to the shop" },
]

export const demoScript = [
  "Hi",
  "Is the coastal linen shirt in stock?",
  "What's your return policy?",
  "Recommend a gift under $50",
  "I'd like 2 coastal linen shirts in medium",
  "Yes, I confirm",
  "Where is my order?",
]

export const probes = [
  { label: "Missing size", text: "I'd like 2 coastal linen shirts" },
  { label: "Out of stock", text: "I'd like a canvas tote" },
  { label: "Over $500", text: "I'd like 3 northwind rain shells" },
  { label: "Other person's order", text: "What's the status of order 10421?" },
  { label: "Ask for a person", text: "I want to talk to a person" },
]
