# Power and remote control of linked servers

Two machines running ABP can be linked (the **Peers** page: one makes a pairing token, the other pastes it). A link
always lets each see the other's overview and start or stop its bots. This page covers the rest: letting one ABP
control another's VM-Harness, Hermes Manager and power, keeping machines awake while they are used, and waking them.

## Letting a linked server control this one

Each machine decides for itself, on its own **Power** page ("What linked servers may control here") or in its
`config/backends.yaml`:

```yaml
peers:
  remote_control: [vm-harness, hermes-manager, transferdaemon, power]
```

| Area | What a linked server may then do here |
|---|---|
| `vm-harness` | everything under `/api/vm-harness/`: VMs, containers, the hub, the window, all operations |
| `hermes-manager` | everything under `/api/hermes-manager/` |
| `transferdaemon` | everything under `/api/transferdaemon/`: messages, files, contacts, its window, terminal UI and relays |
| `power` | keep this machine awake, change its power settings, send wake packets from it |

It is off by default. Only the machine's own dashboard can change it (`PUT /api/peers/control`); a linked server's
key never can. A request from a linked server outside the allowed areas gets a 403 that says so.

## Controlling a linked server

- **Pages.** The VM-Harness, Hermes Manager, TransferDaemon and Power pages have a **Machine** selector: *This machine* or any linked
  server. With a server chosen, the page works the same, through `POST /api/peers/{name}/proxy`.
- **Agent tools.** Every `vmh_*`, `hm_*`, `td_*`, `power_status` and `power_keep_awake` tool takes `machine`: a linked
  server's name. For example, `vmh_call {machine: "Server", operation: "vm.start", args: {name: "omarchy", backend: "qemu"}}`.
- **API.** `POST /api/peers/{id or name}/proxy` with `{method, path, body}` (dashboard token only). Only the three
  areas' paths can be reached; calls that change something are recorded in the audit log.

## Keeping a machine awake

One background thread asks the operating system not to sleep while something needs the machine
(Windows `SetThreadExecutionState`, macOS `caffeinate`, Linux `systemd-inhibit`).

```yaml
power:
  keep_awake: while_busy   # off | always | while_busy
  keep_display_on: false
  idle_minutes: 10         # while_busy: stay awake this long after the last use
  auto_wake: true          # wake a linked server before calling it, if it does not answer
```

With `while_busy`, these keep it awake:

- an agent turn;
- a running job;
- a linked server using it (any allowed control call);

and it stays awake for `idle_minutes` after the last of them. Anyone can also add a **hold** with a reason and a
duration: the Power page's *Keep awake*, the agent's `power_keep_awake`, or another ABP (`machine`). The page lists
every reason and who asked.

## Waking a machine

Wake-on-LAN sends a "magic packet" to a machine's network card.

- **Learn** a linked server's card once. Use the Power page's *Learn* button, or `power_wake {learn: "Server"}`.
  ABP asks that server's ABP for its cards, prefers a wired physical one, and saves it under `power.wake`.
- **Wake** it:
  - with the page's *Wake* button;
  - with `power_wake {target: "Server"}`;
  - or with `POST /api/power/wake`.
- **Auto-wake.** With `auto_wake` on, any call to a linked server that cannot connect wakes the server and waits up to
  two minutes for it to answer before trying again.

What the target needs:

- **Its network card allows waking.** On Windows, open Device Manager, then the card's Power Management and Advanced
  tabs, and enable *Wake on Magic Packet*. The Power page shows each card's setting, and the same for a linked
  server's cards.
- **Wake-on-LAN is on in its firmware.** This matters most when waking from a full shutdown.
- **The packet goes out on the target's own network.** Broadcasts do not cross routers or VPNs. For a machine on
  another subnet, send it from a linked server on that network: the *from* selector on the page, or `via` in
  `power_wake`.

## Routes

| Route | |
|---|---|
| `GET /api/power/status` | mode, holds, reasons, learned machines |
| `PUT /api/power/settings` | `{keep_awake, keep_display_on, idle_minutes, auto_wake}` |
| `POST /api/power/hold` / `release` | `{reason, minutes, key?}` / `{key?}` |
| `GET /api/power/info` | this machine's network cards (MAC, address, broadcast, whether wake is on) |
| `POST /api/power/wake` | `{target}` or `{mac, broadcast}`, optional `via` |
| `POST /api/power/learn/{peer}` | learn a linked server's card |
| `GET` / `PUT /api/peers/control` | what linked servers may control here |
| `POST /api/peers/{peer}/proxy` | call a linked server's module API |
