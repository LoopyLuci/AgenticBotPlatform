# ABP Bridge (browser extension)

Links your browser to the ABP desktop app. Design and roadmap: [`docs/browser-extension/DESIGN.md`](../docs/browser-extension/DESIGN.md).

## Status

| Phase | State |
|---|---|
| P0 bridge, pairing, auth tiers | done, tested |
| P1 agentic browser control (snapshot, actions, tab scope, safety layers, agent tools) | done, tested in real Edge |
| P2 model gateway + Grok/Gemini/ChatGPT/Claude/Perplexity web sessions | done, tested against a fake extension and jsdom; **not yet tried against the real chat sites** |
| P3 in-browser models (WebGPU/WASM, WebLLM + transformers.js) | done, tested with unit + gateway tests; **not yet tried on real WebGPU hardware or a real model download** |
| P4 uploads, downloads, Firefox build, native-host installer | done (narrower than first planned - no debugger-based driver, no recording/replay yet); native-host install script not run for real (it changes browser/registry config) |
| P5 store release | designed, not built |

## Build, test, install (development)

```bash
cd browser-extension
npm ci
npm run check         # typecheck + unit tests + build   (dist/ is the unpacked extension)
python -m pytest ../tests/test_browser_extension_e2e.py   # real Edge/Chrome + the real ABP bridge (needs: pip install playwright)
```

Load it: `chrome://extensions` (or `edge://extensions`) -> Developer mode -> **Load unpacked** -> pick `dist/`. The development build
has a fixed extension id (`manifest/dev-key.json`) so pairing and any allow-lists never change between builds.

Then in ABP open **Browser**: press *Ask ABP to approve this browser* in the extension's options page and **Allow** it in ABP, or
enter the 6-digit code ABP shows. Once connected, agents get the `ext_browser` tools.

## Models through your browser (P2)

Once paired, ABP exposes an OpenAI-compatible gateway at `/api/browser/v1/*` (see [`docs/browser-extension/DESIGN.md`](../docs/browser-extension/DESIGN.md)
section 9). `web/<site>` (grok, claude, gemini, chatgpt, perplexity) drives that site's chat page using your own logged-in session in a
background tab in a teal **"ABP web-agents"** tab group; tool calling is emulated with a small in-prompt protocol the gateway parses out
of the reply, with one automatic repair turn if the model gets the format wrong. `browser-local/<model>` is reserved for the in-browser
model engine (P3) and answers "unavailable" until that lands. Both are registered as ordinary `config/providers.yaml` providers from
ABP's Browser page ("Register web / browser-local as providers"), so agents, sub-agents, swarms and routines can use `web/grok` exactly
like any other model — except the model router's "auto" pick never chooses one, and neither is ever sent anything marked sensitive.

Each site is **off until you turn it on**, one at a time, from the extension's own options page — that toggle only ever lives in this
browser's local storage, is never set remotely, and shows the site's terms-of-service note before you confirm it.

## In-browser models (P3)

`browser-local/<model>` in the same gateway runs a model **inside this browser tab** - your prompts never leave the device.
The extension's options page lists a curated catalog (`src/models/catalog.ts`): small WebLLM chat models (Qwen2.5 1.5B,
Llama 3.2 1B, Phi-3.5 Mini, all needing WebGPU) and transformers.js models that run on CPU/WASM alone (bge-small
embeddings, Whisper tiny/base speech-to-text, a sentiment classifier), each shown with a fit score for *this* device
(good / tight / unsupported) before you download anything. The first use of a model downloads and caches its own weights
(WebLLM and transformers.js each do this themselves, verified and cached via the browser's own Cache Storage API - ABP
does not re-implement that); after that it loads from the local cache. `/api/browser/v1/embeddings` and
`browser-local/...` chat completions (streaming and not) work like any other model in the gateway once a model has been
loaded at least once from the options page, or by an agent tool that triggers a load through `/api/browser/models/<id>/load`.

## Uploads, downloads and Firefox (P4)

`ext_browser_act`'s `upload {ref, path}` action attaches a file already in the agent's workspace to a file-picker
element, and `ext_browser`'s `downloads` action lists recent downloads - both off until you turn on the matching
capability under **Settings > Agents > Tools > Browser extension**, and `downloads` additionally needs the browser's
own permission (granted from the extension's own options page). `npm run build:firefox` builds a Firefox-flavoured
copy into `dist-firefox/` from the same source (`npm run lint:firefox` runs `web-ext lint` against it); in-browser
models and the side panel degrade to a clear error there instead of the Chrome-only APIs they need.
`scripts/install_native_host.py` (run from a terminal, not by ABP) registers a small helper the extension can ask to
check on or start ABP when its usual connection cannot reach it at all - see the script's own `--help` for what it
changes and how to undo it.

## Safety, in one paragraph

ABP works only in tabs it opened itself (a purple "ABP agent" tab group) or one tab you explicitly hand over. Banks, payments,
password managers, admin consoles, browser-internal pages and login pages are never acted on; reading a sensitive page needs your
per-site grant. The agent never types passwords, card numbers or one-time codes (a stored login is filled by ABP's code, only on
its own site, and never shown to the model). Every page is treated as untrusted data. A pill on the page and the toolbar popup
each have a Stop button, and the extension enforces all of this itself, in addition to ABP.

## Layout

`src/background` hub (bridge client, pairing, tab scope, policy enforcement, router) - `src/content` page snapshot, actions and
overlay - `src/shared` protocol and URL policy (checked against the same vectors as `bot/browser_policy.py`) - `tests/unit` Vitest -
`tests/fixtures/site` the fixture website the end-to-end tests drive.
