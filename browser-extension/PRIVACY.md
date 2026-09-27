# ABP Bridge — privacy

**Short version:** this extension has no server of its own, no analytics, and no ads. It only ever talks to two
places: the ABP desktop app on **your own computer** (`127.0.0.1`/`localhost`, never a remote address), and, for the
specific features that need it, a website you are already logged into or Hugging Face's/MLC's model-hosting servers
(only to download a model file you chose). Nothing is sold, shared with third parties for advertising, or used to
build a profile of you.

## What the extension can see and do

Only after you pair it with your own copy of ABP (a code you type, or an approval you click in the ABP app), and only
in tabs ABP opened itself or one tab you specifically hand over ("Let ABP use this tab"). It never runs on a browser
page you have not put it in front of. Full detail: this repo's `README.md` and `docs/browser-extension/DESIGN.md`.

## Where data goes, by feature

- **Reading and acting on a page** (`ext_browser`/`ext_browser_act`): page content, screenshots and click/type
  requests travel between the extension and the ABP app running **on your own computer** over a loopback connection
  that never leaves the machine. What ABP's agent then does with that content — for example, sending a summary of it
  to a cloud model provider you configured — is controlled by your own ABP settings, not by this extension.
- **A stored login** (`fill_credential`): the password/username value is generated inside ABP and passed, once, from
  the extension's background script to the exact page it belongs to. It is never written to this extension's own
  audit log, never echoed back to ABP or to any model, and never sent to any site other than the one the login was
  saved for.
- **A web-session model** (`web/grok`, `web/gemini`, etc.): off until you turn it on, per site, and shown that site's
  own terms before you do. Once on, your prompt is typed into that site's own chat page using your existing logged-in
  session — exactly as if you had typed it yourself — so it is subject to that site's own privacy policy, not this
  extension's.
- **An in-browser model** (`browser-local/...`): runs entirely inside your browser tab. Nothing you type is sent
  anywhere. The first time you use a given model, its weight files are downloaded once from Hugging Face's or MLC's
  own servers and cached by the browser; after that, nothing is downloaded again unless you remove it.
- **Uploads and downloads**: an upload reads a file the *agent* already had in its own workspace and attaches it to
  a page element on your computer; the extension does not read arbitrary files from your disk on its own. A download
  listing shows filenames, sizes and state to ABP — never file contents — and only once you have granted both ABP's
  own "downloads" setting and the browser's own downloads permission.
- **The native-messaging helper** (`bot/native_host.py`, installed separately and only if you choose to): answers
  exactly two questions — "is ABP reachable?" and "please start it" — and carries no other data.

## What is stored, and where

- Pairing key, policy and per-site toggles: this browser's own local extension storage (`chrome.storage`), never
  synced to a Google/Microsoft/Mozilla account.
  A local audit log of *what happened* (e.g. "clicked a button on shop.example.com"), never *what was typed* into a
  secret field, capped and periodically forwarded to your own ABP app for its own history.
- Nothing is stored on any server operated by the people who built ABP; there is no such server for this extension to
  talk to.

## Your controls

Uninstalling the extension, or pressing **Disconnect** in its options page, removes everything it stored and stops
all of the above immediately. The **Stop** button (in the page overlay, the toolbar popup, or `Ctrl+Shift+.`) halts
any in-progress agent action at once.

## Contact

This is a self-hosted, personal tool: there is no separate company or support address collecting data about your use
of it beyond your own ABP install. See the main project's `README.md` for where to report a problem.
