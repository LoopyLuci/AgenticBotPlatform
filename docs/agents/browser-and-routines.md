# Browser, stored logins, routines and approvals

## The browser (`native_agent.browser`, off by default)

Needs `pip install playwright` (or `pip install -r requirements-browser.txt`) and a browser: Playwright's own
(`playwright install chromium`), or Microsoft Edge / Google Chrome already installed (found automatically).

Three tools:

* `browser` - look: `open`, `snapshot` (numbered interactive elements plus the visible text), `text`, `scroll`,
  `back`, `screenshot` (saved under `.abp/screenshots/`), `close`. It cannot change anything on a site.
* `browser_act` - `click`, `type`, `select`, `press`, `fill_credential`. Not read-only, so it is approved like any
  other change.
* `browser_handoff` - stop and ask a person to do something (a CAPTCHA, a code sent to their phone, a payment).
  **Always** put to a person, whatever the mode (`bypass` included) or rules say; when they confirm, the agent gets a
  fresh snapshot. With `headless: false` they can do it in the browser window and then approve.

What keeps it safe:

* **Pages are untrusted data.** Looking at a page on a site not in `trusted_sites` taints the session (the same
  mechanism as `web_fetch`): from then on a person must approve anything that changes something.
* **Every request the browser makes** - the page, redirects, images, scripts, frames - is checked against the same
  public-internet-only rules as `web_fetch`. A page cannot make it reach `localhost`, the local network or a cloud
  metadata address. `file:` and other schemes are refused; downloads are refused. `allow_private_hosts` opens
  specific private hosts for local development.
* **The agent never types a password, card number or one-time code.** `type` refuses those fields. Stored logins
  (below) are filled by the code, only into the site the entry belongs to; the value is never returned to the model.
* The profile is **persistent** (a login survives between runs), per profile name, under the agent state folder.
  That is what makes "log in once" work; it also means the profile holds live sessions, so treat that folder like
  a password store.

Checked against local pages with a real Microsoft Edge driven by Playwright, including a test that proves a page
cannot reach a non-allowed address (with a control that shows the same page does reach it when allowed). **Not
tested on real websites.** The numbered-elements approach is simple and will miss shadow DOM, canvas and anything
inside a frame. No screenshots are shown to the model (it reads the text snapshot).

## Stored logins: `python -m bot.vault`

```bash
python -m bot.vault add github --origin https://github.com --username me     # prompts for the password
python -m bot.vault list
```

Encrypted with Fernet in `data/vault.enc`; the key is `ABP_VAULT_KEY` if set, otherwise `data/vault.key`. **Be
clear about the limit:** with the key file beside the vault, anyone who can read the folder can read the vault. It
keeps passwords out of backups, greps and the database, not out of reach of someone who already owns the machine;
set `ABP_VAULT_KEY` from your OS keychain to keep the key elsewhere. The agent can list names, sites and usernames
(`vault_list`) and nothing else. Stored passwords and TOTP secrets are also added to the redaction list, so if a
page echoes one back it is masked, and an attempt to send one out in a request is refused. TOTP codes follow RFC 6238
(checked against the RFC's test vectors).

## Routines

Do a task with the agent, then ask it to save that as a routine. It writes the routine itself (`routine_save`): a
name, a description, a prompt **template** with `{{placeholders}}` for what varies, and what each placeholder means.
Then:

```
/routine list
/routine run pr-digest repo=owner/name days=7
/routine schedule pr-digest every 7d repo=owner/name
/routine pause pr-digest      /routine resume pr-digest      /routine history pr-digest      /routine delete pr-digest
```

A routine is only a stored prompt. Each run is an ordinary agent turn with the ordinary tools, permission rules and
approvals; a routine skips nothing. Saving one asks for approval (it is a change that will run unattended later,
and after untrusted content a person must approve it). Scheduling uses the existing scheduler; each run is recorded
in the routine's history. Values are inserted as plain text and are never expanded again.

## Approvals as objects

`GET /api/approvals` lists what the agent is waiting on - with a **diff** for edits, the command for a shell call, the
patch for a patch - and `POST /api/approvals/{id}/resolve` answers (`once`, `session`, `always`, `deny`) through the
same call the chat buttons use. A paired phone can see and answer approvals for one call; granting a standing
approval (`session`, `always`) needs the dashboard token itself. When an approval is created, paired phones get a
push notification (only the one-line summary; the details are fetched when the phone opens it), if Firebase push is
configured. Previews are built from the request alone and pass through the secret redactor.

The Android app does not show these yet: the API is what it would call.

## Not built

* **A shared cloud computer** (a container with a browser and a desktop, with live view in the apps and pause /
  take-over). It needs infrastructure this repository does not have; the browser above runs on the same machine.
* **A native computer-use tool** (screenshots and mouse coordinates for models trained for it). The transports do not
  carry images back in tool results yet, and there is no display to control.
* **Recording a workflow automatically** by watching clicks. A routine is a prompt the agent writes after doing the
  task, not a replay.
* A dashboard or desktop screen for routines, the vault or approvals (API and chat commands only).
