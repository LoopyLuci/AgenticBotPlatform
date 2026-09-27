"""SQLite storage layer — small, fast, and local.

WAL mode gives concurrent readers (the dashboard) a consistent view while
the bot keeps writing job/telemetry rows, without needing a separate
database server. The event-loop thread owns the primary connection; any
other thread (FastAPI's threadpool, asyncio.to_thread) transparently gets
its own connection to the same file, so a slow query or a lock wait off
the loop never stalls it and threads never share one connection's
transaction state. In-process writes still serialize behind one lock —
see docs/adr/0003-single-sqlite-connection.md.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger(__name__)

# Reuses envfile's canonical-root resolution rather than Path(__file__)
# directly — a release build's bundled bot/ lives under a different folder
# than the source tree, and without pinning to one fixed root, a release
# run and a `cargo tauri dev` run would each quietly keep their own
# separate database (this bit .env the same way before it was fixed).
DB_PATH = PROJECT_ROOT / "data" / "bot.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_owner: Optional[int] = None
_generation = 0
_thread_local = threading.local()
_thread_conns: "set[sqlite3.Connection]" = set()
_thread_conns_lock = threading.Lock()

# Lets a caller (the dashboard's WebSocket broadcaster — see
# bot/dashboard/server.py) learn that a new chat message or job status
# change was just written, without db.py importing anything from the
# dashboard layer itself — same inverted-dependency shape as
# ConfigManager.on_reload() elsewhere in this codebase. Each callback gets
# just the row's id, not a payload shape db.py would have to own; the
# listener re-reads whatever fields it actually needs via get_message()/
# get_job(). A callback's own failure is logged and never allowed to break
# the write that triggered it — logging/broadcasting a message must never
# be why sending or receiving one fails.
_message_listeners: list[Callable[[int], None]] = []
_job_listeners: list[Callable[[int], None]] = []
_job_tool_event_listeners: list[Callable[[int], None]] = []
_job_children_listeners: list[Callable[[int], None]] = []
_kanban_card_created_listeners: list[Callable[[int], None]] = []


def on_message_logged(callback: Callable[[int], None]) -> None:
    _message_listeners.append(callback)


def on_job_changed(callback: Callable[[int], None]) -> None:
    _job_listeners.append(callback)


def on_job_tool_event(callback: Callable[[int], None]) -> None:
    """Fired with a job_id whenever a live tool-call event (from the
    Hermes gateway's SSE stream — see bot/swarm/observability.py) is
    recorded for that job."""
    _job_tool_event_listeners.append(callback)


def on_job_children_set(callback: Callable[[int], None]) -> None:
    """Fired with a job_id once a dispatch's post-hoc structured child
    breakdown (parsed from the final reply — see
    bot/swarm/child_parser.py) has been written."""
    _job_children_listeners.append(callback)


def on_kanban_card_created(callback: Callable[[int], None]) -> None:
    """Fired with a card_id right after a new kanban card is inserted —
    the reactive half of auto-management (bot/auto_manage.py): a manager
    instance configured with trigger including "kanban_card_created" gets
    a real check-in call for that board's owning instance."""
    _kanban_card_created_listeners.append(callback)


def _notify_message_logged(message_id: int) -> None:
    for cb in _message_listeners:
        try:
            cb(message_id)
        except Exception:
            logger.exception("on_message_logged callback failed")


def _notify_job_changed(job_id: int) -> None:
    for cb in _job_listeners:
        try:
            cb(job_id)
        except Exception:
            logger.exception("on_job_changed callback failed")


def _notify_job_tool_event(job_id: int) -> None:
    for cb in _job_tool_event_listeners:
        try:
            cb(job_id)
        except Exception:
            logger.exception("on_job_tool_event callback failed")


def _notify_job_children_set(job_id: int) -> None:
    for cb in _job_children_listeners:
        try:
            cb(job_id)
        except Exception:
            logger.exception("on_job_children_set callback failed")


def _notify_kanban_card_created(card_id: int) -> None:
    for cb in _kanban_card_created_listeners:
        try:
            cb(card_id)
        except Exception:
            logger.exception("on_kanban_card_created callback failed")


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type   TEXT NOT NULL,
    backend       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued|running|retrying|success|failed
    user_id       INTEGER,
    prompt        TEXT,
    result        TEXT,
    error         TEXT,
    tokens        INTEGER,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    duration_ms   INTEGER,
    instance_id   INTEGER,   -- bot_instances.id; NULL for pre-multi-instance rows ("legacy")
    swarm_run_id  TEXT       -- swarm_runs.swarm_run_id; NULL unless this job is one swarm member's call
);

CREATE TABLE IF NOT EXISTS connections_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    component  TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    component  TEXT NOT NULL,
    metric     TEXT NOT NULL,
    value      REAL
);

CREATE TABLE IF NOT EXISTS mcp_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    server    TEXT NOT NULL,
    event     TEXT NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS config_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    version   INTEGER NOT NULL,
    actor     TEXT NOT NULL,
    summary   TEXT
);

CREATE TABLE IF NOT EXISTS allowed_users (
    telegram_id  INTEGER PRIMARY KEY,
    name         TEXT,
    added_at     TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    platform     TEXT NOT NULL DEFAULT 'telegram',  -- 'telegram' | 'discord' | 'slack' | ...
    chat_id      TEXT NOT NULL,   -- platform-native id: Telegram/Discord numeric, Slack channel string
    user_id      TEXT,
    username     TEXT,
    direction    TEXT NOT NULL,   -- 'in' (from the platform) | 'out' (bot/dashboard -> platform)
    source       TEXT NOT NULL,   -- '<platform>' | 'bot' | 'dashboard'
    text         TEXT NOT NULL,
    instance_id  INTEGER,  -- bot_instances.id; NULL for pre-multi-instance rows ("legacy")
    attachment_path  TEXT,  -- relative filename under data/attachments/, NULL if no attachment
    attachment_name  TEXT,  -- original filename as supplied by the platform/user, display only
    attachment_mime  TEXT   -- best-effort mime type, NULL if unknown
);

-- One row per independently-configured bot ("claude-support-telegram",
-- "hermes-sales-discord", etc). Credentials/allowlist/action_overrides are
-- JSON blobs rather than fixed columns since their shape varies by
-- platform and grows without a schema migration every time — mirrors how
-- messages.text already stays schema-free per platform. Not FK-enforced
-- against jobs/messages (this project doesn't enforce FKs on any of its
-- append-only log tables either), so deleting an instance never risks a
-- cascade against its own history.
CREATE TABLE IF NOT EXISTS bot_instances (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    platform           TEXT NOT NULL,                -- telegram | discord | slack
    backend            TEXT NOT NULL DEFAULT 'cli',   -- this instance's own default backend
    enabled            INTEGER NOT NULL DEFAULT 1,
    credentials        TEXT NOT NULL,                 -- JSON, shape depends on platform
    allowed_user_ids   TEXT NOT NULL DEFAULT '[]',    -- JSON array
    admin_user_ids     TEXT NOT NULL DEFAULT '[]',    -- JSON array, subset of allowed_user_ids with the admin slash-command tier (bot/slash_access.py) — empty means the tier system is off entirely for this instance
    action_overrides   TEXT NOT NULL DEFAULT '{}',    -- JSON, same shape as backends.yaml's action_overrides
    can_target         TEXT NOT NULL DEFAULT '[]',    -- JSON array of bot_instances.id this instance may command (agent_control) — also doubles as "manages" for persona='manager'
    model              TEXT,                          -- optional per-instance model override, passed through to this instance's backend
    desktop_session_key TEXT,                         -- links to one specific chat/session in the ui/hermes_gateway backend, NULL if none created yet
    custom_instructions TEXT,                          -- optional persona/instructions prepended to every prompt this instance routes through router.ask()
    persona            TEXT NOT NULL DEFAULT 'assistant', -- one of bot/personas.py's PERSONA_PRESETS keys; purely metadata + a custom_instructions seed
    hermes_home        TEXT,                          -- optional per-instance HERMES_HOME override (hermes_gateway only) — see bot/backends/hermes_gateway_backend.py's isolation docstring; NULL means "share the machine-wide default Hermes home", today's historical behavior
    desktop_project    TEXT,                          -- optional per-instance Claude Desktop project name (ui backend only) — pins this instance's new sessions to one project's "New session in <name>" button instead of the bare "New" one; NULL means outside any project, today's historical behavior
    desktop_workspace_dir TEXT,                       -- optional per-instance absolute folder path (ui backend only) — new sessions open scoped to exactly this folder via Desktop's own Ctrl+N "Open folder..." replace flow, guaranteeing isolation from any existing project; takes priority over desktop_project when both are set
    desktop_effort     TEXT NOT NULL DEFAULT 'low',    -- ui backend only: Desktop's own "Effort" toolbar slider (low/medium/high/extra/max/ultracode) is forced to this level before every send — see bot/backends/ui_backend.py's EFFORT_LEVELS
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_started_at    TEXT,
    last_error         TEXT
);

