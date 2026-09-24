# ABP Browser Extension - design and build plan

Status: design approved for build; Phase 0-1 in progress (see [Roadmap](#17-roadmap-phases-and-exit-criteria)).
Everything below is a requirement unless it says *deferred*. Where this plan and the code disagree, fix whichever is wrong and update this file.

## 0. What it is, in one paragraph

A Manifest V3 browser extension ("ABP Bridge") that pairs itself with the ABP desktop app, and turns the user's **real, logged-in browser** into a first-class ABP surface:
(1) ABP agents can **navigate and operate** the browser agentically (read, click, type, scroll, tabs, forms, files) under ABP's existing safety model;
(2) ABP can use **browser-based chat sessions** (Grok, Gemini, ChatGPT, Claude.ai, Perplexity, Copilot, ...) as models, through the user's own logged-in tabs;
(3) the extension can **run open models in the browser** (WebGPU/WASM) loaded from Hugging Face and other sources, and offer them to ABP;
(4) one **model gateway** joins all four kinds of intelligence - cloud APIs, local desktop models, in-browser models, browser-session agents - behind the OpenAI wire format ABP's agent loop already speaks.

Non-goals: no telemetry; no remote-hosted code (Chrome Web Store policy); no scraping of sites at scale; no defeating CAPTCHAs, paywalls, or bot detection; no handling of the user's passwords by the model, ever.

## 1. Design principles

1. **Zero manual steps.** Pairing, native-host registration, model download, adapter selection are automatic; the user approves, never copies tokens. (Standing ABP rule.)
2. **The desktop app is the brain and the policy authority.** The extension is a hardened actuator and sensor. Every decision that matters (approval, taint, credential guard, e-stop, audit) is made or double-checked in ABP; the extension also enforces the same rules locally so a compromised/bugged server cannot exceed the user's grants.
3. **Reuse before building.** ABP already has: `agent_runtime/browser.py` tools (`browser`, `browser_act`, `browser_handoff`), taint + approval (`taint.py`, `approval.py`), credential guard (`secrets_guard.py`), vault (`vault.py`), e-stop (`estop.py`), permission rules, model router, providers (OpenAI-compatible), `api_keys` table, audit log. The extension plugs into these; it does not fork them.
4. **Everything a page says is untrusted data.** Pages are prompt-injection vectors. Same rule as ABP's browser today, enforced at both ends.
5. **Least privilege, visibly.** Minimal manifest permissions; powerful ones (`debugger`, all-sites host access) are optional, requested at the moment of need, and explained. The user can always see and stop what the agent is doing.
6. **Deterministic and testable.** Every capability has an automated test against a real Chromium/Edge with the extension loaded. Site adapters are data with self-tests.
7. **Degrade, don't fail.** No WebGPU -> WASM; no debugger permission -> content-script input; no native host -> loopback WebSocket; Firefox -> reduced feature set, clearly labelled.

## 2. Architecture

```
 +----------------------------- browser --------------------------------+        +------------- ABP desktop app (Tauri + Python) -------------+
 |                                                                       |        |                                                            |
 |  Service worker (SW) "hub"  <---- chrome.runtime ports ---->  Side panel / Popup / Options (UI)   |        |  bot/browser_bridge.py  (hub, pairing, RPC, sessions)      |
 |    - bridge client (WS)  =====  JSON-RPC 2.0 over WebSocket  =========================================>  |  /api/browser/ws   /api/browser/*  /api/browser/v1/*       |
 |    - tab/agent manager                                                 |        |        |                    |                     |          |
 |    - policy enforcer (grants, sensitive-site blocklist, taint mirror)  |        |  agent tools: ext_browser*   model gateway (OpenAI-compat) |
 |    - audit log (local ring buffer)                                     |        |        |                    |                     |          |
 |         |                |                    |                        |        |  taint / approval / secrets_guard / estop / vault / audit  |
 |         v                v                    v                        |        |                                                            |
 |  Content scripts     chrome.debugger      Offscreen doc "engine"       |        |  model router / providers.yaml (API, Ollama, LM Studio...) |
 |  (per tab/frame:     (optional, CDP:      - Web Worker(s): WebLLM,     |        +------------------------------------------------------------+
 |   a11y snapshot,     trusted input,       transformers.js, wllama, ORT |                     ^
 |   actions, overlay,  screenshots,         - OPFS model cache           |                     | Native Messaging (bootstrap, pairing assist,
 |   web-session        network)             - download manager           |                     |  fallback transport) - tiny host launched by the browser
 |   adapters)                                                            |                     v
 +-----------------------------------------------------------------------+   bot/native_host.py (stdio <-> loopback ABP)
```

### 2.1 Components

| Component | Runs in | Responsibility |
|---|---|---|
| **Hub (service worker)** | extension SW | The only component that talks to ABP. Owns the bridge connection, request routing, tab registry, grants, policy enforcement, audit, keep-alive, reconnect. Stateless across SW restarts: all durable state in `chrome.storage.local`/`session` and IndexedDB. |
| **Content scripts** | every tab (`document_idle`, all frames, `MAIN` never unless required) | Page understanding (accessibility-tree snapshot with stable refs), element actions, in-page overlay (agent border, stop button, action toasts), web-session adapters (only on adapter hosts). |
| **Debugger driver** (optional) | SW via `chrome.debugger` | CDP: trusted input events (`Input.dispatchMouseEvent`/`KeyEvent`), `Accessibility.getFullAXTree`, `Page.captureScreenshot`, `Network` inspection, file chooser interception, dialogs, PDF. Used when content-script actions are insufficient. Shows Chrome's "debugging" infobar - explained to the user. |
| **Engine (offscreen document)** | `chrome.offscreen` (reason `WORKERS`; fallback: side panel) | Hosts inference Web Workers, model cache (OPFS), download manager, WebGPU device. Survives SW sleep. |
| **Side panel** | extension page | Chat (any model via the gateway), live agent activity timeline, approvals, per-tab agent control, model manager. |
| **Popup / Options** | extension pages | Connection state, pair/unpair, permissions & site rules, model catalog, adapter status, diagnostics export. |
| **Native host** | stdio process | Bootstraps pairing without a token, reports whether ABP runs, launches ABP if not running (opt-in), fallback transport. Registered by the desktop app per user (HKCU), allow-listing only the extension IDs. |
| **ABP bridge** | ABP Python | WebSocket hub, pairing, RPC, session model, OpenAI-compatible gateway, agent tools, dashboard/CLI/TUI/MCP surfaces. |

### 2.2 Repository layout

```
browser-extension/
  package.json  tsconfig.json  esbuild.config.mjs  vitest.config.ts  playwright.config.ts
  manifest/manifest.base.json  manifest.chromium.json  manifest.firefox.json   (merged at build)
  src/
    shared/        protocol.ts (types + zod-free validators), errors.ts, limits.ts, ids.ts, log.ts, urlpolicy.ts, redact.ts
    background/    index.ts hub.ts bridge.ts pairing.ts router.ts tabs.ts policy.ts audit.ts keepalive.ts debugger.ts native.ts
    content/       index.ts snapshot.ts refs.ts actions.ts overlay.ts frames.ts readability.ts
    adapters/      engine.ts (interpreter) registry.ts  defs/*.json (grok, gemini, chatgpt, claude, perplexity, copilot, ...)
    engine/        offscreen.html offscreen.ts worker-llm.ts worker-embed.ts runtimes/{webllm,transformers,wllama,ort}.ts
                   models/{catalog,download,cache,verify,hf}.ts  gateway.ts
    sidepanel/  popup/  options/  (small framework-free TS + web components; no runtime deps in pages)
  tests/  unit/ (vitest)  e2e/ (Playwright + real Edge/Chromium + fixture site)  fixtures/
  dist/   (build output; never committed)
bot/browser_bridge.py  bot/native_host.py  bot/dashboard/browser_api.py  bot/agent_runtime/ext_browser.py
tests/test_browser_bridge*.py
docs/browser-extension/  DESIGN.md  PROTOCOL.md (generated)  ADAPTERS.md  MODELS.md  PRIVACY.md
```

Toolchain: TypeScript strict, esbuild bundling (ES2022, no runtime framework in extension pages to keep review simple), Vitest (jsdom for DOM units), Playwright for E2E (persistent context, `--load-extension`, `channel: 'msedge'` or bundled Chromium, headed or `--headless=new`). Node >= 20. Lockfile committed. Dependencies limited to: `@mlc-ai/web-llm`, `@huggingface/transformers`, `@wllama/wllama`, `onnxruntime-web` (all bundled locally - no CDN loads), dev: `typescript`, `esbuild`, `vitest`, `@playwright/test`, `jsdom`. Every dependency pinned and license-checked.

## 3. Transports and connection lifecycle

### 3.1 Primary: WebSocket to loopback
`ws://127.0.0.1:<port>/api/browser/ws` (the dashboard port, default 8787; discovered, see 3.3). Loopback only: the bridge route rejects any non-loopback peer. Not exposed to LAN/Tailscale even when the dashboard listens on 0.0.0.0 (checked per connection against `request.client.host`, and again on the Origin header).

- **Origin check**: `Origin` must be `chrome-extension://<allow-listed-id>` / `moz-extension://<uuid>` registered at pairing. A web page cannot forge it.
- **Auth**: the paired **browser key** (an `api_keys` row of new kind `browser_ext`, never valid on any other route) sent as the first message (`hello`), not in the URL.
- **Scope**: `browser_ext` keys are accepted **only** on the bridge WebSocket and the `/api/browser/v1/*` gateway; every other route rejects them (extends the auth tiers fixed in the peer-key work).

### 3.2 Secondary: Native Messaging
Host `com.abp.bridge` (a small Python entry, `bot/native_host.py`, packaged with the desktop app) speaks the 4-byte-length-prefixed JSON stdio protocol. Uses: (a) **zero-touch pairing** (3.4), (b) ABP-not-running detection and optional launch, (c) fallback transport when a Private Network/loopback policy blocks WebSocket. Registered per user: `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.abp.bridge`, `...\Microsoft\Edge\...`, `...\BraveSoftware\Brave-Browser\...`, `...\Mozilla\NativeMessagingHosts\...` -> manifest JSON with `allowed_origins` (Chromium) / `allowed_extensions` (Firefox). The desktop app writes these on install/first run and repairs them on start (idempotent).

### 3.3 Discovery
Order: (1) last known port from `chrome.storage.local`; (2) native host `whereis` reply; (3) default 8787; (4) user-entered in options. A connection attempt is a `GET /api/browser/hello` (loopback, unauthenticated, returns only `{abp:true, version, protocol, pairing_open}`) then the WebSocket.

### 3.4 Pairing (no tokens typed, ever)
1. Extension installed -> options page opens "Connect to ABP".
2. Extension asks the native host `pair.request {extension_id, browser, version}`. The host calls ABP `POST /api/browser/pair/request` on loopback. ABP shows a **desktop approval** (tray notification + dialog: "Chrome extension 'ABP Bridge' (id abc...) wants to connect. Allow?"). Approval is required unless the ID is on the first-party allow-list *and* the desktop app is configured "auto-approve first-party extension" (default off).
3. On approve, ABP mints a browser key + a per-pairing **secret** and returns them to the host -> extension via native messaging (never over the web). Key stored in `chrome.storage.local` (extension-private; not readable by pages).
4. **No native host available** (e.g. Firefox without registration, locked-down machine): fallback code flow - desktop app shows a 6-digit code (valid 2 minutes, one attempt per second, 5 attempts) that the user types in the extension; the code is bound to a nonce and the extension ID (verified from Origin).
5. Unpair from either side revokes the key immediately; the bridge closes the socket; the extension wipes its state.
6. Each ABP install has a stable **server id** the extension pins after pairing (TOFU): a different server on the same port is refused.

### 3.5 Session, keep-alive, reconnect
- MV3 SW is killed after ~30 s idle. WebSocket traffic resets the timer (Chrome 116+); the hub sends an application `ping` every 20 s while connected and uses `chrome.alarms` (30 s) as a wake-up backstop. Long jobs (model inference, downloads) live in the **offscreen document**, not the SW.
- Reconnect: exponential backoff 0.5 s -> 30 s with jitter; immediate retry on `runtime.onStartup`, `onInstalled`, network `online` event, alarm, or user action.
- **Resume**: `hello` carries `resume: {session_id, last_seq_seen}`; the bridge replays un-acked server->extension requests (idempotency keys make replays safe) and marks unfinished ones `orphaned` if the extension came back empty.
- One live connection per browser profile; a second connection from the same profile supersedes the first.

## 4. Wire protocol (v1)

JSON-RPC 2.0 objects over WebSocket text frames, plus one binary frame type for bulk data. Every message has `v:1`. IDs are ULIDs. Either side may send requests; both sides answer.

```jsonc
// request            {"v":1,"id":"01H..","method":"tab.snapshot","params":{...},"ctx":{"session":"s1","trace":"..","deadline_ms":30000,"idem":"01H.."}}
// response           {"v":1,"id":"01H..","result":{...}}      or   {"v":1,"id":"01H..","error":{"code":"E_PAGE_CHANGED","message":"...","data":{}}}
// notification       {"v":1,"method":"event.tab.updated","params":{...}}          (no id, no reply)
// stream chunk       {"v":1,"id":"01H..","stream":{"seq":7,"data":{...},"done":false}}   (responses to streaming methods)
// binary frame       [4-byte header-length][JSON header {"id","kind":"image/png","seq","done"}][payload]   (screenshots, model shards)
```
Limits: text frame <= 1 MiB (larger -> binary chunks of 256 KiB), request deadline default 30 s (max 10 min), in-flight per connection <= 64, backpressure via `credit` messages on streams.

### 4.1 Handshake
`hello` (ext -> abp): `{protocol:[1], ext:{id,name,version,browser,os}, key, capabilities:{debugger,webgpu,webgpu_f16,offscreen,sidepanel,native}, resume?}`.
`hello.ok` (abp -> ext): `{server_id, abp_version, protocol:1, session, policy:{...effective}, features:{...}}`. Unknown protocol -> `E_PROTOCOL` and the extension shows "update ABP / update extension". Version rule: additive changes only within `v:1`; unknown fields ignored; unknown methods -> `E_METHOD`.

### 4.2 Methods - ABP -> extension (the actuator surface)
| Method | Purpose |
|---|---|
| `tabs.list` `tabs.open` `tabs.close` `tabs.focus` `tabs.group` `window.list/open/close/resize` | tab/window control (agent tabs live in a dedicated **tab group** "ABP agent") |
| `tab.navigate {url, wait}` `tab.back/forward/reload` `tab.wait {for: load\|idle\|selector\|text\|url\|ms}` | navigation and waits |
| `tab.snapshot {mode, max_elements, frames, include}` | numbered-element snapshot (see 5.2) |
| `tab.text {selector?, readability?}` `tab.screenshot {region, scale, format}` `tab.pdf` | read |
| `tab.act {ref, action, args}` where action in `click dblclick rightclick hover type clear select check press scroll drag focus upload fill_credential` | act; refs come from the last snapshot; stale refs -> `E_STALE_REF` with a fresh snapshot attached |
| `tab.eval` | **disabled by default**, arbitrary JS; behind an explicit grant + always-ask (kept for power users/tests) |
| `page.find {query}` | fuzzy locate (role/name/text) -> refs |
| `forms.fill {fields, submit?}` | batch fill with per-field checks (secret fields refused) |
| `downloads.list/accept/cancel`, `files.upload {ref, path\|blob_id}` | downloads refused unless granted; uploads only from ABP-approved paths |
| `history.search`, `bookmarks.search` | optional read-only, off by default |
| `session.mark {label}` `session.record {start\|stop}` | replay tracks |
| `ui.notify {title, body, approval?}` `ui.overlay {tab, state}` | user-facing prompts |
| `policy.update` | push effective policy |
| `models.*` `web.*` | see sections 7-8 |
| `ping`, `diag.get` | health |

### 4.3 Methods/notifications - extension -> ABP
`event.tab.created/updated/removed/activated`, `event.nav.committed`, `event.dialog`, `event.download`, `event.user.stop` (the user pressed stop -> ABP e-stop for that session), `event.user.takeover`, `approval.request/response`, `chat.send {model,messages,...}` (side-panel chat through the gateway, 8.3), `models.report {capabilities, installed}`, `audit.push`, `diag.report`.

### 4.4 Errors (stable codes)
`E_AUTH E_PROTOCOL E_METHOD E_PARAMS E_TIMEOUT E_CANCELLED E_NO_TAB E_STALE_REF E_NOT_ALLOWED (policy) E_NEEDS_APPROVAL E_SENSITIVE_SITE E_PAGE_CHANGED E_DEBUGGER_UNAVAILABLE E_NOT_INTERACTABLE E_BLOCKED_BY_PAGE E_TOO_LARGE E_BUSY E_ADAPTER_BROKEN E_NOT_LOGGED_IN E_RATE_LIMITED E_MODEL_UNAVAILABLE E_OOM E_INTERNAL`. Each carries `retryable: bool` and an actionable `hint`.

## 5. Agentic navigation

### 5.1 Tab model and scope
- The agent operates in its own **tab group "ABP agent"** (colored; badge `ABP`). Tabs it opens join the group. By default it cannot touch tabs outside the group.
- **Attach to current tab** is an explicit per-session user action (popup button / side-panel button / ABP prompt approved by the user); it grants that one tab, revocable, and expires when the tab navigates to a different origin unless "follow" is enabled.
- Incognito: extension disabled unless the user enables "Allow in Incognito"; ABP treats incognito tabs as `sensitive` (never sent to cloud models by default).
- Multi-window/multi-profile: each browser profile is one bridge connection; ABP addresses `profile:tab:frame`.

### 5.2 Snapshot format (the agent's eyes)
Built in the content script from the DOM + computed accessibility semantics (role, name, description, state, value, checked, expanded, disabled, required, href, input type/label, bounding box, in-viewport, occluded), **piercing open shadow roots and same-origin iframes**; cross-origin frames are snapshotted by their own content script and stitched by frame id. Output:

```
[url, title, scroll(y/height), viewport, focus]
@e12 button "Sign in" (disabled=false) [in-view]
@e13 textbox "Email" (required, value="") [in-view]
@e14 link "Pricing" -> /pricing
...
text: <readability-extracted visible text, capped>
```
- **Refs** `@eN` are stable within a snapshot generation and re-resolved by a **locator bundle** (frame path + CSS path + role/name + text hash + bbox) so small DOM changes do not invalidate them; a failed resolve returns `E_STALE_REF` plus a new snapshot rather than clicking the wrong thing (a wrong click is worse than an error).
- `max_elements` default 80 (setting `browser.max_elements` reused); ranking = in-viewport first, then interactive by role weight; `mode: "full"` paginates.
- Optional **vision**: `tab.screenshot` with set-of-marks overlay (refs drawn on the image) for vision-capable models.
- Large/complex pages: virtualized lists get an explicit "N more items below, scroll to load" marker; canvas/`<video>` are reported as opaque regions with their bbox; PDFs/`<embed>` via `tab.pdf` text extraction.

### 5.3 Actions and fidelity
Two engines, chosen automatically per action, `auto` by default:
1. **Content-script (default, no scary permission)**: scrolls into view, computes actionability (visible, enabled, not occluded - `elementFromPoint` hit-test, not `pointer-events:none`, stable for 2 frames), then dispatches a full realistic event sequence (`pointerdown/mousedown/pointerup/mouseup/click`, `input`/`change` with the native value setter for React/Vue-controlled inputs, `keydown/keypress/keyup`). Handles `contenteditable`, select, checkbox/radio, date inputs, sliders.
2. **Debugger/CDP (optional, when granted)**: trusted (`isTrusted`) input for sites that ignore synthetic events, file choosers, drag & drop, canvas apps, clipboard, dialogs, screenshots of arbitrary regions, network idle detection. Attached lazily per tab, detached when idle; the infobar is expected and documented.

Auto-wait after every action: settle on `navigation | network idle (<=2 in-flight for 500 ms) | DOM quiet 300 ms`, capped by `wait.timeout` (default 8 s); returns a **delta** (what changed: new URL, new/removed elements, dialogs, downloads) so the model rarely needs a full re-snapshot. Retries: an action is retried once on `E_NOT_INTERACTABLE` after scroll-and-settle; never on side-effecting `submit`-class clicks (idempotency).

### 5.4 Hard cases (each has a fixture test)
Shadow DOM (open), same-origin + cross-origin iframes, dialogs (`alert/confirm/prompt/beforeunload` - surfaced as events and answered by policy), new-tab/popup handling (adopted into the group when opened by an agent tab), file upload/download, drag & drop, infinite scroll, sticky overlays/cookie banners (a policy-gated "dismiss consent" helper that prefers *reject/necessary only*), SPA route changes without load events, `contenteditable` rich editors, autofocus traps, iframes with `sandbox`, PDFs, `about:blank`/`chrome://` (refused), tab crash (`E_NO_TAB` + recovery: reopen at last URL on request).

### 5.5 Recording and replay
Every session can record: actions, snapshots (compressed), screenshots on demand, network summary. Replays run in a fixture harness (regression tests for agent skills) and are exportable to ABP routines (`routine_save`) so "do it once, run it every Monday" works against the real browser.

## 6. Safety model (the part that must be right)

### 6.1 Layers (every action passes all)
1. **Grant scope** (extension-enforced): which tabs/origins the session may touch (5.1) and which capability classes are enabled: `read`, `interact` (click/type), `navigate`, `forms` (submit), `downloads`, `uploads`, `eval`, `history`.
2. **Sensitive-site blocklist** (extension-enforced, ABP-updatable, user-editable): banks/payments/tax, password managers, email/webmail compose pages, admin consoles of cloud providers, identity providers' password/2FA pages, `chrome://`, `edge://`, `about:`, extension pages, other extensions' stores' settings, and any page containing a password/OTP/card field (field-level detection). Reading is allowed only with an explicit per-site user grant; acting never on password/OTP/card fields.
3. **ABP permission layer**: the tool call goes through `permissions.py` (allow/ask/deny rules incl. `tool: ext_browser_act` with `match: <host>`), plan mode (read-only), `bypass` never removes the always-ask set (handoff, submit-on-sensitive, eval, uploads outside workspace, downloads).
4. **Taint**: reading any page on a host not in `trusted_sites` taints the session (same mechanism as `web_fetch`/`browser`) -> subsequent state-changing calls need human approval. The extension mirrors a `tainted` flag and refuses `interact/forms/navigate-to-new-origin` without an approval token from ABP when tainted.
5. **Credential guard**: outbound text/type args scanned for ABP secrets (existing `secrets_guard`). Stored logins use `fill_credential {entry}`: ABP resolves from the vault and the *extension* fills only if the tab's origin equals the entry's origin (eTLD+1 + scheme check); the value never enters the model context or the audit log.
6. **Prompt-injection controls**: page text is delivered to the model inside an explicit untrusted-content envelope; instructions found in pages cannot change grants/policy; cross-origin navigations and form submissions initiated after reading untrusted text on a *different* origin are held for approval ("data exfiltration guard"); links/forms whose target host differs from the page host are flagged in the snapshot.
7. **Approvals**: routed to whichever surface the user is on - side panel, desktop notification, Telegram/etc. via ABP's existing approval plumbing. Each shows: action, target element (with screenshot crop), origin, why.
8. **Rate limits & budgets**: actions/minute, page loads/minute, max tabs (default 5), max session duration, max navigations to new origins; exceeding -> `E_RATE_LIMITED` (a runaway loop cannot hammer a site). Loop guard reuse (`loop_guard.py`).
9. **E-stop**: toolbar button, `Esc` x2 in the page, overlay Stop, ABP tray "Stop all agents" (`estop.py`), and a keyboard command. Stops in-flight actions, detaches debugger, clears grants for the session, closes no user tabs.
10. **Visibility**: colored tab group, animated page border + "ABP is controlling this tab - Stop / Take over" pill, action toasts, live timeline in the side panel. User input during an agent action pauses the agent (takeover detection) and offers Resume.
11. **Audit**: every request/decision/result written locally (ring buffer, 10k entries, IndexedDB) and to ABP's audit log; secrets/values redacted; exportable; retention configurable.

### 6.2 Egress policy (where page content may go)
Each model has a trust class: `local` (in-browser, desktop-local), `lan/private endpoint`, `cloud-api`, `web-session` (third-party site). Each page/session has a sensitivity: `normal`, `sensitive` (matches 6.1.2 or incognito or user-marked), `never-share`. Default matrix: sensitive/never-share content is only ever given to `local` models; the extension **strips** page content from anything bound for a disallowed class and tells the agent it was withheld. Users can edit the matrix per site.

### 6.3 Privacy
No analytics, no crash reporting to third parties; diagnostics are local files the user chooses to export. No history/cookie/password access. Screenshots and snapshots are held in memory and (only when recording) in the local store; never sent anywhere except to the model the user's policy allows. Privacy policy (`PRIVACY.md`) is written before store submission and is machine-checked against the manifest permission list in CI.

### 6.4 Threat model (abridged)
| Threat | Mitigation |
|---|---|
| Malicious web page sends messages to the extension / forges commands | No `externally_connectable` except an allow-listed pairing page; content scripts accept commands only from the SW (sender.id check); page can't reach the bridge |
| Local malware / other local user connects to the bridge | loopback-only + Origin allow-list + pairing key + per-server pinning + approval on pair; keys revocable; rate-limited auth failures |
| Web page prompt-injects the agent | untrusted envelope, taint, approval gates, exfil guard, sensitive-site blocklist, no secret access |
| Compromised/buggy ABP server drives the browser beyond the user's intent | extension-side grants, sensitive blocklist, rate limits, always-ask set, visible control indicators, kill switch |
| Extension supply chain | pinned deps, reproducible build, no remote code, signed store release, SBOM |
| Model download tampering | HTTPS only, allow-listed hosts, sha256 from the hub's LFS metadata verified, size caps, format allow-list (no pickles) |
| XSS in extension pages | strict CSP (`script-src 'self' 'wasm-unsafe-eval'`), no inline script, DOM built with `textContent`/templates, page text never `innerHTML`'d |
| Adapter hijack (fake update swaps selectors to exfiltrate) | adapters are **data** interpreted by bundled code, signed (Ed25519) by the ABP release key, verified before use, cannot contain code or arbitrary URLs outside the adapter's own hosts |

## 7. Browser-session agents (Grok, Gemini, ChatGPT, Claude.ai, Perplexity, Copilot, ...)

Goal: a chat product the user is already logged into becomes a **selectable ABP model**, using their own account and tab. This is UI automation of the user's own session - disclosed, opt-in per site, rate-limited, off by default.

### 7.1 Adapter architecture
An adapter is a **JSON definition** (no code) interpreted by `adapters/engine.ts`:
```jsonc
{ "id":"grok","name":"Grok","hosts":["grok.com","x.com/i/grok"],"version":3,
  "login_check":{"absent":"[data-testid=login]","present":"textarea"},
  "new_chat":{"click":"[aria-label='New chat']","or_navigate":"https://grok.com/"},
  "compose":{"input":"textarea","mode":"paste|type","submit":{"key":"Enter"|"click":"button[type=submit]"}},
  "stream":{"container":"[data-message-author-role=assistant]:last-of-type","done":{"absent":"[aria-label='Stop']","quiet_ms":1200}},
  "extract":{"format":"markdown","strip":[".copy-button"]},
  "attachments":{"input":"input[type=file]"},
  "models":["fast","expert"], "limits":{"min_interval_ms":4000,"max_prompt_chars":30000},
  "selftest":{"prompt":"Reply with exactly: ABP-OK","expect":"ABP-OK"} }
```
Selectors are a **ranked list** (aria/role/text first, CSS last) with fallbacks; the engine reports which fallback matched so drift is visible before it breaks. Each adapter ships a **self-test** run on demand and daily (only if the tab is open); failures mark the adapter `degraded` and surface in the dashboard with the last-good version. Adapters update via the normal extension release *and* via signed data updates from ABP (7.4).

### 7.2 Session lifecycle
`web.sessions.list` -> find matching tabs; `web.session.open {adapter}` opens a background tab (in a separate "ABP web-agents" tab group) if none; verifies login (`E_NOT_LOGGED_IN` -> hand-off to the user, never types credentials); one **in-flight prompt per tab** (queue), fresh chat per ABP conversation unless `continue: true`; sends prompt by paste (fast, robust) with typing fallback; waits for completion by DOM signals (`done` conditions) with a hard timeout; extracts markdown; captures citations/links; detects refusals/rate-limit banners -> `E_RATE_LIMITED`; supports cancel (clicks stop). Streaming to ABP is best-effort: incremental text diffs every 150 ms.

### 7.3 Mapping to the OpenAI wire format
The gateway (section 9) exposes `model: "web/grok"`, `"web/gemini"`, ... Chat-completions messages are flattened to a transcript prompt with a stable header. **Tool calling** for web models is emulated: ABP injects a compact tool protocol into the prompt (`<abp_tool name=...>{json}</abp_tool>` blocks, few-shot) and parses the reply; a malformed reply triggers one repair turn. These models are flagged `tools: "emulated"`, `context: unknown`, `vision: per-adapter` so the router and users see the limits. They are never chosen by "auto" routing unless the user opted in, and never for `sensitive` content (6.2).

### 7.4 Compliance guardrails
Per-site opt-in with a plain-language notice ("uses your logged-in session; the provider's terms apply; rate limits are enforced"); default `min_interval` per adapter; no parallel prompts on one account; no automated account creation, no CAPTCHA solving (handoff); kill switch to disable all web-session use; adapters carry a `tos_note` shown in the UI.

## 8. Models in the browser

### 8.1 Runtimes (bundled, local files only)
| Runtime | Formats | Use |
|---|---|---|
| **WebLLM (MLC)** on WebGPU | MLC-compiled weights | fastest chat models (Llama/Qwen/Phi/Gemma families) |
| **transformers.js** (ONNX RT Web; WebGPU or WASM) | ONNX | embeddings, whisper STT, TTS, classification, small LLMs, vision, OCR |
| **wllama** (llama.cpp WASM) | GGUF | broadest model choice, CPU/WASM, multithread when cross-origin isolated |
| **ONNX Runtime Web** direct | ONNX | custom pipelines |
| *deferred:* MediaPipe LLM, Chrome built-in AI (Gemini Nano via `LanguageModel` API) | | Chrome Prompt API exposed as a `browser-native/gemini-nano` model when present |

Runtime selection: `capabilities()` probes `navigator.gpu` adapter (limits, `shader-f16`, `maxBufferSize`, `maxStorageBufferBindingSize`), device memory, hardware concurrency, cross-origin isolation, OPFS quota -> a **fit score** per catalog entry (fits VRAM? fits RAM? expected tokens/s bucket) shown in the UI and used by the router.

### 8.2 Model sources and catalog
- **Hugging Face Hub**: search via `https://huggingface.co/api/models?search=&filter=` (filters: `onnx`, `gguf`, `transformers.js`, `text-generation`, `feature-extraction`, `automatic-speech-recognition`, ...). Metadata via `/api/models/<repo>?blobs=true` gives file sizes and LFS `sha256`. Files via `/resolve/<revision>/<path>` (revision pinned to a commit hash at install time, not `main`).
- Other sources: MLC prebuilt list, any HTTPS URL to a GGUF/ONNX **from an allow-listed host** (user can add hosts explicitly), and a **local ABP source** (files on the desktop machine served through the bridge - lets desktop-downloaded models feed the browser without re-downloading).
- A curated **catalog** (JSON, signed like adapters) recommends models per task and device class (e.g. "chat: Qwen2.5-1.5B q4 ~1 GB WebGPU", "embed: bge-small", "stt: whisper-tiny/base", "tts: kokoro/piper", "ocr", "vision"), each with license, size, context, and tested-on notes. Anything else can be added by repo id with a warning about untested models.

### 8.3 Download manager (offscreen)
Resumable (HTTP `Range`), parallel (<=3 connections), verified (sha256 of each LFS file; size match; mismatch -> delete + `E_INTEGRITY`), cached in **OPFS** (fast, quota-managed) with `navigator.storage.persist()` requested; disk-budget setting (default 20 GB) with LRU eviction and a manager UI (size, last used, delete); progress events to the side panel; pause/resume/cancel; survives SW restarts (state in IndexedDB); refuses formats that execute code (no pickle/`.bin` torch, no `trust_remote_code`); shows license + gated-repo handling (HF token stored only in `chrome.storage.local`, used only for `huggingface.co`, never sent to ABP unless the user opts in "share token with ABP for desktop downloads").

### 8.4 Inference service (offscreen + workers)
One engine document, N model workers (default 1 LLM + 1 utility). Lifecycle: `load` (warm from OPFS), `generate` (streaming tokens, stop sequences, temperature/top-p, JSON-schema/grammar-constrained output where the runtime supports it, seed), `embed`, `transcribe`, `speak`, `unload` (idle timeout, memory pressure via `performance.measureUserAgentSpecificMemory`/errors). GPU device loss and OOM are caught -> worker restart -> `E_OOM`/retry on smaller quantization suggestion. Concurrency: queue per model, priority (interactive > agent > batch), cancellation propagates to the worker. KV-cache/session reuse for multi-turn. Throughput/telemetry kept locally (tokens/s) and reported to ABP for routing.

### 8.5 What ABP sees
Each installed/loaded model is reported to ABP as a provider entry `browser-local/<model-id>` with capabilities `{tools: json-mode|emulated, vision, ctx, embed, stt, tts, speed_hint, trust: local}`. ABP's `providers.yaml`/model router treat it like any other OpenAI-compatible provider (via the gateway), including eval recording (`abp_agenteval --record`) so quality is measured, not assumed.

## 9. The model gateway (one bridge for every kind of model)

ABP serves an **OpenAI-compatible API** at `/api/browser/v1/` (`/chat/completions` incl. SSE streaming, `/embeddings`, `/audio/transcriptions`, `/audio/speech`, `/models`) authenticated by the browser key (for the extension) or the dashboard token (for ABP's own agent loop). It routes by model id prefix:

| Prefix | Backend | Path |
|---|---|---|
| `api/...` `openai/...` `openrouter/...` | cloud providers | existing providers.yaml + transports |
| `local/...` | desktop local models | Ollama / LM Studio / llama.cpp server / vLLM already in providers.yaml |
| `browser-local/...` | in-browser models | bridge -> extension engine (streaming over the bridge, back-pressured) |
| `web/...` | browser-session agents | bridge -> adapter engine |
| `auto` | router | `model_router.recommend()` honoring trust/egress policy, availability, quota |

Consequences: (a) the existing **native agent loop, sub-agents, swarms, routines** can use browser-local and web models with **no code changes** (register `browser-local` and `web` as providers whose `base_url` is the gateway); (b) the extension's side-panel chat and any web page tool the user allows can use **any** ABP model through the same door; (c) fallbacks chain across kinds (e.g. web/gemini -> local/qwen -> api/...); (d) usage/limits/cost are accounted uniformly (`usage_limits.py`).
Reverse direction (deferred, opt-in per site): a page-facing `window.abp` provider injected on allow-listed origins so web apps can call the user's ABP models with user approval per origin (like `window.ethereum` prompts) - designed, not in v1.

## 10. User interface

- **Side panel** (primary): connection chip; chat with model picker (grouped by trust class, with fit/speed badges); "Ask about this page" (page/selection/screenshot context toggles showing exactly what will be sent); agent mode ("Do this for me") with live **timeline** (each action with thumbnail, target, result, undo where possible), approvals inline, Stop/Take over; downloads/models manager; web-session status.
- **Popup**: connected/paired state, current tab grant toggle ("Let ABP use this tab"), stop-all, quick model, open side panel.
- **Options**: pairing, permission classes, sensitive-site list, egress matrix, trusted sites, adapters (enable/self-test/notes), model catalog + storage, hotkeys, diagnostics (export bundle), advanced (transport, port, native host repair).
- **In-page overlay** (Shadow DOM, closed, high z-index): border + pill + toasts; never intercepts page focus; respects `prefers-reduced-motion`.
- **Omnibox** `abp <task>`, **context menu** ("Ask ABP about selection", "Save page to ABP memory"), **commands** (open panel, stop all, toggle tab grant).
- Accessibility: full keyboard nav, ARIA live region for the timeline, contrast >= AA, RTL-safe, i18n via `_locales` (en first; strings externalised from day one).

## 11. ABP desktop side

New/changed code (each with tests, hot-reload classification, CHANGELOG):
- `bot/browser_bridge.py`: connection hub (per profile), pairing store, RPC client with deadlines/idempotency/replay, event bus, session objects, audit hook, capability cache; `browser_ext` key kind in `bot/db.py` (+ auth-tier rules: valid only on bridge routes).
- `bot/dashboard/browser_api.py`: `GET /api/browser/hello`, pairing (`/pair/request|approve|deny|code`), `GET /api/browser/status`, `GET/DELETE /api/browser/browsers`, sessions, adapters (list/self-test/enable), models (installed/catalog/download commands), policy (sensitive sites, egress matrix), `WS /api/browser/ws`, gateway `/api/browser/v1/*`.
- `bot/native_host.py` + installer hooks (NSIS `hooks.nsh` + first-run repair) writing the native-messaging manifests/registry keys.
- `bot/agent_runtime/ext_browser.py`: tools `ext_browser` (read/look/snapshot/text/screenshot/tabs) and `ext_browser_act` (click/type/select/press/scroll/upload/fill_credential/navigate) and `ext_browser_handoff`, registered like `browser.py`, honoring taint/permissions/always-ask, `browser.trusted_sites`, `browser.*` settings extended with `browser.target: playwright|extension|auto` (auto prefers the extension when connected, since it is the user's real logged-in browser).
- Settings schema (`settings_schema.py`) new group "Browser extension": enable bridge, auto-approve first-party pairing, allowed capability classes, default egress matrix, web-session sites, max tabs, action rate limits.
- Surfaces: dashboard/desktop **"Browser"** page (paired browsers, live sessions, timeline, adapters, models, policy), TUI screen, `abp_cli browser ...`, MCP tools, tray item "Stop all browser agents".
- Model gateway routes and provider registration (`browser-local`, `web`) with capability metadata.

## 12. Manifest (Chromium) and permissions

```jsonc
{ "manifest_version":3, "name":"ABP Bridge", "version":"0.1.0", "minimum_chrome_version":"116",
  "key":"<dev key for a stable id>",
  "background":{"service_worker":"background.js","type":"module"},
  "action":{"default_popup":"popup.html"}, "side_panel":{"default_path":"sidepanel.html"},
  "options_page":"options.html",
  "permissions":["storage","tabs","tabGroups","scripting","activeTab","alarms","offscreen","sidePanel","nativeMessaging","notifications","contextMenus","webNavigation","unlimitedStorage"],
  "optional_permissions":["debugger","downloads","history","bookmarks","clipboardRead","clipboardWrite"],
  "host_permissions":["http://127.0.0.1/*","http://localhost/*"],
  "optional_host_permissions":["https://*/*","http://*/*"],
  "content_scripts":[],            // injected on demand via chrome.scripting after a grant; adapters register per-host scripts only after the user enables that site
  "commands":{"stop-all":{"suggested_key":{"default":"Ctrl+Shift+Period"},"description":"Stop all ABP agents"}},
  "content_security_policy":{"extension_pages":"script-src 'self' 'wasm-unsafe-eval'; object-src 'self'"},
  "web_accessible_resources":[],   // none; overlay is created by content script
  "omnibox":{"keyword":"abp"} }
```
Rationale per permission is recorded in `PRIVACY.md` and checked in CI. **All-sites host access is optional**: the user grants it once ("Allow ABP to work on all sites") or per site; until then only `activeTab`-style, user-initiated use works. `debugger` is requested the first time an action needs trusted input. Firefox: `manifest.firefox.json` swaps `background.scripts`, `sidebar_action` for `side_panel`, no `offscreen` (engine runs in a hidden extension page), no `debugger` (content-script actions only, documented gap), `browser_specific_settings.gecko.id`.

## 13. Reliability and performance budgets

| Item | Budget |
|---|---|
| SW cold start to connected | <= 500 ms after wake, <= 2 s after browser start |
| Snapshot latency (typical page, 80 elements) | p50 <= 120 ms, p95 <= 400 ms; 1,000-element page <= 1.2 s |
| Action round trip (click + settle) | p50 <= 600 ms excluding page-imposed waits |
| Bridge overhead | <= 5 ms per RPC on loopback |
| Idle CPU / memory | < 1% CPU, < 60 MB (engine unloaded) |
| First token (in-browser 1B q4 model, warm) | <= 1.5 s on a mid GPU |
| Reconnect after ABP restart | <= 5 s |
Failure handling: every RPC has a deadline and a typed error; SW restarts never lose the bridge for > backoff; unfinished agent steps are reported `orphaned` (never silently retried when side-effecting); engine crashes restart workers; offline/ABP-down states are shown, not swallowed.

## 14. Cross-browser

| Browser | Support |
|---|---|
| Chrome, Edge, Brave, Opera, Vivaldi, Arc (Chromium >= 116) | full (MV3; sidePanel; offscreen; optional debugger) |
| Firefox (>= 128, MV3 event pages) | core agentic control via content scripts, models in a hidden extension page (WebGPU per Firefox availability), web sessions; no debugger; sidebar instead of side panel; documented gaps |
| Safari (macOS/iOS) | *deferred* (Xcode wrapper, different permission model) |
Feature detection everywhere; the options page shows a per-browser capability table.

## 15. Build, test, release

### 15.1 Tests (all automated, in the local pipeline; a phase is not done until its tests pass)
1. **Unit (Vitest)**: protocol encode/decode & validation, error mapping, url policy, redaction, snapshot ranking, locator resolution, adapter engine (against recorded DOM fixtures), download verifier, catalog fit scoring, rate limiter, reconnect/backoff (fake timers).
2. **Integration (Python)**: bridge against a **fake extension client** (websockets): pairing flows (approve/deny/code/expiry/brute force), auth tiers (a `browser_ext` key rejected everywhere else; peers/phones rejected on the bridge), RPC deadlines/replay/idempotency, gateway routing incl. SSE streaming and cancellation, taint/approval interplay with `ext_browser*` tools.
3. **End-to-end (Playwright + real Edge/Chromium with the built extension loaded + the real ABP bridge in-process)**: a fixture website (forms, shadow DOM, iframes, dialogs, SPA routing, infinite scroll, file up/download, cookie banner, hostile prompt-injection page, fake login) + each hard case in 5.4; safety cases (sensitive site refuses; password field refused; tainted session requires approval; stale ref never clicks wrong element; e-stop halts within 250 ms; cross-origin exfil held); web-session adapters against **local mock chat sites** that emulate each provider's DOM (real sites are covered by opt-in manual self-tests, never CI); in-browser model test with a tiny ONNX/GGUF fixture (real WebGPU or WASM fallback) for load/generate/cancel/unload and download resume/verify against a local HF-compatible mock server.
4. **Manifest/policy checks**: permissions vs `PRIVACY.md`, no remote code, CSP, no `eval`/`Function`, bundle size budget, license audit, `web-ext lint` (Firefox), Chrome Web Store validation.
5. **Live checks on the dev machine** (recorded in the CHANGELOG honestly): real Edge with the desktop app, pairing via native host, a real navigation task, a real in-browser model from Hugging Face, and - opt-in, manual - one real web-session round trip.

### 15.2 Release
Reproducible build (`npm ci && npm run build`), version in lockstep semantic (`ext X.Y` <-> `protocol N` <-> ABP min version matrix in the hello handshake), signed store packages (Chrome Web Store, Edge Add-ons, AMO), SBOM, changelog. The desktop app's "Browser" page detects installed browsers and offers **one-click install** (opens the store listing / registers the external-install manifest where the OS allows), then completes native-host registration and pairing automatically. Dev builds load unpacked with a fixed key so the extension ID (and native-host allow-list) is stable.

## 16. Decisions, risks and open questions

Decisions: JSON-RPC over WebSocket + native messaging bootstrap; adapters as signed data (store-policy safe); OpenAI-compatible gateway as the single model bridge; OPFS for model cache; content-script actions by default with optional CDP; extension-side policy enforcement in addition to ABP's.
Risks and mitigations: (1) site UI drift breaks adapters -> ranked selectors, self-tests, signed data hot-fixes, degraded state; (2) provider ToS/anti-automation changes -> opt-in, rate limits, kill switch, honest UI notice; (3) Chrome policy on remote code/automation -> no remote code, clear single-purpose description, privacy policy, optional permissions; (4) WebGPU variability -> fit scoring + WASM fallback + clear errors; (5) MV3 SW lifetime -> keep-alive + offscreen; (6) debugger infobar UX -> optional and explained; (7) huge models vs memory -> fit score, quotas, eviction.
Open questions (decided defaults in brackets): auto-approve first-party pairing [off]; web-session models in `auto` routing [off]; default all-sites grant [ask on first use]; telemetry [none].

## 17. Roadmap, phases, and exit criteria

Each phase ends with: all its tests green in the local pipeline, docs updated, CHANGELOG entry that states what was and was not verified live.

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 Foundations** | repo/toolchain; shared protocol + validators; ABP `browser_bridge` (hub, WS, auth tier `browser_ext`, RPC, pairing via code + native host request path); options/popup shell; fake-extension integration tests | pairing approve/deny/expiry/brute-force tests; a `browser_ext` key is refused on every non-bridge route; reconnect/resume tests |
| **P1 Agentic core** | content-script snapshot + refs + actions; tab group/scope model; auto-wait + deltas; safety layers 1-5, 9-11 (grants, sensitive blocklist, taint/approval, credential fill, e-stop, overlay, audit); `ext_browser*` tools; timeline in side panel | E2E suite for 5.4 fixtures and all 6 safety cases green on real Edge; latency budgets met; live run on the dev machine |
| **P2 Gateway + web sessions** | OpenAI-compatible gateway; providers `browser-local`/`web`; adapter engine + Grok/Gemini/ChatGPT/Claude/Perplexity adapters with self-tests and mock sites; egress policy | gateway conformance tests (streaming/cancel/errors); adapter suite against mocks; tool-emulation repair test; opt-in live check |
| **P3 In-browser models** | offscreen engine; runtimes (WebLLM, transformers.js, wllama); HF search/catalog/download/verify/OPFS; fit scoring; model manager UI; embeddings/STT/TTS | download resume/verify tests; load/generate/cancel/unload on real WebGPU and WASM; real HF model on dev machine |
| **P4 Polish + reach** | CDP driver, uploads/downloads, drag&drop, recording/replay -> routines, omnibox/context menu, Firefox build, i18n scaffolding, native-host installer + one-click install, dashboard/TUI/CLI/MCP surfaces | Firefox reduced suite green; installer registers hosts on Edge/Chrome; routine replay test |
| **P5 Release** | store packages, privacy policy check, SBOM, security review checklist, docs | store validation passes; threat-model checklist signed off; upgrade/downgrade compatibility tests |
