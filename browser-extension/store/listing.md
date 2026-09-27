# Store listing metadata

Reference copy for whoever submits this to the Chrome Web Store, Edge Add-ons and/or addons.mozilla.org. Nothing here
is submitted automatically — see `docs/browser-extension/DESIGN.md` section 15.2 for the manual steps (a store
account and its own review process are outside anything this repo can do for you).

## Title

ABP Bridge

## Summary (≤132 characters, Chrome Web Store's limit)

Links your browser to your own ABP desktop app: agentic browsing, chat-site models, and local in-browser AI.

## Category

Productivity (Chrome Web Store) / Developer Tools (secondary, if the store allows more than one)

## Single purpose (Chrome Web Store requires one sentence)

Lets a person's own locally-running ABP agent read and act on their browser, with their explicit, per-tab
permission — nothing else.

## Description

ABP Bridge pairs this browser with the ABP desktop app running on your own computer (never a remote server) and lets
its agent:

- read and act on tabs it opened itself, or one tab you hand it, with a visible pill and a Stop button always available;
- never touch banks, payment pages, password managers, admin consoles or login pages;
- optionally use a chat site you are already logged into (Grok, Gemini, ChatGPT, Claude.ai, Perplexity) as a model — off by
  default, one site at a time, with that site's terms shown before you turn it on;
- optionally run a small AI model entirely inside the browser (WebGPU/WASM) — your prompts never leave the device.

Requires the free, open-source ABP desktop app (github.com/LoopyLuci/AgenticBotPlatform) already running on the same
computer. See this extension's own options page for pairing instructions.

## Permission justifications (what the Chrome Web Store review form asks for)

| Permission | Why |
|---|---|
| `storage` | Remember pairing, policy and per-site toggles on this device. |
| `tabs`, `tabGroups` | List/open/close/group the tabs ABP is allowed to use, and tell them apart from the rest of your browsing. |
| `scripting` | Inject the page-reading/acting helper only into a tab ABP is currently allowed to use, only when asked. |
| `activeTab` | Let the toolbar popup's "use this tab" button work without a broader standing grant. |
| `alarms` | A periodic keep-alive so the background service worker reconnects after Chrome puts it to sleep. |
| `sidePanel` | The optional chat/timeline side panel. |
| `notifications` | A visible OS notification when the agent needs you to look, click something, or intervene. |
| `webNavigation` | Read (not modify) same-tab-group iframe URLs, needed for the page snapshot to work across frames. |
| `offscreen` | Hosts the in-browser model engine (WebGPU/WASM), which needs a document context a service worker cannot provide. |
| `host_permissions` (`127.0.0.1`, `localhost`) | The only addresses the extension ever calls directly — your own ABP app. |
| `debugger` *(optional, requested only if a future release needs it)* | Not requested by the current build; kept reserved, unused. |
| `downloads` *(optional)* | Only requested if you turn on the "let the agent see downloads" setting in ABP; lists filenames/state, never contents. |
| `nativeMessaging` *(optional)* | Only requested if you want the native-messaging fallback (checks on/starts ABP when the usual connection fails entirely). |
| `optional_host_permissions` (`https://*/*`, `http://*/*`) | Requested per-site, only when you open a tab for the agent or turn on a specific chat-site model — never a blanket grant. |

## Privacy policy

`PRIVACY.md` in this same folder (most stores accept a file link from the repo, or ask for it to be hosted — either
way, its content is the actual policy text).

## Screenshots / promotional images

Not produced by this repo — take these from a real paired session before submitting.
