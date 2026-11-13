# Proposal answers

These answers describe the agent in this repository. The sandbox is the demo: `./scripts/demo.sh`, then open the WhatsApp panel.

## 1. Have you built and deployed similar agents before? Can you share a demo?

The working example is this project. It is the support and order agent described in the job, runnable without Shopify or WhatsApp credentials.

In the sandbox you can watch it keep a conversation, answer from the catalogue and the policy notes, recommend an in-stock gift under a budget, check inventory, create the customer record for the WhatsApp number, and place an order only after a separate "yes". The right-hand panel shows the draft, the Shopify order, the retrieved notes, and the audit log. Buttons cover the failure cases: missing size, out of stock, over the automatic limit, another customer's order, and a handoff to a person.

I am not attaching a list of older client deployments here. This repository is the thing you can click through, and the same process is what gets pointed at a development store when you are ready.

## 2. How would you design the architecture for WhatsApp conversations and Shopify order operations?

The shape that is built:

- **WhatsApp** terminates on `POST /webhooks/whatsapp` (or on the n8n inbound workflow, which forwards the raw Cloud API body and returns immediately). The sender's phone is the only identity.
- **LangGraph** runs one turn: retrieve, plan, tools, reply. Prior messages are loaded from SQLite into the graph, so context survives across webhook calls.
- **Tools** talk to Shopify Admin GraphQL for catalogue, inventory, customers, order lookup, and order creation. In the sandbox those calls hit a mock store with the same methods.
- **Safeguards** sit in front of `orderCreate`. The model can ask for an order; it cannot supply the phone, the price, or a confirmation flag the server will trust.
- **n8n** is the workflow layer around that: inbound webhook, a second validation of the order event, notification, and error logging. The agent creates the Shopify order and marks the event `already_created`, so n8n does not write a duplicate. A branch in the order workflow calls `orderCreate` only when that flag is false, for a setup that wants n8n to own the write instead.
- **Audit log** records drafts, denials, creates, duplicates, handoffs, and n8n delivery.

Orders are created with `financialStatus: PENDING` and `inventoryBehaviour: DECREMENT_OBEYING_POLICY`. The agent does not capture payment.

## 3. Which tools would you use for the agent, the workflows, and the knowledge base, and why?

| Piece | Choice | Why |
| --- | --- | --- |
| Agent | LangGraph | The turn is an explicit graph (retrieve → plan → tools → reply) with the message history on the state. Tool calls and the final answer are separate steps, which is what makes the confirmation rule testable. |
| Model | Groq, `openai/gpt-oss-120b`, OpenAI-compatible API | Default in config (`GROQ_API_KEY`, base URL `https://api.groq.com/openai/v1`). It is on Groq's free tier, so the MVP does not start with a model bill. OpenAI, Gemini, and Ollama remain a provider switch. |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` on CPU | Groq does not offer an embeddings endpoint. A local model keeps retrieval free and usable offline. |
| Vector store | Embedded Qdrant | No cluster to pay for in the demo. Set `QDRANT_URL` when you want a server. The retriever is hybrid: vector score plus keyword overlap, which is reliable on a small policy set. |
| Workflows | Self-hosted n8n | Webhooks, the order check, notifications, and the error path are JSON you can import. Docker Compose runs n8n next to the agent. |
| Store | Shopify Admin GraphQL | `orderCreate` is the supported way to create an order, including inventory behaviour. The mock store implements the same operations for the demo. |
| Channel | WhatsApp Cloud API | Real send and signature check in live mode. The sandbox UI calls the same `handle()` path the webhook uses. |
| App | FastAPI + a small Next.js console | Webhooks and the demo UI. The console is for showing the client the gates, not a second product. |

The offline planner exists so the demo and the test suite run with no key. It is not a second product: both planners call the same tools.

## 4. How would you prevent incorrect, duplicate, or unauthorised orders?

By not letting the model be the authority.

- **Incorrect.** Line items are parsed from the customer's message and priced from Shopify at draft time and again at confirm time. A sized product is not ordered until they name a size. Stock is checked twice. If the price moved, the draft is rejected. If the model says "I've placed the order" without a successful tool result, the reply is rewritten to say no order exists. New orders are pending, never marked paid.
- **Duplicate.** Each confirmation has an idempotency key (phone + draft id), stored locally and sent to Shopify. A second confirm returns the existing order and does not decrement stock again. An identical basket for the same phone inside ten minutes is blocked unless they explicitly ask for another order.
- **Unauthorised.** The phone on the order is the WhatsApp sender, not a number the model or the message proposes. Order lookup returns only that customer's orders and refuses with no line-item details if the number belongs to someone else. A yes in the same message as the purchase does not count; confirmation is a later turn. Orders over $500 (configurable) are not created and a person is flagged. While a handoff is open, drafting and placing are paused. n8n repeats the confirmation, phone, idempotency, and value checks before it will notify or write.

The tests in `tests/test_orders.py` exercise each of these without a live store.

## 5. What timeline and budget would you propose for the MVP?

**Budget: $1,000 fixed**, matching the posted job. That amount covers the MVP that is in this repository:

- the conversational agent and the sandbox demo
- WhatsApp Cloud API webhook and outbound send
- Shopify customer, catalogue, inventory, order lookup, and pending order creation
- RAG over the catalogue and the shop policies
- the four n8n workflows
- the order safeguards and audit log
- Docker Compose, tests, and this handover

Going live is not a second build. It is the checklist in the README: a Shopify development store, a WhatsApp test app, `GROQ_API_KEY`, `APP_MODE=live`, and importing the workflows. A development store, Groq's free tier, local embeddings, and self-hosted n8n keep the running cost at zero until real WhatsApp conversations start (those are billed by Meta, not by this agent).

Out of this MVP, and fine to scope afterwards: payment links, template messages beyond plain text, languages other than English, and a staff inbox beyond the escalation list. I would not quote a calendar for those until the sandbox has been tried against the real catalogue.
