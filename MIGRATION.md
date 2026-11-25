# Moving case 7 off Agent Builder to my own server (RU + EN)

Written 23.11.2026. Deadline: **Agent Builder shuts down on 30 November 2026** (OpenAI Deprecations page, notice of 3 June 2026). The documented replacements are the Agents SDK (code) or Workspace Agents in ChatGPT.

## What I chose and why

**Agents SDK + my own ChatKit server.** Workspace Agents live inside ChatGPT (they need a Business, Enterprise or Edu plan) and can't be embedded in a website, so they don't work for the widget on retreat.tatidzufri.com.

ChatKit as a product stays; only the mode changes. Before, the widget got a `client_secret` and talked to a workflow on OpenAI's servers. After the move, the widget talks to my server, and the server runs the agents through the Agents SDK.

| | Before | After the move |
|---|---|---|
| Where the agents live | Agent Builder (OpenAI dashboard) | Code in the repository, the `retreat-chatkit` container |
| Widget | `api: { getClientSecret }` | `api: { url: "/api/chatkit", domainKey }` |
| Knowledge base | a vector store attached in Agent Builder | two vector stores via the API: RU and EN |
| Languages | Russian only | RU and EN: the page language picks the agent set and the knowledge base |
| Conversation history | kept by OpenAI | SQLite on the server (a volume that survives restarts) |

What had to be rewritten by hand, because according to OpenAI the export "doesn't carry over the graph and doesn't guarantee the same behavior": the classifier with transitions between agents becomes a dispatcher agent with handoffs, the guardrail becomes an Agents SDK input guardrail, and file attachments become `FileSearchTool` with the vector store id.

## Status on 23.11.2026

- ✅ Step 1: the export is in.
- ✅ Steps 2–5, 7: English vector stores created, the ChatKit server with agents (RU and EN) runs on the server, `/api/chatkit` is routed in nginx, the widget supports both modes.
- ✅ Step 8, partly: 14 scenarios run through the ChatKit protocol (RU and EN: greeting, prices, refunds, a template with and without the code, marketing, jailbreak, continuing a conversation). The browser check hit the domain key.
- ✅ Step 6 (24.11): the domain is registered and the key is set. The new mode was checked in the browser on both pages: the start screen in the page language, answers from the knowledge base, conversation memory, an email by the team code.
- ✅ Step 9 (24.11): Tetiana signed off, and the widget now uses my server by default. The old mode is available with `?chat=old` until 30.11.
- ✅ Step 10, partly: the site (EN link to `/en/`, demo_text with the `team2026` code in both languages, the stack), the README.
- ⏳ After 30.11: remove `?chat=old`, `/api/create-session`, `/api/thread-items/`, their nginx routes and the `CHATKIT_WORKFLOW_ID*` variables.

## What the export got wrong and how it was fixed

1. **The export doesn't run as is.** For four agents the temperature is written as `0["5"]`, `0["1"]`, `0["3"]`, a generator bug that makes Python fail with a TypeError. The values 0.5, 0.1 and 0.3 were carried over.
2. **The export doesn't return the agent's answer.** Every branch ends with a stub `{"message": "Your return is on the way."}` from the End node. In Agent Builder the answer streamed into ChatKit; in code it doesn't. On my server the agent's answer streams into the widget through `stream_agent_response`.
3. **The classifier was matched as a string.** Any answer other than exactly `faq`, `documents` and so on (say, "FAQ" or "faq.") went to the refusal branch. Now the classifier returns a structured answer with five possible values.
4. **The staff code check in Templates Agent was broken.** The instruction contains an unfilled variable, `{{STAFF_CODE}}`. Now the code is checked in code (templates only if the code is in the message) and inserted into the instructions from `.env`.
5. **Hallucination Detection sat at the input.** This guardrail checks claims against the knowledge base, but it was getting the user's question, not the model's answer. Removed; the input keeps Jailbreak with the same settings (gpt-4.1-mini, threshold 0.7).
6. **Two models ran just to output fixed text.** The greeting called gpt-5.4 with reasoning, the guardrail refusal called gpt-4o-mini, though both instructions say "send only this text". Now they're fixed texts with no model call: faster and cheaper.
7. **The agents didn't remember the conversation.** The workflow passed the agent only the current message. Now the agent sees the last 20 messages of the thread; checked with a follow-up question.
8. **Answers without searching the knowledge base.** In testing, the English templates agent once wrote an email "on its own". Now agents with a knowledge base must search it first (`tool_choice="required"`; after the first call the SDK resets it by itself).
9. Added: English versions of all agents and knowledge bases, daily demo limits (per IP and in total), a message length limit.

## Steps

Who does what: **T** is Tetiana, **C** is Claude.

### 1. T · Export the current workflow to code (5 minutes, before 30 November)

This is the only thing that can't be done without you, or after 30 November: the export holds the exact agent instructions, guardrail settings and vector store ids.

1. platform.openai.com → Agent Builder → the "Know Yourself" («Познай себя») workflow (id `wf_69bd6b44…`).
2. At the top choose **Code** → **Agents SDK** → **Python**.
3. Copy all the code and save it to `Case_7_AI_assistant_for_technical_support_and_FAQs/project/agent-builder-export/workflow_ru.py` (or just send it to me in the chat: there are no secrets in the export, the API key comes from an environment variable).
4. Ideally, a screenshot of the whole workflow graph, for cross-checking.

