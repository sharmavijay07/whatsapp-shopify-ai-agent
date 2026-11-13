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
- **Tools** talk to Shopify Admin GraphQL for catalogue search, live inventory, customers, order status, and draft orders. In the sandbox those calls hit a mock store with the same confirmation rules. In live mode `prepare_order` opens a Shopify draft (`draftOrderCreate`). `confirm_order` completes it with `paymentPending: true` and calls `orderInvoiceSend` when the customer has an email. The direct `orderCreate` mutation is still in the client for a caller that has no draft id, with `financialStatus: PENDING` and `inventoryBehaviour: DECREMENT_OBEYING_POLICY`.
- **Safeguards** sit in front of that completion. The model can ask for an order; it cannot supply the phone, the price, or a confirmation flag the server will trust. A YES reply button and a typed YES are the same confirmation.
- **WhatsApp** sends reply buttons on a draft, a list when there is more than one product to choose, and the utility template `harbor_order_status` when a Shopify `orders/updated` or fulfillment webhook arrives (those can land outside the 24-hour window). The template body is in `docs/whatsapp-templates.json`.
- **Email** is Gmail SMTP on `smtp.gmail.com:465` (`GMAIL_USER`, `GMAIL_APP_PASSWORD`) to `OWNER_EMAIL`, plus the customer when we have an address. Missing Gmail credentials keep demo mode: the message is audited and not sent.
- **n8n** is optional. Empty webhook URLs skip it and the order still completes. When it is connected it forwards inbound messages, re-checks the order event, and logs errors. The agent marks the event `already_created`, so n8n does not write a second Shopify order. The workflows import inactive and run on the n8n Cloud trial or the optional free Render Docker service.
- **Audit log** records drafts, denials, creates, duplicates, handoffs, invoice sends, email, and n8n delivery.

The agent does not capture payment. Shopify webhooks are HMAC-checked with the custom app API secret.

## 3. Which tools would you use for the agent, the workflows, and the knowledge base, and why?

| Piece | Choice | Why |
| --- | --- | --- |
| Agent | LangGraph | The turn is an explicit graph (retrieve → plan → tools → reply) with the message history on the state. Tool calls and the final answer are separate steps, which is what makes the confirmation rule testable. |
| Model | Groq, `openai/gpt-oss-120b`, OpenAI-compatible API | Default in config (`GROQ_API_KEY`, base URL `https://api.groq.com/openai/v1`). It is on Groq's free tier, so the MVP does not start with a model bill. OpenAI, Gemini, and Ollama remain a provider switch. |
| Embeddings | BM25 by default; Jina (or any OpenAI-compatible embeddings API) or fastembed when Qdrant is on | Groq does not offer an embeddings endpoint. MiniLM does not fit Render's 512 MB free instance. Hosted embeddings do, and a failed setup falls back to BM25. |
| Vector store | In-memory BM25, or Qdrant Cloud's free cluster | `QDRANT_URL` and `QDRANT_API_KEY`. Embedded Qdrant remains available off Render for the local demo. Retrieval is hybrid: vector score plus keyword overlap. |
| Workflows | n8n, optional | The four JSON workflows import inactive. Run them on the n8n Cloud trial or the optional `harbor-n8n` service in `render.yaml`. The agent is complete with the webhook URLs empty. |
| Store | Shopify Admin GraphQL | Live orders are draft orders, completed and invoiced on confirmation, payment left pending. `scripts/seed_dev_store` loads the demo catalogue into a Partner development store. |
| Channel | WhatsApp Cloud API | Text, YES/NO reply buttons, product list messages, and the `harbor_order_status` template. The sandbox UI calls the same `handle()` path the webhook uses. |
| Mail | Gmail SMTP, port 465 | Order copies and handoff alerts. Demo mode when the app password is unset. |
| App | FastAPI + a small Next.js console | Webhooks and the demo UI. The console is for showing the client the gates, not a second product. |

The offline planner exists so the demo and the test suite run with no key. It is not a second product: both planners call the same tools.

## 4. How would you prevent incorrect, duplicate, or unauthorised orders?

By not letting the model be the authority.

- **Incorrect.** Line items are parsed from the customer's message and priced from Shopify at draft time and again at confirm time. A sized product is not ordered until they name a size. Stock is checked twice. If the price moved, the draft is rejected. If the model says "I've placed the order" without a successful tool result, the reply is rewritten to say no order exists. New orders are pending, never marked paid. In live mode that yes completes the Shopify draft and sends the invoice.
- **Duplicate.** Each confirmation has an idempotency key (phone + draft id), stored locally and sent to Shopify. A second confirm returns the existing order and does not decrement stock again. An identical basket for the same phone inside ten minutes is blocked unless they explicitly ask for another order.
- **Unauthorised.** The phone on the order is the WhatsApp sender, not a number the model or the message proposes. Order lookup returns only that customer's orders and refuses with no line-item details if the number belongs to someone else. A yes in the same message as the purchase does not count; confirmation is a later turn. Orders over $500 (configurable) are not created and a person is flagged. While a handoff is open, drafting and placing are paused. n8n repeats the confirmation, phone, idempotency, and value checks before it will notify or write.

The tests in `tests/test_orders.py` exercise each of these without a live store.

## 5. What timeline and budget would you propose for the MVP?

**Budget: $1,000 fixed**, matching the posted job. That amount covers the MVP that is in this repository:

- the conversational agent and the sandbox demo
- WhatsApp Cloud API webhook, reply buttons, product lists, the order-status template, and outbound send
- Shopify customer create, catalogue search, live inventory, order status, draft orders, and invoice send on confirmation
- Gmail order copies and handoff alerts, with demo mode when the app password is unset
- Shopify order and fulfillment webhooks, HMAC checked, then WhatsApp template plus email
- RAG over the catalogue and the shop policies, BM25 by default, Qdrant Cloud when configured
- the four n8n workflows, optional
- the order safeguards and audit log
- Docker Compose, tests, and this handover

Going live is not a second build. It is the "Go live with free tools" checklist in the README: a Shopify Partner development store, Meta's free WhatsApp test number, a Gmail app password, optional Qdrant Cloud and Jina embeddings, `GROQ_API_KEY`, and `APP_MODE=live`. n8n is optional. Those accounts are free. Meta bills WhatsApp only after you leave the test number and message customers who are not on the test list.

Out of this MVP, and fine to scope afterwards: taking payment inside the chat, languages other than English, and a staff inbox beyond the escalation list and the owner email. Order-status templates, reply buttons, and list messages are in this build.
