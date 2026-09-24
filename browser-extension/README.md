# ABP Bridge (browser extension)

Links your browser to the ABP desktop app. Design and roadmap: [`docs/browser-extension/DESIGN.md`](../docs/browser-extension/DESIGN.md).

## Status

| Phase | State |
|---|---|
| P0 bridge, pairing, auth tiers | done, tested |
| P1 agentic browser control (snapshot, actions, tab scope, safety layers, agent tools) | done, tested in real Edge |
| P2 model gateway + Grok/Gemini/ChatGPT web sessions | designed, not built |
| P3 in-browser models (WebGPU/WASM from Hugging Face) | designed, not built |
| P4 CDP driver, uploads/downloads, recording, Firefox, native-host installer | designed, not built |
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