-- Telegram (or other chat-platform) pairing codes for an unrecognized DM
-- sender — see bot/pairing.py. A code is single-use: consumed by exactly
-- one approve or expiry/lockout outcome, never reused. Approval appends
-- the user id into the owning instance's allowed_user_ids; nothing here
-- enforces auth itself, bot/handlers.py's require_auth still owns that.
CREATE TABLE IF NOT EXISTS pairing_codes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  INTEGER NOT NULL,   -- bot_instances.id this code was requested against
    code         TEXT NOT NULL UNIQUE,
    user_id      TEXT NOT NULL,      -- platform-native user id, string either way (Telegram numeric, Slack U…)
    user_name    TEXT,               -- best-effort display name/username at request time, for the approver's benefit
    chat_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,   -- failed approval attempts against this code (wrong code typed elsewhere) — see LOCKOUT constants in bot/pairing.py
    approved_at  TEXT,
    denied_at    TEXT
);

-- Another AgenticBotPlatform installation this one is linked to (see bot/peers.py)
-- — e.g. a home PC and a laptop each running their own independent bot
-- fleet, linked so either admin can see and manage the other's bots from
-- their own dashboard, without merging databases or sharing one Telegram
-- bot. `outbound_api_key` is the plaintext credential THIS server presents
-- when calling THAT one (kept plaintext, same trust boundary as .env's
-- DASHBOARD_TOKEN — needed on every outbound call, unlike api_keys.key_hash
-- which only ever needs comparing). `inbound_api_key_id` is the api_keys
-- row (kind='peer_server') THIS server minted for THAT one to call back —
-- unlinking revokes it so the peer's old credential stops working
-- immediately. `base_url` is only known when the admin typed it in (either
-- as the link target, or the peer voluntarily shared it during handshake)
-- — a peer that never shared a reachable address can still call us, just
-- not be called back.
CREATE TABLE IF NOT EXISTS peer_servers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    base_url            TEXT NOT NULL DEFAULT '',
    outbound_api_key    TEXT NOT NULL,
    inbound_api_key_id  INTEGER NOT NULL,
    linked_at           TEXT NOT NULL,
    last_seen_at        TEXT,
    last_error          TEXT
);

-- Short-lived, single-use tokens minted specifically to authenticate the
-- /api/peers/handshake bootstrap call — see bot/peers.py. This is the
-- credential that actually crosses the network when linking two servers;
-- the real DASHBOARD_TOKEN never does. Only one is ever valid at a time
-- (generating a new one invalidates whatever was pending), so there's
-- nothing stale left lying around to worry about revoking later.
CREATE TABLE IF NOT EXISTS server_pairing_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

-- A named group of bot instances plus a chosen collaboration strategy.
-- `config` is JSON, strategy-specific (member instance ids, roles, or for
-- strategy='custom' a full step graph) — see bot/swarm/strategies.py.
CREATE TABLE IF NOT EXISTS swarms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    strategy     TEXT NOT NULL,   -- fanout_synthesize | leader_vote | sequential_relay | decompose_delegate | custom
    config       TEXT NOT NULL,   -- JSON, strategy-specific
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- One row per triggered run. `steps` is a live-updated JSON array (each
-- member's status/result as it completes) so the dashboard can poll for
-- genuine in-progress state, not just a final result. Individual member
-- calls still show up as ordinary rows in `jobs`, tagged via
-- jobs.swarm_run_id = swarms_runs.swarm_run_id, so the Jobs tab needs no
-- separate swarm-aware code path.
CREATE TABLE IF NOT EXISTS swarm_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    swarm_id       INTEGER NOT NULL,
    swarm_run_id   TEXT NOT NULL UNIQUE,  -- uuid4 hex, correlates member jobs
    status         TEXT NOT NULL DEFAULT 'running',  -- running|success|failed|cancelled
    prompt         TEXT NOT NULL,
    result         TEXT,
    error          TEXT,
    steps          TEXT,               -- JSON, live/final per-step progress
    requested_by   TEXT,
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    duration_ms    INTEGER
);

-- A browsable grouping of jobs/messages for the same (instance_id, chat_id)
-- pair that happened close together in time — see _get_or_create_session().
-- Populated at write time, not computed on read, so listing/filtering stays
-- index-backed as history grows (mirrors how jobs/swarm_runs are handled).
CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id       INTEGER,
    chat_id           TEXT,
    title             TEXT NOT NULL DEFAULT '',
    started_at        TEXT NOT NULL,
    last_activity_at  TEXT NOT NULL,
    item_count        INTEGER NOT NULL DEFAULT 0
);

-- A per-chat link to one real, resumable backend conversation (Claude
-- Desktop's "ui" backend or Hermes's "hermes_gateway" — the two backends
-- with their own create_session()). Distinct from `sessions` above:
-- `sessions` is auto-bucketed message-activity history for the dashboard,
-- this table is the actual routing pointer Router.ask() reads to know
-- which backend conversation a given chat's next message continues.
-- Exactly one row per (instance_id, chat_id) has archived_at NULL at a
-- time — that's "current"; every /new or /resume archives whichever row
-- was current and inserts a fresh one, so full history stays browsable
-- via list_chat_sessions() instead of being overwritten in place.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id          INTEGER NOT NULL,
    chat_id              TEXT NOT NULL,
    thread_id            TEXT,    -- Telegram forum-topic message_thread_id; NULL = the chat's root/non-topic session — see /topic
    desktop_session_key  TEXT NOT NULL,
    title                TEXT,
    created_at           TEXT NOT NULL,
    last_used_at         TEXT NOT NULL,
    archived_at          TEXT
);

-- Real multi-turn conversation history for the agent-loop engine
-- (bot/agent_runtime/) — keyed by chat_sessions.desktop_session_key (works
-- transparently for the "api" backend's synthetic uuid4 session keys, see
-- ApiBackend.create_session()). `content` is JSON: a plain string for a
-- user prompt, or a list of Anthropic content blocks (text/tool_use/
-- tool_result) for assistant turns and tool results — the shape the
-- Anthropic Messages API itself uses, stored as-is so replaying history
-- back into the next API call needs no reshaping.
CREATE TABLE IF NOT EXISTS agent_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key  TEXT NOT NULL,
    role         TEXT NOT NULL,   -- user | assistant
    content      TEXT NOT NULL,   -- JSON
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_key, id);

