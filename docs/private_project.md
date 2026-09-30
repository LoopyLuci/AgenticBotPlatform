# ABP and the PrivateProject estate

The PrivateProject estate (the private GitHub org **PrivateProject-Security**) is a friend's personal infrastructure: about 40
services on a NixOS server, behind one sign-in (private_project-auth), with **private_project-router** as the coding and model hub.
ABP connects to all of it, and the Router connects back to ABP. Everything is on the **PrivateProject** page (dashboard and
desktop app), in the agent's tools through modules, and over MCP.

## The PrivateProject page

| Tab | What it does |
|---|---|
| **Estate** | Every service, where it answers (the real subdomain: plan, shop, write and chat are not named after their repos), and whether it is up, from each service's own `/api/build`. |
| **Router** | private_project-router's status, its own UI in a pane, chat through its routing, usage, conversations and missions. |
| **Integration** | Keys that let other programs drive ABP with only the access they need (the Router's preset first), and which sites may frame ABP or be framed by it. |
| **Sign-in** | Sign in to the estate: username, password and authenticator or recovery code, sent once to private_project-auth. ABP keeps only the session. |

## private_project-router, both ways

**ABP → Router.** Give ABP the Router's address and owner token on the Router tab (the token is kept in ABP's
`.env` as `PRIVATE_PROJECT_ROUTER_TOKEN` and never shown again). Then:

- the Router's own UI opens in the Router pane (ABP adds the Router to its `frame-src`);
- chat, route previews, runs (start, confirm, cancel), conversations, missions, models, usage and workspaces are
  available through `/api/private_project/router/...`;
- the Router's metered OpenAI endpoint is the provider **private_project-router**, so `private_project-router/auto`,
  `private_project-router/local` and every alias work wherever ABP takes a model.

**Router → ABP.** The Router's Bot Platform view (`server/botplatform.js`) drives ABP's bots and Docker manager.
Give it an **integration key**, not the dashboard token (Integration tab → *Make a Router key*; paste it in the
Router under Settings → Bot Platform, or as `ABP_DASHBOARD_TOKEN` in its `.env`):

- the key reaches only `status:read`, `bots:read`, `bots:control`, `docker:read`, `docker:control`,
  `modules:read`: exactly the calls the Router makes, and nothing else (no config, providers, secrets, bot
  creation, destructive Docker verbs);
- bot rows reach it with credentials masked;
- making the key with the Router's address also lets the Router show ABP in a frame (`frame-ancestors`).

`tests/test_integrations.py` replays the Router's exact calls with such a key, so an ABP change that would break
the Router's view fails ABP's own pipeline. Tested live on 2026-09-29 against the Router's latest upstream: both
directions, chat through the Router's local route, and its Bot Platform view listing this machine and Server.

## Connectors: every service as operations

Each web-facing service has a connector, a separate module in its own private repo (`LoopyLuci/abp-private_project-<service>`).
All of them run one shared runtime, `LoopyLuci/abp-private_project-connector` (Python standard library only). A connector
holds none of the service's code: its `spec.toml` lists the service's routes, generated from its source and pinned
to the commit they came from. Each route is an operation (1,053 across 29 connectors on 2026-09-29), plus:

- `service.status`: target, health, session, counts;
- `auth.status`, `auth.set_token`, `auth.clear`;
- `api.request`: any request as the signed-in user.

Calls go out as you. When you sign in (or a connector starts), ABP hands every running connector the session.
The Router's connector gets the Router's owner token instead.

Install one from the Modules page (area **private_project**). ABP finds checkouts named after their repo next to ABP, or in
any folder listed in `modules.search_paths`. Internal services (ops, claude, ai, neith, trainer, dash) listen on the
server's loopback or private network. Set their address with `OCTOCONN_BASE_URL` (for example a Tailscale address).

Services with no web API are covered differently:

| Service | How it is covered |
|---|---|
| nixos-hetzner, pentest-flake | ABP's NixOS flake and module (`nix/`); Fabric's deploy work |
| private_project-auth-client | `bot/private_project/sso.py` does the same sign-in, verify and refresh |
| alfred-js (Alfred, the Discord bot) | A runner, `LoopyLuci/abp-private_project-alfred-js`: ABP clones and installs the upstream bot, runs it as a managed service with its own tokens (stored with `service.set_secret`, never in the repo), lists and registers its slash commands, runs its tests, updates it and reads its chip balances. `RUNNERS` in `bot/private_project/connectors.py` |
| private_project-vault, Cephaloscan, PentestPlayground, private_project-simplex, private_project-mail, private_project-conversation-exporter | Listed on the Estate tab with their repos. Connectors to come (docs/modules/ROADMAP.md §5.7). |

## Configuration (config/backends.yaml)

```yaml
private_project:
  domain: private_projecttechnology.net          # the estate's base domain
  router_url: http://127.0.0.1:3030      # where private_project-router runs
  services:                              # per service: url, subdomain, health, disabled
    private_project-ops: { url: "http://100.x.y.z:3021" }
integrations:
  frame_ancestors: ["http://127.0.0.1:3030"]   # sites that may show ABP in a frame
  frame_src: ["http://127.0.0.1:3030"]         # sites ABP's panes may show
modules:
  search_paths: ["/path/redacted"]
```