### 2. C · Knowledge bases: two vector stores

- RU: `01_faq_knowledge_base.md`, `02_docs_and_instructions.md`, `03_email_templates.md` (as in the current workflow; if the export lists different files, follow the export).
- EN: files from `agent-builder-en/` (adapted: the Dolomites, Innsbruck).
- Created via the API with the key already on the server. The ids go to `~/apps/retreat/chatkit/.env`.

### 3. C · Agents in code (Agents SDK)

Ported from the export, one set per language:

- **Guardrail** at the input: checks for jailbreak and prompt injection before the agents. When it fires, a polite refusal in the page language.
- **Dispatcher** (the former classifier) with handoffs: FAQ Agent, Docs Agent, Templates Agent (only with the `team2026` code), Off Topic Handler. The greeting (Welcome Message) comes from the widget's start screen and the dispatcher's instruction.
- FAQ, Docs and Templates search their own vector store (`FileSearchTool`).
- RU texts come from the export unchanged. EN texts come from `agent-builder-en/SETUP.md`, checked against the export.
- The model is as in the export.

### 4. C · A ChatKit server instead of issuing a client_secret

- The `openai-chatkit` and `openai-agents` packages (checked versions 1.6.5 and 0.25.0), a FastAPI server in the same `retreat-chatkit` container.
- A `ChatKitServer` subclass: in `respond()`, thread history → `simple_to_agent_input` → `Runner.run_streamed` → `stream_agent_response` (as in the ChatKit docs).
- The language comes from the page (a request header) and is saved in the thread metadata, so a continued conversation stays in its language.
- Thread storage is SQLite on a volume. The package has no ready SQLite store, only an abstract `Store`, so I implement it myself.
- Demo limits like the other paid demos: per IP and per day, plus message length.

### 5. C · The widget

- `api: { url: "/api/chatkit", domainKey }` instead of `getClientSecret`.
- A start screen in the page language: a greeting and 3–4 ready questions (`startScreen.greeting`, `startScreen.prompts`), interface locale `ru` or `en`.

### 6. T · Register the domain and send the key (5 minutes)

Checked in the browser on 23.11: the `local-dev` placeholder doesn't pass. The widget calls `POST https://api.openai.com/v1/chatkit/domain_keys/verify`, gets a 400 and shows: "Domain verification failed for https://retreat.tatidzufri.com. Please register your domain at https://platform.openai.com/settings/organization/security/domain-allowlist". That page isn't in the docs; the address comes from the error text.

1. Open https://platform.openai.com/settings/organization/security/domain-allowlist (Settings → Organization → Security → Domain allowlist).
2. Add the domain `retreat.tatidzufri.com` (with `https://` if it asks for the protocol).
3. Copy the domain key and send it to me. It's public: it sits in the page code and isn't a secret.

Then C: puts the key into the widget and runs step 8 in the browser.

### 7. C · Server and nginx

- A new route, `/api/chatkit` → the `retreat-chatkit` container (currently `/api/` goes to the reviews API), with streaming responses (no buffering).
- An nginx request limit, as for the other paid demos.
- The old `/api/create-session` route stays until the check, so a rollback is possible.

### 8. C · Checks (RU and EN)

On both pages, `/` and `/en/`:

- a question about the program and prices: an answer from the knowledge base, in the page language, the EN one about the Dolomites;
- `team2026` + a request for an email template: Templates Agent;
- an off-topic question and "tell me about the sales funnel": a polite refusal;
- a jailbreak attempt: the guardrail fires;
- a second message in the same conversation: the context holds;
- a container restart: the history isn't lost.

### 9. T · Sign-off in the browser (10 minutes)

Go through the same points on the site yourself. If all is well, we switch for good.

### 10. C · Cleanup

- Remove `/api/create-session`, the `CHATKIT_WORKFLOW_ID*` variables and the old `client_secret` code.
- The case README: "agents on the Agents SDK and my own ChatKit server" instead of Agent Builder.
- The site: the EN link to `/en/` and the new `demo_text` for case 7, texts handed to the content factory.

## Timeline

| When | What |
|---|---|
| As soon as possible | Step 1 (T). Without it I'd port the instructions from the case description and risk losing details |
| Within 1–2 days after the export | Steps 2–8 (C) |
| Before 29 November | Step 9 (T) and the switch |
| After 30 November | Step 10 |

Steps 2, 4 and 5 can start before the export: only the agent instruction texts (step 3) depend on it.

## If something goes wrong

- Until the switch, the old widget works as before.
- After the switch, rolling back means restoring the previous `widget.js` and nginx route: copies are kept at deploy time. After 30 November there will be nothing to roll back to, so sign-off happens before that date.

## Sources

- Deprecations: https://developers.openai.com/api/docs/deprecations (the line "Nov 30, 2026 · Agent Builder · Agents SDK or ChatGPT Workspace Agents")
- Migration: https://developers.openai.com/api/docs/guides/agent-builder/migrate-from-agent-builder
- ChatKit with your own server: https://developers.openai.com/api/docs/guides/custom-chatkit
- Agents SDK: https://developers.openai.com/api/docs/guides/agents/quickstart
- Widget options: the `@openai/chatkit` 1.9.0 package types (`CustomApiConfig`: `url`, `domainKey`)