-- One row per dangerous tool call awaiting a human's approve/deny — see
-- bot/agent_runtime/approval.py. `status` starts 'pending' and ends in
-- exactly one of approved_once/approved_session/approved_always/denied/
-- expired; the in-memory asyncio.Event that actually wakes the waiting
-- turn lives in approval.py's process-local registry (this row is the
-- durable/audit side, and what a Telegram button edit reads back).
CREATE TABLE IF NOT EXISTS pending_approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  INTEGER NOT NULL,
    chat_id      TEXT NOT NULL,
    session_key  TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    tool_input   TEXT NOT NULL,   -- JSON
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    resolved_by  TEXT
);

-- Standing "session" or "always" approvals granted via the ea:session /
-- ea:always outcomes above, so the same tool doesn't re-prompt every call.
-- session_key NULL means instance-wide ("always"); non-NULL scopes it to
-- one linked conversation ("session" — cleared the moment that session is
-- superseded by a fresh /new, since a new session_key won't match).
CREATE TABLE IF NOT EXISTS tool_approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  INTEGER NOT NULL,
    session_key  TEXT,
    tool_name    TEXT NOT NULL,
    granted_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_approvals_lookup ON tool_approvals(instance_id, tool_name, session_key);

-- Mobile/device API keys — a growing multi-device credential set, unlike
-- the single legacy DASHBOARD_TOKEN env value. Only a hash is ever stored;
-- the plaintext is returned once, at creation, and never again.
CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,
    key_hash        TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    revoked_at      TEXT,
    -- Per-device admin capability for the NEW conversational admin surface
    -- (Server Chat/Support Bot admin actions, device/pairing management,
    -- destructive local ops, unrestricted shell) — see bot/device_tiers.py.
    -- Deliberately does NOT affect the pre-existing dashboard REST API's
    -- own flat "any paired device = desktop parity" model; that's a
    -- separate, already-deliberate decision this column doesn't touch.
    permission_tier TEXT NOT NULL DEFAULT 'none'
);

-- One row per device's current FCM registration token, tied to the mobile
-- api_keys row that registered it — revoking the key (api_keys.revoked_at)
-- should stop paging that device too, so revoke_api_key() deletes the
-- matching rows here in the same call.
CREATE TABLE IF NOT EXISTS push_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id    INTEGER NOT NULL,
    fcm_token     TEXT NOT NULL UNIQUE,
    updated_at    TEXT NOT NULL
);

-- Live presence for a paired device — one row per api_keys id, upserted on
-- every authenticated request (see verify_api_key()) rather than via a
-- separate heartbeat endpoint, since every real client call already proves
-- the device is alive. "Online" is computed at read time (now - last_seen
-- < window), not stored, so nothing needs to age it out on disconnect.
CREATE TABLE IF NOT EXISTS device_presence (
    api_key_id    INTEGER PRIMARY KEY,
    platform      TEXT,
    app_version   TEXT,
    device_model  TEXT,   -- e.g. "Pixel 8 Pro" — real hardware model, not the user-typed pairing label
    os_version    TEXT,   -- e.g. "Android 14"
    local_ip      TEXT,   -- as this server observed it — only useful when caller shares the server's LAN
    mesh_port     INTEGER,-- self-reported: which TCP port this device's own mesh listener is bound to right now, if any
    last_seen     TEXT NOT NULL
);

-- User-added training phrases for the Support Bot's TF-IDF intent
-- classifier (bot/support_bot/model.py), on top of the hand-authored
-- baseline in bot/support_bot/training_data.py's EXAMPLES. Lets someone
-- improve recognition for a phrasing the classifier missed without
-- editing code — see the Training tab.
CREATE TABLE IF NOT EXISTS support_bot_phrases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase     TEXT NOT NULL,
    intent     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    module_id  TEXT
);

-- Self-monitoring log for the Support Bot's hybrid classifier
-- (bot/support_bot/hybrid.py) — every single classification, both
-- sub-models' independent verdicts, and which one the hybrid trusted.
-- This is what the Training tab's model-health panel is computed from —
-- real logged behavior, not a guess.
CREATE TABLE IF NOT EXISTS support_bot_classifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    text              TEXT NOT NULL,
    tfidf_intent      TEXT NOT NULL,
    tfidf_confidence  REAL NOT NULL,
    nn_intent         TEXT NOT NULL,
    nn_confidence     REAL NOT NULL,
    final_intent      TEXT NOT NULL,
    final_confidence  REAL NOT NULL,
    source            TEXT NOT NULL,   -- 'ensemble' | 'tfidf' | 'nn' | 'unknown'
    agreed            INTEGER NOT NULL,
    reviewed          INTEGER NOT NULL DEFAULT 0
);

-- Synthetic training examples generated by the free-model swarm (see
-- bot/support_bot/synthetic_gen.py) — never inserted directly into the
-- live training set (support_bot_phrases), since an LLM-generated label
-- can be wrong. Mirrors memory_entries' pending/approved/rejected shape.
CREATE TABLE IF NOT EXISTS support_bot_pending_examples (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase           TEXT NOT NULL,
    intent           TEXT NOT NULL,
    source_provider  TEXT NOT NULL,
    source_model     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    source_kind      TEXT,        -- NULL/'synthetic' (default, synthetic_gen.py's batches) | 'llm_fallback_live' (a real production miss Tier 2 just labeled, see bot/support_bot/llm_fallback.py) | 'auto:2-model-agreement'
    approved_by      TEXT,        -- NULL (human, via the dashboard Approve button) | 'auto:2-model-agreement' (see synthetic_gen.py's auto-approve rule)
    resulting_phrase_id INTEGER   -- the support_bot_phrases.id this approval created, if status='approved' — lets revert_support_bot_pending_example() delete the exact right live phrase, never a fragile text match
);

-- Server Chat — a permanent, bot-independent messaging/file-transfer
-- channel between the devices themselves (the desktop app and every
-- paired Android phone), separate from the platform-facing `messages`
-- table above. Device identity here is a plain integer: 0 always means
-- "the desktop app" (a fixed sentinel — never a real api_keys.id, since
-- those start at 1), any positive integer is an api_keys.id. One 'group'
-- row always exists (the single permanent "Server Chat" room every
-- device sees); one 'direct' row exists per unordered device pair,
-- auto-created for a new device against every device that already
-- existed at pairing time — see create_conversations_for_new_device().
CREATE TABLE IF NOT EXISTS server_chat_conversations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,   -- 'group' | 'direct'
    participant_a  INTEGER,         -- NULL for 'group'; the lower device id for 'direct'
    participant_b  INTEGER,         -- NULL for 'group'; the higher device id for 'direct'
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS server_chat_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   INTEGER NOT NULL,
    sender_device_id  INTEGER NOT NULL,
    ts                TEXT NOT NULL,
    text              TEXT NOT NULL DEFAULT '',
    attachment_path   TEXT,
    attachment_name   TEXT,
    attachment_mime   TEXT,
    attachment_size   INTEGER,
    thumbnail_path    TEXT,
    -- Admin control surface plan, Section 3 — 'message' (default, an
    -- ordinary chat message) or 'approval_request' (a dangerous-tool
    -- approval prompt posted by the AgenticBotPlatform admin pipeline, which the
    -- UI renders with Approve/Deny buttons instead of plain text).
    -- approval_id is only ever set for the latter, and only ever
    -- resolves through the SAME bot.agent_runtime.approval state
    -- machine the Telegram admin bot's dangerous-tool prompts use.
    kind              TEXT NOT NULL DEFAULT 'message',
    approval_id       INTEGER
);

-- A pending APK offer for one paired device — created by the desktop
-- app's "Send APK" / "Send APK to All Paired Devices" buttons. Pull-based
-- by design (there's no reliable way to push to a backgrounded phone
-- without FCM, which is optional and often unconfigured): the Android
-- app checks GET /api/android/apk/pending on its own next poll, and
-- downloaded_at is stamped the moment it actually downloads the file, so
-- the "update available" banner clears on its own without a separate ack
-- round trip. One row per (device, send) — sending again to an already-
-- pending device is fine, list_pending_apk_push() only returns the newest.
-- A recurring prompt for one chat — /cron (arbitrary interval), /loop
-- (interval + optional run cap), /heartbeat (re-enters the session when
-- idle, so interval_s is a minimum gap rather than a strict clock — see
-- bot/scheduler.py). One background task polls this table for due rows
-- and dispatches each through the same agent-loop engine /background
-- uses, so a scheduled prompt gets the same tool access, approval gating,
-- and session history as a manually-typed one.
CREATE TABLE IF NOT EXISTS scheduled_commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id   INTEGER NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT,            -- Telegram forum-topic id, NULL outside a topic — see /topic
    kind          TEXT NOT NULL,   -- cron | loop | heartbeat
    prompt        TEXT NOT NULL,
    interval_s    INTEGER NOT NULL,
    next_run_at   TEXT NOT NULL,
    last_run_at   TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    max_runs      INTEGER,         -- NULL = unlimited
    run_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    -- Failure-streak tracking (bot/scheduler.py's _fire()/_deliver()) —
    -- reset to 0 on any successful run, incremented on a failed one;
    -- once it crosses scheduler.max_consecutive_failures the row is
    -- auto-disabled and exactly one alert is sent (not a repeat every
    -- poll). last_error is the most recent failure's message, kept even
    -- after the row is disabled so a human can see why.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

-- A per-instance kanban board — see bot/kanban.py, /kanban.
CREATE TABLE IF NOT EXISTS kanban_boards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id   INTEGER NOT NULL,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(instance_id, name)
);
CREATE TABLE IF NOT EXISTS kanban_cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id      INTEGER NOT NULL,
    column_name   TEXT NOT NULL DEFAULT 'todo',
    text          TEXT NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- A long-term fact the agent loop asked to remember (via the save_memory
-- tool — bot/agent_runtime/tools.py) or a human added directly, gated
-- behind the same pending/approve/reject flow as a dangerous tool call
-- unless action_overrides.memory_approval is turned off for that
-- instance. Approved rows get folded into the api backend's system
-- prompt on every turn — see bot/memory.py's approved_summary().
CREATE TABLE IF NOT EXISTS memory_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id   INTEGER NOT NULL,
    content       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    source        TEXT NOT NULL DEFAULT 'user',      -- user | tool
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);

