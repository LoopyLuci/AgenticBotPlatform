# Peers and TransferDaemon overlay

Linked ABP servers (Peers) find each other through TransferDaemon's overlay, with no firewall changes.

- Pairing token carries an overlay invite (`overlay.invite_create`); linking accepts it (`overlay.invite_accept`).
- Both sides publish their dashboard to paired nodes only (`overlay.expose {service: "abp", allow: ["*paired"]}`).
- Peers are reached via `overlay.connect` (stable local endpoint) with metrics (kind, rtt_ms, loss, mbps, state).
- Heartbeat refreshes routes automatically; direct URL remains a fallback.
- Cluster placement uses TD route metrics (`_distance_ms`) for "close by"; groups target service groups (`service_group`).
- Big results (>8 MB) use TD file transfer (`transfers.send_file` / `await_download`).
- `firewall-open` stays optional; pairing never asks for it.
