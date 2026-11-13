"use client"

import { useEffect, useRef, useState, type RefObject } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { loadHealth, loadSandbox, resetSandbox, resolveEscalation, sendChat } from "@/lib/api"
import { demoScript, personas, probes, type Persona, type Snapshot } from "@/lib/types"
import { money, prettyPhone } from "@/lib/utils"

export function DemoConsole() {
  const [persona, setPersona] = useState<Persona>(personas[0])
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [draftText, setDraftText] = useState("")
  const [busy, setBusy] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState("mock")
  const threadRef = useRef<HTMLDivElement>(null)
  const personaRef = useRef(persona)
  personaRef.current = persona

  async function refresh(phone: string) {
    const data = await loadSandbox(phone)
    setSnapshot(data)
    setError(null)
  }

  useEffect(() => {
    let cancelled = false
    const phone = personaRef.current.phone
    loadHealth()
      .then((health) => {
        if (!cancelled) setMode(health.shopify)
      })
      .catch(() => {
        if (!cancelled) setError("The agent API is not reachable. Start it with ./scripts/demo.sh.")
      })
    refresh(phone).catch((reason: Error) => {
      if (!cancelled) setError(reason.message)
    })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight })
  }, [snapshot?.messages.length, busy])

  async function selectPersona(next: Persona) {
    if (busy) return
    setPersona(next)
    setDraftText("")
    try {
      await refresh(next.phone)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load that chat.")
    }
  }

  async function submit(text: string) {
    const trimmed = text.trim()
    if (!trimmed || busy) return
    setBusy(true)
    setError(null)
    setDraftText("")
    try {
      const data = await sendChat(persona.phone, trimmed, persona.name)
      setSnapshot(data)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Message failed.")
    } finally {
      setBusy(false)
    }
  }

  async function play() {
    setPlaying(true)
    setBusy(true)
    setError(null)
    try {
      await resetSandbox()
      const avery = personas[0]
      setPersona(avery)
      for (const line of demoScript) {
        const data = await sendChat(avery.phone, line, avery.name)
        setSnapshot(data)
        await new Promise((resolve) => setTimeout(resolve, 450))
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The demo script stopped.")
    } finally {
      setPlaying(false)
      setBusy(false)
    }
  }

  async function reset() {
    setBusy(true)
    try {
      await resetSandbox()
      await refresh(persona.phone)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Reset failed.")
    } finally {
      setBusy(false)
    }
  }

  async function closeHandoff(id: string) {
    setBusy(true)
    try {
      await resolveEscalation(id)
      await refresh(persona.phone)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not close the handoff.")
    } finally {
      setBusy(false)
    }
  }

  const llm = snapshot?.llm
  const brain =
    llm?.active === "groq"
      ? `Groq · ${llm.model}`
      : llm?.active === "mock-fallback"
        ? "Offline planner · Groq key not set"
        : llm
          ? `${llm.active} · ${llm.model}`
          : "Starting"

  return (
    <main className="mx-auto flex min-h-screen max-w-7xl flex-col gap-4 px-4 py-5 md:px-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-muted-foreground">WhatsApp Business sandbox</p>
          <h1 className="font-display text-4xl leading-none text-primary">Harbor & Co.</h1>
          <p className="mt-2 max-w-xl text-sm text-muted-foreground">
            The agent answers from the catalogue and policies, checks live stock, and creates a Shopify order only
            after a separate yes. This phone is the only customer it will act for.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge>Shopify {mode}</Badge>
          <Badge variant="secondary">{brain}</Badge>
          <Badge variant="outline">Local embeddings</Badge>
        </div>
      </header>

      {llm?.reason ? (
        <p className="rounded-lg border border-border bg-card px-3 py-2 text-sm text-muted-foreground">{llm.reason}</p>
      ) : null}
      {error ? (
        <p className="rounded-lg border border-destructive/40 bg-card px-3 py-2 text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)_330px]">
        <aside className="hidden lg:block">
          <SideRail
            persona={persona}
            busy={busy}
            onPersona={selectPersona}
            onSend={(text) => void submit(text)}
            onPlay={() => void play()}
            onReset={() => void reset()}
            playing={playing}
            products={snapshot?.products || []}
          />
        </aside>

        <section className="min-w-0">
          <div className="lg:hidden">
            <Tabs defaultValue="chat">
              <TabsList className="w-full">
                <TabsTrigger value="chat">Chat</TabsTrigger>
                <TabsTrigger value="shop">Shop</TabsTrigger>
                <TabsTrigger value="audit">Audit</TabsTrigger>
              </TabsList>
              <TabsContent value="chat">
                <Phone
                  persona={persona}
                  snapshot={snapshot}
                  busy={busy}
                  draftText={draftText}
                  setDraftText={setDraftText}
                  onSubmit={(text) => void submit(text)}
                  threadRef={threadRef}
                />
              </TabsContent>
              <TabsContent value="shop">
                <SideRail
                  persona={persona}
                  busy={busy}
                  onPersona={selectPersona}
                  onSend={(text) => void submit(text)}
                  onPlay={() => void play()}
                  onReset={() => void reset()}
                  playing={playing}
                  products={snapshot?.products || []}
                />
              </TabsContent>
              <TabsContent value="audit">
                <Ops snapshot={snapshot} busy={busy} onResolve={(id) => void closeHandoff(id)} />
              </TabsContent>
            </Tabs>
          </div>
          <div className="hidden lg:block">
            <Phone
              persona={persona}
              snapshot={snapshot}
              busy={busy}
              draftText={draftText}
              setDraftText={setDraftText}
              onSubmit={(text) => void submit(text)}
              threadRef={threadRef}
            />
          </div>
        </section>

        <aside className="hidden lg:block">
          <Ops snapshot={snapshot} busy={busy} onResolve={(id) => void closeHandoff(id)} />
        </aside>
      </div>
    </main>
  )
}

function SideRail({
  persona,
  busy,
  playing,
  products,
  onPersona,
  onSend,
  onPlay,
  onReset,
}: {
  persona: Persona
  busy: boolean
  playing: boolean
  products: Snapshot["products"]
  onPersona: (persona: Persona) => void
  onSend: (text: string) => void
  onPlay: () => void
  onReset: () => void
}) {
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Who is texting</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {personas.map((item) => (
            <button
              key={item.phone}
              type="button"
              disabled={busy}
              onClick={() => onPersona(item)}
              className={`rounded-lg border px-3 py-2 text-left text-sm ${
                item.phone === persona.phone ? "border-primary bg-accent" : "border-border bg-card"
              }`}
            >
              <span className="block font-medium">{item.name}</span>
              <span className="text-xs text-muted-foreground">
                {prettyPhone(item.phone)} · {item.note}
              </span>
            </button>
          ))}
          <div className="mt-1 flex gap-2">
            <Button size="sm" onClick={onPlay} disabled={busy}>
              {playing ? "Playing…" : "Play demo"}
            </Button>
            <Button size="sm" variant="outline" onClick={onReset} disabled={busy}>
              Reset shop
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Try a safeguard</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {probes.map((probe) => (
            <Button key={probe.label} size="sm" variant="secondary" disabled={busy} onClick={() => onSend(probe.text)}>
              {probe.label}
            </Button>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Mock catalogue</CardTitle>
        </CardHeader>
        <CardContent className="flex max-h-72 flex-col gap-2 overflow-auto">
          {products.length === 0 ? (
            <p className="text-sm text-muted-foreground">Catalogue loads with the agent.</p>
          ) : (
            products.map((product) => {
              const stock = product.variants.reduce((sum, variant) => sum + variant.inventory_qty, 0)
              const price = product.variants[0]?.price_cents ?? 0
              return (
                <div key={product.id} className="flex items-start justify-between gap-3 text-sm">
                  <div>
                    <p className="font-medium">{product.title}</p>
                    <p className="text-xs text-muted-foreground">{money(price)}</p>
                  </div>
                  <Badge variant={stock > 0 ? "secondary" : "warning"}>{stock > 0 ? `${stock} in stock` : "Out"}</Badge>
                </div>
              )
            })
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Phone({
  persona,
  snapshot,
  busy,
  draftText,
  setDraftText,
  onSubmit,
  threadRef,
}: {
  persona: Persona
  snapshot: Snapshot | null
  busy: boolean
  draftText: string
  setDraftText: (value: string) => void
  onSubmit: (text: string) => void
  threadRef: RefObject<HTMLDivElement | null>
}) {
  const messages = snapshot?.messages || []
  return (
    <div className="overflow-hidden rounded-[28px] border border-[#0e2a26] bg-[#0e2a26] shadow-xl">
      <div className="flex items-center justify-between px-5 py-4 text-[#f4ecdf]">
        <div>
          <p className="font-display text-2xl leading-none">Harbor & Co.</p>
          <p className="mt-1 text-xs text-[#d5e6df]">Business account · replies in this sandbox</p>
        </div>
        <div className="text-right text-xs text-[#d5e6df]">
          <p>{persona.name}</p>
          <p>{prettyPhone(persona.phone)}</p>
        </div>
      </div>
      <div ref={threadRef} className="chat-paper h-[min(62vh,580px)] overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="mx-auto mt-16 max-w-sm rounded-2xl bg-card/90 px-4 py-5 text-center text-sm text-muted-foreground">
            No messages yet. Ask about a product, a policy, or play the demo. An order is not created until you reply
            YES to a draft.
          </div>
        ) : (
          <div className="flex flex-col gap-2" aria-live="polite">
            {messages.map((message, index) => (
              <div
                key={`${message.created_at || index}-${index}`}
                className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm leading-relaxed ${
                  message.role === "user"
                    ? "ml-auto rounded-br-md bg-[#d7efe4] text-[#10241f]"
                    : "mr-auto rounded-bl-md bg-card text-foreground"
                }`}
              >
                {message.content}
              </div>
            ))}
            {busy ? (
              <div className="mr-auto rounded-2xl bg-card px-3 py-2 text-sm text-muted-foreground">Typing…</div>
            ) : null}
          </div>
        )}
      </div>
      <form
        className="flex gap-2 bg-[#f7f1e6] p-3"
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit(draftText)
        }}
      >
        <label className="sr-only" htmlFor="composer">
          Message Harbor & Co.
        </label>
        <Input
          id="composer"
          value={draftText}
          onChange={(event) => setDraftText(event.target.value)}
          placeholder={`Message as ${persona.name}`}
          disabled={busy}
        />
        <Button type="submit" disabled={busy || !draftText.trim()}>
          Send
        </Button>
      </form>
    </div>
  )
}

function Ops({
  snapshot,
  busy,
  onResolve,
}: {
  snapshot: Snapshot | null
  busy: boolean
  onResolve: (id: string) => void
}) {
  const limit = snapshot?.limits?.max_order_value_cents
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Draft waiting</CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          {snapshot?.draft ? (
            <div className="space-y-2">
              {snapshot.draft.lines.map((line) => (
                <p key={line.variant_id}>
                  {line.quantity} × {line.title}
                </p>
              ))}
              <Separator />
              <p className="font-medium">
                {money(snapshot.draft.total_cents)} · token {snapshot.draft.token}
              </p>
              <p className="text-xs text-muted-foreground">Not an order until the next message is an explicit yes.</p>
            </div>
          ) : (
            <p className="text-muted-foreground">No draft. Nothing will be sent to Shopify.</p>
          )}
          {snapshot?.safeguard ? (
            <p className="mt-3 rounded-md bg-secondary px-2 py-2 text-xs">
              Gate: {snapshot.safeguard.code.replace(/_/g, " ")}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Orders on this number</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {limit ? (
            <p className="text-xs text-muted-foreground">Automatic cap {money(limit)}. Payment stays pending.</p>
          ) : null}
          {(snapshot?.orders || []).length === 0 ? (
            <p className="text-muted-foreground">No orders yet.</p>
          ) : (
            snapshot?.orders.map((order) => (
              <div key={order.id}>
                <p className="font-medium">
                  #{order.number} · {order.financial_status} · {money(order.total_cents)}
                </p>
                <p className="text-xs text-muted-foreground">
                  {order.lines.map((line) => `${line.quantity} × ${line.title}`).join(", ")}
                </p>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Retrieved notes</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {(snapshot?.sources || []).length === 0 ? (
            <p className="text-muted-foreground">Sources show up when an answer uses the knowledge base.</p>
          ) : (
            snapshot?.sources.map((source) => (
              <div key={source.title}>
                <p className="font-medium">{source.title}</p>
                <p className="text-xs text-muted-foreground">{source.excerpt}</p>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Handoffs</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {(snapshot?.escalations || []).length === 0 ? (
            <p className="text-muted-foreground">No specialist has been asked in.</p>
          ) : (
            snapshot?.escalations.map((ticket) => (
              <div key={ticket.id} className="space-y-1">
                <p className="font-medium">
                  {ticket.id} · {ticket.status}
                </p>
                <p className="text-xs text-muted-foreground">{ticket.reason}</p>
                {ticket.status === "open" ? (
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => onResolve(ticket.id)}>
                    Close handoff
                  </Button>
                ) : null}
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Audit log</CardTitle>
        </CardHeader>
        <CardContent>
          <ScrollArea className="h-48">
            <div className="space-y-2 pr-2 text-xs">
              {(snapshot?.audit || []).length === 0 ? (
                <p className="text-sm text-muted-foreground">Order decisions will land here.</p>
              ) : (
                snapshot?.audit.map((row) => (
                  <div key={row.id} className="flex items-start justify-between gap-2">
                    <span>
                      {row.action}
                      <span className="block text-muted-foreground">{row.ts.slice(11, 19)} UTC</span>
                    </span>
                    <Badge variant={row.outcome === "denied" || row.outcome === "error" ? "warning" : "outline"}>
                      {row.outcome}
                    </Badge>
                  </div>
                ))
              )}
            </div>
          </ScrollArea>
        </CardContent>
      </Card>
    </div>
  )
}