-- A reusable instruction/snippet the agent loop can pull in on demand via
-- the read_skill tool — see bot/skills.py. instance_id NULL means every
-- instance can see and use it; non-NULL scopes it to one bot. This is a
-- local file-backed store (/skills install <path>), not a networked
-- marketplace — see bot/skills.py's module docstring for why.
CREATE TABLE IF NOT EXISTS skills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id   INTEGER,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL,
    installed_at  TEXT NOT NULL,
    UNIQUE(instance_id, name)
);

CREATE TABLE IF NOT EXISTS apk_pushes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id          INTEGER NOT NULL,
    apk_path            TEXT NOT NULL,   -- "" for a mesh-origin push — see origin_api_key_id
    version_label       TEXT,
    created_at          TEXT NOT NULL,
    downloaded_at       TEXT,
    origin_api_key_id   INTEGER,         -- NULL = server disk; set = a device is serving its own installed APK directly
    mesh_token          TEXT,            -- single-use secret the target redeems against the origin device's mesh listener
    mesh_token_used_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_component ON telemetry_events(component, ts);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(platform, chat_id, id);
CREATE INDEX IF NOT EXISTS idx_bot_instances_platform ON bot_instances(platform);
CREATE INDEX IF NOT EXISTS idx_pairing_codes_instance_user ON pairing_codes(instance_id, user_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_lookup ON chat_sessions(instance_id, chat_id, archived_at);
CREATE INDEX IF NOT EXISTS idx_swarm_runs_swarm ON swarm_runs(swarm_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_instance ON sessions(instance_id, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(instance_id, chat_id, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_push_tokens_key ON push_tokens(api_key_id);
CREATE INDEX IF NOT EXISTS idx_apk_pushes_key ON apk_pushes(api_key_id, id);
CREATE INDEX IF NOT EXISTS idx_server_chat_conv_participants ON server_chat_conversations(participant_a, participant_b);
CREATE INDEX IF NOT EXISTS idx_server_chat_messages_conv ON server_chat_messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_support_bot_classifications_ts ON support_bot_classifications(ts);
CREATE INDEX IF NOT EXISTS idx_support_bot_pending_examples_status ON support_bot_pending_examples(status);
CREATE INDEX IF NOT EXISTS idx_scheduled_commands_due ON scheduled_commands(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_kanban_cards_board ON kanban_cards(board_id, column_name, position);
CREATE INDEX IF NOT EXISTS idx_memory_entries_instance ON memory_entries(instance_id, status);
CREATE INDEX IF NOT EXISTS idx_skills_instance ON skills(instance_id, name);

-- A user-installed local Python plugin (bot/plugins.py) that registers
-- extra agent tools and/or slash commands. `path` points at the
-- plugin.py file on this machine's disk — nothing here ever fetches code
-- over a network (see docs/adr/0007-plugins-are-trusted-local-code.md).
-- `enabled=0` keeps the row (and its install history) without loading
-- the plugin's code at startup.
CREATE TABLE IF NOT EXISTS plugins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    path          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    installed_at  TEXT NOT NULL
);

-- An operator-configured EXTERNAL MCP server (bot/agent_runtime/mcp_client.py)
-- — a stdio subprocess or a remote Streamable HTTP endpoint whose tools get
-- merged into all_tool_schemas() namespaced "mcp_<name>_<tool>". Deliberately
-- never agent-creatable (see that module's docstring) — connecting to an
-- arbitrary external process/URL is a materially bigger trust boundary than
-- create_plugin's already-approval-gated local code, so this is dashboard/
-- Telegram-config-only, same as bot/providers.py's named registry.
-- instance_id NULL means every instance can use this server's tools;
-- non-NULL scopes it to just that one bot instance.
CREATE TABLE IF NOT EXISTS external_mcp_servers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    transport     TEXT NOT NULL,            -- "stdio" or "remote"
    command       TEXT,                      -- stdio only
    args_json     TEXT NOT NULL DEFAULT '[]',
    env_json      TEXT NOT NULL DEFAULT '{}',
    url           TEXT,                      -- remote only
    auth_token    TEXT,                      -- remote only, optional static bearer token
    enabled       INTEGER NOT NULL DEFAULT 1,
    instance_id   INTEGER,
    created_at    TEXT NOT NULL,
    -- Real OAuth 2.1 (dynamic client registration + authorization code +
    -- PKCE) for a remote server that requires it instead of a static
    -- bearer token — see bot/agent_runtime/mcp_client.py's _DbTokenStorage,
    -- which reads/writes these two columns for the mcp package's own
    -- OAuthClientProvider. Mutually exclusive with auth_token in practice
    -- (a server uses one or the other), never enforced at the schema level.
    oauth_enabled           INTEGER NOT NULL DEFAULT 0,
    oauth_client_info_json  TEXT,   -- mcp.shared.auth.OAuthClientInformationFull, JSON
    oauth_tokens_json       TEXT    -- mcp.shared.auth.OAuthToken, JSON
);

-- An operator-configured lifecycle hook (bot/agent_runtime/hooks.py) —
-- Claude Code-style local automation scoped to the four events that map
-- onto real, already-centralized call sites in the native agent loop.
-- `matcher` is only meaningful for PreToolUse/PostToolUse (a tool name to
-- match, or NULL/empty to match every tool); ignored for SessionStart/
-- UserPromptSubmit. `command` is a local shell command run with the
-- event's JSON on stdin and a JSON decision/context object expected on
-- stdout — same "trusted local code" boundary as bot/plugins.py
-- (ADR-0007), so this is dashboard/Telegram-config-only, never
-- agent-creatable. instance_id NULL means every instance's turns fire it;
-- non-NULL scopes it to just that one instance.
CREATE TABLE IF NOT EXISTS agent_hooks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event         TEXT NOT NULL,   -- PreToolUse | PostToolUse | SessionStart | UserPromptSubmit
    matcher       TEXT,
    command       TEXT NOT NULL,
    instance_id   INTEGER,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_hooks_event ON agent_hooks(event, enabled);

-- A small, named markdown document any agent (any backend, any instance)
-- can read/write via the read_project_context/write_project_context
-- tools — see bot/shared_context.py. Unlike memory_entries/kanban_*
-- (both scoped to ONE instance), this is deliberately cross-instance: the
-- one place a swarm of workers and their manager keep a shared,
-- human-readable project status without needing their own conversation
-- history to carry it. `name` is a short slug ("status", "architecture",
-- caller's choice), not an id — callers think in names, not ids, the
-- same reasoning bot/skills.py's own name-keyed rows already follow.
CREATE TABLE IF NOT EXISTS shared_context_docs (
    name          TEXT PRIMARY KEY,
    content       TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL DEFAULT 'system'
);

-- Live, best-effort per-call tool-event trail for a swarm_dispatch job —
-- see bot/swarm/observability.py, which taps the Hermes gateway's SSE
-- stream for its own top-level delegate_task tool_started/tool_completed
-- events (no per-child data reaches this — see that module's docstring
-- for why). Soft FK to jobs.id, matching this project's existing
-- no-enforced-FKs convention on jobs-adjacent tables.
CREATE TABLE IF NOT EXISTS job_tool_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    event_type    TEXT NOT NULL,   -- tool_started|tool_completed
    tool_name     TEXT NOT NULL,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    ts            TEXT NOT NULL
);

-- Per-model on/off state for the custom_model/native_agent provider
-- families (see bot/models.py's browse_provider_models()/
-- live_custom_models() filtering) — sparse by design: only an explicit
-- override is stored. Absence of a row falls back to a free/paid-based
-- default (free models default enabled, paid/unpriced models default
-- disabled — see bot.models._resolve_effective_enabled) rather than a
-- flat "always enabled", so a freshly-configured provider with a real
-- API key doesn't silently expose hundreds of paid models with no
-- explicit opt-in. A provider can have hundreds of models (OpenRouter's
-- real catalog does); storing one row per model regardless of state
-- would make this table needlessly large for no benefit.
CREATE TABLE IF NOT EXISTS model_toggles (
    provider    TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    enabled     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (provider, model_id)
);

-- Unified agent/swarm control settings (see bot/agent_settings.py) — one
-- row per bot_instances.id, or a single instance_id=NULL row for the
-- process-wide default an instance falls back to when it has no row of
-- its own. Sparse (any column may be NULL, meaning "use the next
-- fallback level down" — see agent_settings.get()'s three-level
-- resolution) rather than every instance getting a fully-populated row
-- at creation time, matching model_toggles' own sparse-by-design
-- reasoning above. Hermes-backed instances do NOT use worker_effort/
-- manager_effort here — those write straight through to
-- bot/hermes_config.py's delegation.reasoning_effort/agent.reasoning_effort
-- instead, since Hermes already owns that state in its own config.yaml
-- and this table must never become a second, divergent source of truth
-- for it.
CREATE TABLE IF NOT EXISTS agent_settings (
    instance_id             INTEGER UNIQUE,
    max_concurrent_children INTEGER,
    worker_provider         TEXT,
    worker_model            TEXT,
    worker_effort           TEXT,
    manager_effort          TEXT,
    fallback_provider       TEXT,
    fallback_model          TEXT,
    -- Plan-mode analog (Phase G of the Claude API/Claude Code parity
    -- plan) — see bot/agent_runtime/approval.py::request_plan_approval()
    -- and NativeAgentBackend.ask(). NULL/unset falls through the usual
    -- three-level chain to "off" (bot/agent_settings.py's own
    -- _hardcoded_default), so an instance with nothing configured
    -- behaves exactly as before this column existed.
    require_plan_approval   INTEGER,
    -- Admin control surface plan — marks the ONE instance whose tool
    -- loop gets bot/agent_runtime/tools.py's admin_* tools. NULL/unset
    -- falls through to False via agent_settings.py's own
    -- _hardcoded_default, so nothing changes for any instance until an
    -- operator explicitly sets this via the dashboard/MCP channel.
    is_admin_instance      INTEGER,
    updated_at              TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_settings_instance_null
    ON agent_settings ((instance_id IS NULL)) WHERE instance_id IS NULL;

-- Global emergency-stop sentinel (see bot/agent_runtime/estop.py) —
-- mirrors Hermes Agent's own estop.py concept (a global pause checked by
-- long-running components before starting new work), DB-backed here
-- instead of a sentinel file since AgenticBotPlatform already centralizes runtime
-- state in SQLite. Always exactly one row (id=1); checked at the top of
-- every new-turn/new-dispatch entry point, never mid-turn — an
-- already-running turn finishes rather than being killed.
CREATE TABLE IF NOT EXISTS estop_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    engaged    INTEGER NOT NULL DEFAULT 0,
    reason     TEXT,
    actor      TEXT,
    changed_at TEXT NOT NULL
);

-- Post-hoc per-child delegate_task breakdown, parsed from a completed
-- swarm_dispatch job's own final reply (see bot/swarm/child_parser.py) —
-- written once, as a full replace, when the dispatch finishes. This is
-- the actual "per-child detail" data; job_tool_events above is only a
-- live top-level status signal.
CREATE TABLE IF NOT EXISTS job_children (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL,
    child_index     INTEGER NOT NULL,
    goal            TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT '',
    result_excerpt  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- One row per ephemeral sub-agent spawned by spawn_subagent (see
-- bot/agent_runtime/subagents.py) — deliberately NOT a bot_instances row:
-- a spawned worker is disposable and has no persistent identity, so
-- giving it one would clutter the Bots tab with a throwaway row per
-- dispatch. Pruned by the existing retention mechanism (bot/retention.py)
-- like jobs/telemetry_events, not a bespoke cleanup job.
CREATE TABLE IF NOT EXISTS ephemeral_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_instance_id  INTEGER,
    backend             TEXT NOT NULL,
    model               TEXT NOT NULL,
    goal                TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'running',
    result              TEXT,
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);

-- SSH Toolkit session monitor/recording (see bot/ssh_session_monitor.py):
-- structured events (not video/screen-share) from a watched SSH command -
-- start/stdout/stderr/metric/exit - so a GUI can show every action an agent
-- or user takes over an SSH connection live, and a recording can replay it
-- exactly later. One row per recording; its events live in
-- ssh_session_events, same shape as job_tool_events above.
CREATE TABLE IF NOT EXISTS ssh_session_recordings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_name TEXT NOT NULL,
    command         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'recording',  -- recording|paused|stopped
    started_at      TEXT NOT NULL,
    stopped_at      TEXT
);

CREATE TABLE IF NOT EXISTS ssh_session_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id  INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    event_type    TEXT NOT NULL,  -- start|stdout|stderr|metric|exit|note
    payload_json  TEXT NOT NULL DEFAULT '{}',
    ts            TEXT NOT NULL
);
"""
# idx_jobs_instance / idx_jobs_swarm_run / idx_messages_instance are created
# in _migrate(), not here — on a pre-existing DB, jobs/messages get their
# instance_id/swarm_run_id columns via ALTER TABLE in _migrate(), which
# runs *after* this script; indexing them here would fail with "no such
# column" the first time this runs against an existing database.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        # Multiple paired devices can poll/write concurrently — wait out a
        # brief lock instead of raising "database is locked" immediately.
        conn.execute("PRAGMA busy_timeout=5000;")
    except sqlite3.Error:
        # A corrupt file fails here, after connect() already opened it. Close it,
        # or the handle stays open and (on Windows) blocks the repair that has to
        # replace the file.
        conn.close()
        raise
    return conn


def get_conn() -> sqlite3.Connection:
    """The primary connection for the thread that opened it (the event loop),
    or this thread's own connection to the same file for any other thread.
    Thread connections are dropped and reopened whenever the primary is
    replaced (a test pointing DB_PATH elsewhere, a snapshot restore)."""
    global _conn, _owner, _generation
    if _conn is None:
        _conn = _open(DB_PATH)
        _owner = threading.get_ident()
        _generation += 1
    if threading.get_ident() == _owner:
        return _conn
    local = _thread_local
    conn = getattr(local, "conn", None)
    if conn is None or local.generation != _generation:
        if conn is not None:
            _forget(conn)
        conn = _open(DB_PATH)
        local.conn, local.generation = conn, _generation
        with _thread_conns_lock:
            _thread_conns.add(conn)
    return conn


def _forget(conn: sqlite3.Connection) -> None:
    with _thread_conns_lock:
        _thread_conns.discard(conn)
    try:
        conn.close()
    except sqlite3.Error:
        pass


def close_conn() -> None:
    """Closes and drops every connection (the primary and all per-thread
    ones) so the next get_conn() call reopens fresh — used by
    bot/snapshots.py's restore_snapshot() and bot/sentinel's database repair
    to swap the underlying file safely (a plain file copy while an old
    connection is still open could either fail on Windows or leave a
    stale WAL/SHM pointing at the replaced file)."""
    global _conn, _generation
    _generation += 1
    with _thread_conns_lock:
        stale = list(_thread_conns)
        _thread_conns.clear()
    for c in stale:
        try:
            c.close()
        except sqlite3.Error:
            pass
    if _conn is not None:
        _conn.close()
        _conn = None


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema patches for databases created before a
    column existed — CREATE TABLE IF NOT EXISTS in SCHEMA only helps fresh
    installs; an existing messages table from before multi-platform support
    needs its new column added explicitly."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "platform" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'")
    if "instance_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN instance_id INTEGER")
    if "attachment_path" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_path TEXT")
    if "attachment_name" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_name TEXT")
    if "attachment_mime" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_mime TEXT")
    if "attachment_size" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_size INTEGER")
    if "thumbnail_path" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN thumbnail_path TEXT")

    if "session_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER")

    mem_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_entries)").fetchall()}
    if "kind" not in mem_cols:
        conn.execute("ALTER TABLE memory_entries ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'")
    if "uses" not in mem_cols:
        conn.execute("ALTER TABLE memory_entries ADD COLUMN uses INTEGER NOT NULL DEFAULT 0")
    if "last_used" not in mem_cols:
        conn.execute("ALTER TABLE memory_entries ADD COLUMN last_used TEXT")

    job_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "instance_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN instance_id INTEGER")
    if "swarm_run_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN swarm_run_id TEXT")
    if "session_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN session_id INTEGER")
    if "chat_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN chat_id TEXT")

    instance_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bot_instances)").fetchall()}
    if "can_target" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN can_target TEXT NOT NULL DEFAULT '[]'")
    if "model" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN model TEXT")
    if "desktop_session_key" not in instance_cols:
        # Links this instance to one specific already-created chat/session in
        # a real desktop agent app — see bot/backends/ui_backend.py and
        # hermes_gateway_backend.py. NULL means "no session created yet" —
        # those two backends must never fall back to "whatever's open" when
        # this is NULL; see Router.create_session().
        conn.execute("ALTER TABLE bot_instances ADD COLUMN desktop_session_key TEXT")
    if "custom_instructions" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN custom_instructions TEXT")
    if "persona" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN persona TEXT NOT NULL DEFAULT 'assistant'")
    if "admin_user_ids" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN admin_user_ids TEXT NOT NULL DEFAULT '[]'")
    if "hermes_home" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN hermes_home TEXT")
    if "desktop_project" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN desktop_project TEXT")
    if "desktop_workspace_dir" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN desktop_workspace_dir TEXT")
    if "desktop_effort" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN desktop_effort TEXT NOT NULL DEFAULT 'low'")

    agent_settings_cols = {row["name"] for row in conn.execute("PRAGMA table_info(agent_settings)").fetchall()}
    if "fallback_provider" not in agent_settings_cols:
        conn.execute("ALTER TABLE agent_settings ADD COLUMN fallback_provider TEXT")
    if "fallback_model" not in agent_settings_cols:
        conn.execute("ALTER TABLE agent_settings ADD COLUMN fallback_model TEXT")
    if "require_plan_approval" not in agent_settings_cols:
        conn.execute("ALTER TABLE agent_settings ADD COLUMN require_plan_approval INTEGER")
    if "is_admin_instance" not in agent_settings_cols:
        conn.execute("ALTER TABLE agent_settings ADD COLUMN is_admin_instance INTEGER")

    server_chat_msg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(server_chat_messages)").fetchall()}
    if "kind" not in server_chat_msg_cols:
        conn.execute("ALTER TABLE server_chat_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'message'")
    if "approval_id" not in server_chat_msg_cols:
        conn.execute("ALTER TABLE server_chat_messages ADD COLUMN approval_id INTEGER")

    support_bot_class_cols = {row["name"] for row in conn.execute("PRAGMA table_info(support_bot_classifications)").fetchall()}
    if "reviewed" not in support_bot_class_cols:
        conn.execute("ALTER TABLE support_bot_classifications ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")

    chat_session_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    if "thread_id" not in chat_session_cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN thread_id TEXT")

    scheduled_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_commands)").fetchall()}
    if "thread_id" not in scheduled_cols:
        conn.execute("ALTER TABLE scheduled_commands ADD COLUMN thread_id TEXT")
    if "consecutive_failures" not in scheduled_cols:
        conn.execute("ALTER TABLE scheduled_commands ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0")
    if "last_error" not in scheduled_cols:
        conn.execute("ALTER TABLE scheduled_commands ADD COLUMN last_error TEXT")

    external_mcp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(external_mcp_servers)").fetchall()}
    if "oauth_enabled" not in external_mcp_cols:
        conn.execute("ALTER TABLE external_mcp_servers ADD COLUMN oauth_enabled INTEGER NOT NULL DEFAULT 0")
    if "oauth_client_info_json" not in external_mcp_cols:
        conn.execute("ALTER TABLE external_mcp_servers ADD COLUMN oauth_client_info_json TEXT")
    if "oauth_tokens_json" not in external_mcp_cols:
        conn.execute("ALTER TABLE external_mcp_servers ADD COLUMN oauth_tokens_json TEXT")

    presence_cols = {row["name"] for row in conn.execute("PRAGMA table_info(device_presence)").fetchall()}
    if "device_model" not in presence_cols:
        conn.execute("ALTER TABLE device_presence ADD COLUMN device_model TEXT")
    if "os_version" not in presence_cols:
        conn.execute("ALTER TABLE device_presence ADD COLUMN os_version TEXT")
    if "local_ip" not in presence_cols:
        conn.execute("ALTER TABLE device_presence ADD COLUMN local_ip TEXT")
    if "mesh_port" not in presence_cols:
        conn.execute("ALTER TABLE device_presence ADD COLUMN mesh_port INTEGER")

    apk_push_cols = {row["name"] for row in conn.execute("PRAGMA table_info(apk_pushes)").fetchall()}
    if "origin_api_key_id" not in apk_push_cols:
        # NULL means the file lives on the server's own disk (today's
        # behavior — desktop-built APK). Non-NULL means a *device* is the
        # source: its bytes are its own installed app, served directly over
        # the mesh listener at that device's device_presence.local_ip —
        # this server never touches the file in that case.
        conn.execute("ALTER TABLE apk_pushes ADD COLUMN origin_api_key_id INTEGER")
    if "mesh_token" not in apk_push_cols:
        conn.execute("ALTER TABLE apk_pushes ADD COLUMN mesh_token TEXT")
    if "mesh_token_used_at" not in apk_push_cols:
        conn.execute("ALTER TABLE apk_pushes ADD COLUMN mesh_token_used_at TEXT")

    audit_cols = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    if "job_id" not in audit_cols:
        # Lets the Delegation Activity dashboard panel link a swarm_dispatch
        # audit row to that dispatch's own job_tool_events/job_children rows
        # (see bot/swarm/observability.py / child_parser.py) — NULL for
        # every other audit action, and for swarm_dispatch_blocked (refused
        # before a job was ever created).
        conn.execute("ALTER TABLE audit_log ADD COLUMN job_id INTEGER")

    api_key_cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "kind" not in api_key_cols:
        # 'device' (default, every key minted before this column existed)
        # vs 'peer_server' — a credential a linked AgenticBotPlatform installation
        # uses to call this one (see bot/peers.py). Same auth check either
        # way (verify_api_key doesn't care), but keeping them tagged lets
        # the dashboard show "Paired Devices" and "Linked Servers" as
        # separate lists instead of one confusing mixed table.
        conn.execute("ALTER TABLE api_keys ADD COLUMN kind TEXT NOT NULL DEFAULT 'device'")
    if "permission_tier" not in api_key_cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN permission_tier TEXT NOT NULL DEFAULT 'none'")

    pending_example_cols = {row["name"] for row in conn.execute("PRAGMA table_info(support_bot_pending_examples)").fetchall()}
    if "source_kind" not in pending_example_cols:
        conn.execute("ALTER TABLE support_bot_pending_examples ADD COLUMN source_kind TEXT")
    if "approved_by" not in pending_example_cols:
        conn.execute("ALTER TABLE support_bot_pending_examples ADD COLUMN approved_by TEXT")
    if "resulting_phrase_id" not in pending_example_cols:
        conn.execute("ALTER TABLE support_bot_pending_examples ADD COLUMN resulting_phrase_id INTEGER")

    phrase_cols = {row["name"] for row in conn.execute("PRAGMA table_info(support_bot_phrases)").fetchall()}
    if "module_id" not in phrase_cols:
        # Nullable by design — see bot/support_bot/knowledge_modules.py's
        # module docstring: a NULL here is resolved at read time by
        # looking the phrase's intent up against the live module
        # registry, so no destructive backfill is needed for rows that
        # predate Knowledge Modules.
        conn.execute("ALTER TABLE support_bot_phrases ADD COLUMN module_id TEXT")

    # Safe to create now — the columns above are guaranteed to exist by
    # this point, whether this is a fresh install (created in SCHEMA) or an
    # upgrade (just ALTERed in).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_instance ON jobs(instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_swarm_run ON jobs(swarm_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_instance ON messages(instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_tool_events_job ON job_tool_events(job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_children_job ON job_children(job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ephemeral_sessions_parent ON ephemeral_sessions(parent_instance_id)")


# ---------------------------------------------------------- schema versions --
# The schema version is stamped into the file itself (PRAGMA user_version), so
# any build can tell what wrote a database. To change the schema:
#   1. write `def _migration_N(conn)` doing the change (ALTER/CREATE/backfill),
#   2. append (N, _migration_N) to MIGRATIONS and set SCHEMA_VERSION = N,
#   3. also update SCHEMA so fresh installs get the same shape.
# Migrations only ever move forward. A database stamped with a NEWER version
# than this build knows is refused rather than written to, since an older
# build can't know what the newer columns and tables mean.

SCHEMA_VERSION = 1


class SchemaTooNewError(RuntimeError):
    pass


# Version 1 is the baseline: _migrate() brings any pre-versioning database
# (user_version 0) up to the shape every build since has expected. It is
# idempotent and runs on every start, as it always did.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migrate),
]


def schema_version(conn: Optional[sqlite3.Connection] = None) -> int:
    return int((conn or get_conn()).execute("PRAGMA user_version").fetchone()[0])


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is not None


def _pre_migration_backup(from_version: int) -> None:
    """A verified backup before any schema change touches real data, so a
    migration that goes wrong can always be rolled back."""
    try:
        from bot.sentinel import backup

        m = backup.create_backup(reason=f"pre-migration v{from_version}->v{SCHEMA_VERSION}")
        logger.info("pre-migration backup %s (verified=%s)", m["name"], m["verified"])
    except Exception:  # noqa: BLE001 — never block startup on the safety net itself; say so loudly
        logger.exception("pre-migration backup failed — migrating without one")


def _apply_migrations(conn: sqlite3.Connection, *, existing: bool) -> None:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"{DB_PATH} was written by a newer AgenticBotPlatform (schema v{current}; this build knows up to "
            f"v{SCHEMA_VERSION}). Upgrade AgenticBotPlatform, or restore a backup made by this version from "
            f"data/backups/."
        )
    if existing and 0 < current < SCHEMA_VERSION:
        _pre_migration_backup(current)
    _migrate(conn)  # the idempotent baseline, every start
    for version, fn in MIGRATIONS:
        if version > max(current, 1):
            logger.info("migrating database schema to v%d", version)
            fn(conn)
    if current != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


def init_db() -> None:
    conn = get_conn()
    existing = _has_user_tables(conn)
    with _lock:
        conn.executescript(SCHEMA)
        _apply_migrations(conn, existing=existing)
        conn.commit()
    ensure_server_chat_group()
    backfill_server_chat_conversations()

    # Lazy import: bot.shared_context imports bot.db at module level, so a
    # top-level import here would be circular. Safe at call time — init_db()
    # only ever runs once bot.db itself is fully loaded.
    from bot import shared_context

    shared_context.seed_default_docs()


# ------------------------------------------------------------ sessions ----

SESSION_GAP_MINUTES = 30


# ---------------------------------------------------------- chat sessions --
# The real per-chat backend-session link — see chat_sessions' schema
# comment above for how this differs from `sessions`.


# ------------------------------------------------------- agent messages ---


# ------------------------------------------------------ tool approvals ----


# ------------------------------------------------------ scheduled commands


# ------------------------------------------------------------------ kanban


# ------------------------------------------------------------------ memory


# ------------------------------------------------------------------ skills


# --------------------------------------------------------- shared context


# ----------------------------------------------------------------- plugins

# ----------------------------------------------------------- agent_settings

_AGENT_SETTINGS_COLUMNS = (
    "max_concurrent_children", "worker_provider", "worker_model", "worker_effort", "manager_effort",
    "fallback_provider", "fallback_model", "require_plan_approval", "is_admin_instance",
)


# ------------------------------------------------------------------ estop


# --------------------------------------------------------- external_mcp_servers


# --------------------------------------------------------------- agent_hooks


# ---------------------------------------------------------------- jobs ----


# ----------------------------------------------------- swarm observability


# ------------------------------------------------- SSH session recordings


# --------------------------------------------------------- ephemeral sessions


# ------------------------------------------------------------ model toggles


# ------------------------------------------------------------- logging ----


# ------------------------------------------------------------ users -------


# --------------------------------------------------------------- pairing --
# See bot/pairing.py for the rate-limiting/expiry constants and policy —
# this layer is pure storage.


# ------------------------------------------------- support bot training ---


# ----------------------------------------- support bot pending examples ---
# Synthetic training examples generated by the free-model swarm — see
# bot/support_bot/synthetic_gen.py's own module docstring for why these
# are never inserted directly into support_bot_phrases.


# ---------------------------------------------------------- api keys ------
# Mobile/device credentials — see SCHEMA's api_keys comment. Plaintext only
# ever exists in create_api_key()'s return value; every other accessor sees
# hashes/metadata only.


# ------------------------------------------------------- peer servers -----
# Other AgenticBotPlatform installations this one is linked to — see SCHEMA's
# peer_servers comment and bot/peers.py for the linking handshake.


# ---------------------------------------------------- server pairing tokens
# See server_pairing_tokens' SCHEMA comment and bot/peers.py: the one
# secret that actually crosses the network to link two servers, instead of
# the real DASHBOARD_TOKEN. Plaintext only ever exists in
# create_server_pairing_token()'s return value, same discipline as
# create_api_key().


SERVER_CHAT_DESKTOP_DEVICE_ID = 0
# The synthetic sender identity for the AgenticBotPlatform admin pipeline's own
# replies in the permanent group room (Admin control surface plan,
# Section 3) — never a real api_keys.id (those start at 1), never the
# desktop sentinel (0), so a client can distinguish "a message from
# AgenticBotPlatform itself" from every real device's own messages.
SERVER_CHAT_BOT_DEVICE_ID = -1


# -------------------------------------------------------------- chat ------


# --------------------------------------------------------- dashboard KPIs -


# Same whitelist as get_table_counts() above — kept separate because this
# one gates which table names api_export() will ever interpolate into SQL.
EXPORTABLE_TABLES = [
    "jobs", "connections_log", "telemetry_events", "mcp_events", "audit_log",
    "messages", "bot_instances", "swarms", "swarm_runs",
]


# -------------------------------------------------------------- swarms ----
# Thin SQL wrappers only — `config`/`steps` are passed through as raw JSON
# text, same as jobs.prompt/messages.text; encoding/decoding is the
# caller's job (bot/swarm/engine.py, dashboard/server.py), matching how
# this module stays a plain SQL layer everywhere else.


# ---------------------------------------------------------- domain storage --
# The per-area functions live in bot/storage/; they are part of this module's API.
# Imported last: each of those modules uses names defined above.
from bot.storage.sessions import (  # noqa: E402,F401
    _get_or_create_session, get_session, list_sessions, link_chat_session, get_active_chat_session,
    touch_active_chat_session, get_chat_session, list_chat_sessions, list_topic_sessions,
    set_chat_session_title, append_agent_message, list_agent_messages, clear_agent_messages,
    compress_agent_messages, count_legacy_items, get_legacy_items, get_session_items,
    _delete_attachment_files, delete_session, clear_legacy_items, export_session_data,
    export_legacy_data, delete_chat_messages, delete_instance_messages, export_instance_messages_data,
    export_chat_messages_data,
)
from bot.storage.approvals import (  # noqa: E402,F401
    create_pending_approval, get_pending_approval, list_pending_approvals, resolve_pending_approval,
    grant_tool_approval, has_tool_approval,
)
from bot.storage.schedules import (  # noqa: E402,F401
    create_scheduled_command, get_scheduled_command, list_scheduled_commands,
    list_due_scheduled_commands, mark_scheduled_command_ran, reset_scheduled_command_failures,
    record_scheduled_command_failure, set_scheduled_command_enabled, delete_scheduled_command,
    delete_scheduled_commands_for_instance,
)
from bot.storage.kanban import (  # noqa: E402,F401
    get_or_create_kanban_board, list_kanban_boards, get_kanban_board, create_kanban_card,
    list_kanban_cards, get_kanban_card, move_kanban_card, delete_kanban_card,
)
from bot.storage.memory import (  # noqa: E402,F401
    create_memory_entry, get_memory_entry, list_memory_entries, touch_memory_entry, delete_memory_entry,
    resolve_memory_entry,
)
from bot.storage.extensions import (  # noqa: E402,F401
    install_skill, list_skills, get_skill, delete_skill, set_context_doc, get_context_doc,
    list_context_docs, delete_context_doc, install_plugin_row, list_plugin_rows, get_plugin_row,
    set_plugin_enabled, delete_plugin_row, add_external_mcp_server, list_external_mcp_servers,
    get_external_mcp_server, set_external_mcp_server_enabled, delete_external_mcp_server,
    set_external_mcp_oauth_client_info, set_external_mcp_oauth_tokens, add_agent_hook, list_agent_hooks,
    get_agent_hook, set_agent_hook_enabled, delete_agent_hook,
)
from bot.storage.agent_settings import (  # noqa: E402,F401
    get_agent_settings_row, find_admin_instance_id, set_agent_settings_row, get_estop_state,
    set_estop_state,
)
from bot.storage.server_chat import (  # noqa: E402,F401
    clear_server_chat_messages, delete_server_chat_conversation, export_server_chat_data,
    ensure_server_chat_group, ensure_direct_conversation, backfill_server_chat_conversations,
    create_conversations_for_new_device, is_conversation_participant, list_server_chat_conversations,
    create_server_chat_message, list_server_chat_messages, get_server_chat_message,
    delete_server_chat_message,
)
from bot.storage.jobs import (  # noqa: E402,F401
    create_job, mark_job_running, mark_job_retrying, mark_job_done, get_job, list_jobs, get_latest_job,
    log_job_tool_event, list_job_tool_events, create_ssh_recording, set_ssh_recording_status,
    get_ssh_recording, list_ssh_recordings, delete_ssh_recording, log_ssh_session_event,
    list_ssh_session_events, set_job_children, list_job_children, create_ephemeral_session,
    finish_ephemeral_session, get_ephemeral_session, list_ephemeral_sessions, set_model_toggle,
    bulk_set_model_toggles, list_model_toggles, disabled_model_ids,
)
from bot.storage.telemetry import (  # noqa: E402,F401
    log_connection_event, log_telemetry, log_mcp_event, log_audit, set_audit_log_job_id, list_audit_log,
    record_config_version, list_config_history,
)
from bot.storage.devices import (  # noqa: E402,F401
    add_allowed_user, remove_allowed_user, list_allowed_users, create_pairing_code, get_pairing_code,
    get_pairing_code_by_id, count_recent_pairing_requests, count_pending_pairing_codes,
    list_pending_pairing_codes, approve_pairing_code, deny_pairing_code, create_api_key, list_api_keys,
    set_api_key_tier, update_api_key_label, revoke_api_key, get_api_key, purge_revoked_keys,
    list_devices, upsert_push_token, list_push_tokens, create_apk_push, get_pending_apk_push,
    get_apk_push, mark_apk_push_downloaded, get_device_presence, redeem_mesh_token, device_label,
    verify_api_key, api_key_kind,
)
from bot.storage.support_bot import (  # noqa: E402,F401
    list_support_bot_phrases, add_support_bot_phrase, delete_support_bot_phrase,
    log_support_bot_classification, get_support_bot_classification_stats, get_recent_misses,
    mark_support_bot_classification_reviewed, add_support_bot_pending_example,
    list_support_bot_pending_examples, get_support_bot_pending_example,
    resolve_support_bot_pending_example, revert_support_bot_pending_example,
)
from bot.storage.peers import (  # noqa: E402,F401
    create_peer_server, list_peer_servers, get_peer_server, mark_peer_server_ok, mark_peer_server_error,
    delete_peer_server, create_server_pairing_token, consume_server_pairing_token,
)
from bot.storage.stats import (  # noqa: E402,F401
    prune_old_data, get_overview, get_usage_summary, get_insights, get_jobs_timeseries_24h,
    get_jobs_by_backend_today, get_latency_by_backend, get_table_counts, get_db_size_bytes, export_table,
    get_recent_connection_events, vacuum, days_since_last_vacuum,
)
from bot.storage.chat import (  # noqa: E402,F401
    log_message, get_message, delete_message, list_messages,
)
from bot.storage.swarms import (  # noqa: E402,F401
    create_swarm, update_swarm, delete_swarm, get_swarm, list_swarms, create_swarm_run, update_swarm_run,
    get_swarm_run, list_swarm_runs,
)
