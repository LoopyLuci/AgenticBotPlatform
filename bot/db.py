"""SQLite storage layer — small, fast, and local.

WAL mode gives concurrent readers (the dashboard) a consistent view while
the bot keeps writing job/telemetry rows, without needing a separate
database server. One connection is shared process-wide behind a lock;
at this traffic scale (a single-user bot) that is simpler and just as
fast as a connection pool, and avoids a second moving part.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

# Reuses envfile's canonical-root resolution rather than Path(__file__)
# directly — a release build's bundled bot/ lives under a different folder
# than the source tree, and without pinning to one fixed root, a release
# run and a `cargo tauri dev` run would each quietly keep their own
# separate database (this bit .env the same way before it was fixed).
DB_PATH = PROJECT_ROOT / "data" / "bot.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

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

-- Another BotServer installation this one is linked to (see bot/peers.py)
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT
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
    created_at TEXT NOT NULL
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
    agreed            INTEGER NOT NULL
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
    thumbnail_path    TEXT
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
    created_at    TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_scheduled_commands_due ON scheduled_commands(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_kanban_cards_board ON kanban_cards(board_id, column_name, position);
CREATE INDEX IF NOT EXISTS idx_memory_entries_instance ON memory_entries(instance_id, status);
CREATE INDEX IF NOT EXISTS idx_skills_instance ON skills(instance_id, name);
"""
# idx_jobs_instance / idx_jobs_swarm_run / idx_messages_instance are created
# in _migrate(), not here — on a pre-existing DB, jobs/messages get their
# instance_id/swarm_run_id columns via ALTER TABLE in _migrate(), which
# runs *after* this script; indexing them here would fail with "no such
# column" the first time this runs against an existing database.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
        # Multiple paired devices can poll/write concurrently — wait out a
        # brief lock instead of raising "database is locked" immediately.
        _conn.execute("PRAGMA busy_timeout=5000;")
    return _conn


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

    chat_session_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    if "thread_id" not in chat_session_cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN thread_id TEXT")

    scheduled_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_commands)").fetchall()}
    if "thread_id" not in scheduled_cols:
        conn.execute("ALTER TABLE scheduled_commands ADD COLUMN thread_id TEXT")

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

    api_key_cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "kind" not in api_key_cols:
        # 'device' (default, every key minted before this column existed)
        # vs 'peer_server' — a credential a linked BotServer installation
        # uses to call this one (see bot/peers.py). Same auth check either
        # way (verify_api_key doesn't care), but keeping them tagged lets
        # the dashboard show "Paired Devices" and "Linked Servers" as
        # separate lists instead of one confusing mixed table.
        conn.execute("ALTER TABLE api_keys ADD COLUMN kind TEXT NOT NULL DEFAULT 'device'")

    # Safe to create now — the columns above are guaranteed to exist by
    # this point, whether this is a fresh install (created in SCHEMA) or an
    # upgrade (just ALTERed in).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_instance ON jobs(instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_swarm_run ON jobs(swarm_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_instance ON messages(instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")


def init_db() -> None:
    conn = get_conn()
    with _lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    ensure_server_chat_group()
    backfill_server_chat_conversations()


# ------------------------------------------------------------ sessions ----

SESSION_GAP_MINUTES = 30


def _get_or_create_session(
    conn: sqlite3.Connection,
    instance_id: Optional[int],
    chat_id: Optional[Any],
    first_text: str = "",
) -> Optional[int]:
    """Buckets activity for (instance_id, chat_id) into a session row, reusing
    the most recent one if it's still "current" (last activity within
    SESSION_GAP_MINUTES). Must be called with `_lock` already held by the
    caller — this issues its own execute() calls on the shared connection,
    not a separate one, to stay inside the caller's transaction.

    Returns None when instance_id is unset (pre-multi-instance/"legacy"
    activity) — nothing meaningful to bucket a session under."""
    if instance_id is None:
        return None
    chat_key = str(chat_id) if chat_id is not None else None
    now = _now()
    row = conn.execute(
        "SELECT id, last_activity_at FROM sessions WHERE instance_id=? AND "
        "(chat_id=? OR (chat_id IS NULL AND ? IS NULL)) ORDER BY last_activity_at DESC LIMIT 1",
        (instance_id, chat_key, chat_key),
    ).fetchone()
    if row:
        last = datetime.fromisoformat(row["last_activity_at"])
        if datetime.now(timezone.utc) - last <= timedelta(minutes=SESSION_GAP_MINUTES):
            conn.execute(
                "UPDATE sessions SET last_activity_at=?, item_count=item_count+1 WHERE id=?",
                (now, row["id"]),
            )
            return row["id"]
    title = (first_text or "").strip().replace("\n", " ")[:60] or "New session"
    cur = conn.execute(
        "INSERT INTO sessions (instance_id, chat_id, title, started_at, last_activity_at, item_count) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (instance_id, chat_key, title, now, now),
    )
    return cur.lastrowid


def get_session(session_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def list_sessions(
    instance_id: Optional[int] = None,
    q: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    conn = get_conn()
    clauses = []
    params: list[Any] = []
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
    if q:
        clauses.append("title LIKE ?")
        params.append(f"%{q}%")
    if since:
        clauses.append("last_activity_at>=?")
        params.append(since)
    if until:
        clauses.append("last_activity_at<=?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM sessions {where} ORDER BY last_activity_at DESC LIMIT ?", params
    ).fetchall()


# ---------------------------------------------------------- chat sessions --
# The real per-chat backend-session link — see chat_sessions' schema
# comment above for how this differs from `sessions`.

def link_chat_session(
    instance_id: int, chat_id: Any, key: str, title: Optional[str] = None, thread_id: Optional[Any] = None
) -> int:
    """Archives whichever chat_sessions row is currently active for this
    (instance_id, chat_id, thread_id) and inserts a fresh active row
    pointing at `key`. Used by both /new (a freshly created backend key)
    and /resume (an old key pulled back out of history) — the only
    difference between them is where `key` came from. thread_id is a
    Telegram forum-topic id (see /topic) — None means the chat's root
    session, same as before that feature existed, so every non-topic chat
    is unaffected."""
    conn = get_conn()
    now = _now()
    tid = str(thread_id) if thread_id is not None else None
    with _lock:
        conn.execute(
            "UPDATE chat_sessions SET archived_at=? WHERE instance_id=? AND chat_id=? AND thread_id IS ? AND archived_at IS NULL",
            (now, instance_id, str(chat_id), tid),
        )
        cur = conn.execute(
            "INSERT INTO chat_sessions (instance_id, chat_id, thread_id, desktop_session_key, title, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (instance_id, str(chat_id), tid, key, title, now, now),
        )
        conn.commit()
        return cur.lastrowid


def get_active_chat_session(instance_id: int, chat_id: Any, thread_id: Optional[Any] = None) -> Optional[sqlite3.Row]:
    conn = get_conn()
    tid = str(thread_id) if thread_id is not None else None
    return conn.execute(
        "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? AND thread_id IS ? AND archived_at IS NULL",
        (instance_id, str(chat_id), tid),
    ).fetchone()


def touch_active_chat_session(instance_id: int, chat_id: Any, thread_id: Optional[Any] = None) -> None:
    conn = get_conn()
    tid = str(thread_id) if thread_id is not None else None
    with _lock:
        conn.execute(
            "UPDATE chat_sessions SET last_used_at=? WHERE instance_id=? AND chat_id=? AND thread_id IS ? AND archived_at IS NULL",
            (_now(), instance_id, str(chat_id), tid),
        )
        conn.commit()


def get_chat_session(chat_session_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM chat_sessions WHERE id=?", (chat_session_id,)).fetchone()


def list_chat_sessions(
    instance_id: int, chat_id: Optional[Any] = None, limit: int = 20, thread_id: Optional[Any] = None
) -> list[sqlite3.Row]:
    """Every link ever made for this instance (optionally narrowed to one
    chat, and further to one forum topic within it) — active one first
    (it's always most recent), then history."""
    conn = get_conn()
    if chat_id is not None and thread_id is not None:
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? AND thread_id IS ? ORDER BY created_at DESC LIMIT ?",
            (instance_id, str(chat_id), str(thread_id), limit),
        ).fetchall()
    if chat_id is not None:
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? ORDER BY created_at DESC LIMIT ?",
            (instance_id, str(chat_id), limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM chat_sessions WHERE instance_id=? ORDER BY created_at DESC LIMIT ?",
        (instance_id, limit),
    ).fetchall()


def list_topic_sessions(instance_id: int, chat_id: Any) -> list[sqlite3.Row]:
    """Every forum topic in this group that has its own active session —
    for /topic list."""
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? AND thread_id IS NOT NULL AND archived_at IS NULL "
        "ORDER BY last_used_at DESC",
        (instance_id, str(chat_id)),
    ).fetchall()


def set_chat_session_title(chat_session_id: int, title: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE chat_sessions SET title=? WHERE id=?", (title, chat_session_id))
        conn.commit()


# ------------------------------------------------------- agent messages ---

def append_agent_message(session_key: str, role: str, content: Any) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO agent_messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_key, role, json.dumps(content), _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_agent_messages(session_key: str, limit: int = 200) -> list[dict]:
    """Oldest-first, ready to feed straight into the Anthropic Messages API
    as the `messages` list (each row's content is already in that shape —
    see agent_messages' schema comment)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM agent_messages WHERE session_key=? ORDER BY id ASC LIMIT ?",
        (session_key, limit),
    ).fetchall()
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]


def clear_agent_messages(session_key: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM agent_messages WHERE session_key=?", (session_key,))
        conn.commit()


# ------------------------------------------------------ tool approvals ----

def create_pending_approval(instance_id: int, chat_id: Any, session_key: str, tool_name: str, tool_input: dict) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO pending_approvals (instance_id, chat_id, session_key, tool_name, tool_input, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (instance_id, str(chat_id), session_key, tool_name, json.dumps(tool_input), _now()),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_approval(approval_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM pending_approvals WHERE id=?", (approval_id,)).fetchone()


def list_pending_approvals(instance_id: int, chat_id: Optional[Any] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if chat_id is not None:
        return conn.execute(
            "SELECT * FROM pending_approvals WHERE instance_id=? AND chat_id=? AND status='pending' ORDER BY id ASC",
            (instance_id, str(chat_id)),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM pending_approvals WHERE instance_id=? AND status='pending' ORDER BY id ASC", (instance_id,)
    ).fetchall()


def resolve_pending_approval(approval_id: int, status: str, resolved_by: Optional[str]) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE pending_approvals SET status=?, resolved_at=?, resolved_by=? WHERE id=?",
            (status, _now(), resolved_by, approval_id),
        )
        conn.commit()


def grant_tool_approval(instance_id: int, tool_name: str, session_key: Optional[str]) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO tool_approvals (instance_id, session_key, tool_name, granted_at) VALUES (?, ?, ?, ?)",
            (instance_id, session_key, tool_name, _now()),
        )
        conn.commit()


def has_tool_approval(instance_id: int, session_key: str, tool_name: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM tool_approvals WHERE instance_id=? AND tool_name=? AND (session_key IS NULL OR session_key=?) LIMIT 1",
        (instance_id, tool_name, session_key),
    ).fetchone()
    return row is not None


# ------------------------------------------------------ scheduled commands

def create_scheduled_command(
    instance_id: int, chat_id: Any, kind: str, prompt: str, interval_s: int,
    next_run_at: str, max_runs: Optional[int] = None, thread_id: Optional[Any] = None,
) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO scheduled_commands (instance_id, chat_id, thread_id, kind, prompt, interval_s, next_run_at, max_runs, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (instance_id, str(chat_id), str(thread_id) if thread_id is not None else None,
             kind, prompt, interval_s, next_run_at, max_runs, _now()),
        )
        conn.commit()
        return cur.lastrowid


def get_scheduled_command(sched_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM scheduled_commands WHERE id=?", (sched_id,)).fetchone()


def list_scheduled_commands(
    instance_id: int, chat_id: Optional[Any] = None, thread_id: Optional[Any] = None
) -> list[sqlite3.Row]:
    conn = get_conn()
    if chat_id is not None and thread_id is not None:
        return conn.execute(
            "SELECT * FROM scheduled_commands WHERE instance_id=? AND chat_id=? AND thread_id IS ? ORDER BY id",
            (instance_id, str(chat_id), str(thread_id)),
        ).fetchall()
    if chat_id is not None:
        return conn.execute(
            "SELECT * FROM scheduled_commands WHERE instance_id=? AND chat_id=? ORDER BY id", (instance_id, str(chat_id))
        ).fetchall()
    return conn.execute("SELECT * FROM scheduled_commands WHERE instance_id=? ORDER BY id", (instance_id,)).fetchall()


def list_due_scheduled_commands(now_iso: str) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM scheduled_commands WHERE enabled=1 AND next_run_at<=? "
        "AND (max_runs IS NULL OR run_count<max_runs)",
        (now_iso,),
    ).fetchall()


def mark_scheduled_command_ran(sched_id: int, next_run_at: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE scheduled_commands SET last_run_at=?, next_run_at=?, run_count=run_count+1 WHERE id=?",
            (_now(), next_run_at, sched_id),
        )
        conn.commit()


def set_scheduled_command_enabled(sched_id: int, enabled: bool) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE scheduled_commands SET enabled=? WHERE id=?", (1 if enabled else 0, sched_id))
        conn.commit()


def delete_scheduled_command(sched_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM scheduled_commands WHERE id=?", (sched_id,))
        conn.commit()


def delete_scheduled_commands_for_instance(instance_id: int) -> int:
    """Called when a bot instance is deleted — unlike jobs/messages (kept
    intentionally as history), a scheduled command is a *live* recurring
    task with nothing left to run against once its instance is gone, so it
    has to be cleaned up rather than preserved. Without this, an orphaned
    row keeps coming due forever: the scheduler has no natural reason to
    ever stop polling a row that still says enabled=1. Returns how many
    were removed, for the caller's audit log."""
    conn = get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM scheduled_commands WHERE instance_id=?", (instance_id,))
        conn.commit()
        return cur.rowcount


# ------------------------------------------------------------------ kanban

def get_or_create_kanban_board(instance_id: int, name: str) -> int:
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT id FROM kanban_boards WHERE instance_id=? AND name=?", (instance_id, name)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO kanban_boards (instance_id, name, created_at) VALUES (?, ?, ?)",
            (instance_id, name, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_kanban_boards(instance_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM kanban_boards WHERE instance_id=? ORDER BY name", (instance_id,)).fetchall()


def get_kanban_board(board_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM kanban_boards WHERE id=?", (board_id,)).fetchone()


def create_kanban_card(board_id: int, column_name: str, text: str) -> int:
    conn = get_conn()
    with _lock:
        pos_row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM kanban_cards WHERE board_id=? AND column_name=?",
            (board_id, column_name),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO kanban_cards (board_id, column_name, text, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (board_id, column_name, text, pos_row["p"], _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_kanban_cards(board_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM kanban_cards WHERE board_id=? ORDER BY column_name, position", (board_id,)
    ).fetchall()


def get_kanban_card(card_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM kanban_cards WHERE id=?", (card_id,)).fetchone()


def move_kanban_card(card_id: int, column_name: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE kanban_cards SET column_name=?, updated_at=? WHERE id=?", (column_name, _now(), card_id)
        )
        conn.commit()


def delete_kanban_card(card_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM kanban_cards WHERE id=?", (card_id,))
        conn.commit()


# ------------------------------------------------------------------ memory

def create_memory_entry(instance_id: int, content: str, source: str = "user", status: str = "pending") -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO memory_entries (instance_id, content, status, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (instance_id, content, status, source, _now()),
        )
        conn.commit()
        return cur.lastrowid


def get_memory_entry(entry_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM memory_entries WHERE id=?", (entry_id,)).fetchone()


def list_memory_entries(instance_id: int, status: Optional[str] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if status is not None:
        return conn.execute(
            "SELECT * FROM memory_entries WHERE instance_id=? AND status=? ORDER BY id DESC", (instance_id, status)
        ).fetchall()
    return conn.execute("SELECT * FROM memory_entries WHERE instance_id=? ORDER BY id DESC", (instance_id,)).fetchall()


def resolve_memory_entry(entry_id: int, status: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE memory_entries SET status=?, resolved_at=? WHERE id=?", (status, _now(), entry_id)
        )
        conn.commit()


# ------------------------------------------------------------------ skills

def install_skill(instance_id: Optional[int], name: str, description: str, content: str) -> int:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM skills WHERE instance_id IS ? AND name=?", (instance_id, name))
        cur = conn.execute(
            "INSERT INTO skills (instance_id, name, description, content, installed_at) VALUES (?, ?, ?, ?, ?)",
            (instance_id, name, description, content, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_skills(instance_id: Optional[int]) -> list[sqlite3.Row]:
    """Skills visible to this instance: its own plus every global
    (instance_id IS NULL) one."""
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM skills WHERE instance_id IS NULL OR instance_id=? ORDER BY name", (instance_id,)
    ).fetchall()


def get_skill(instance_id: Optional[int], name: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM skills WHERE (instance_id IS NULL OR instance_id=?) AND name=? "
        "ORDER BY instance_id IS NULL LIMIT 1",
        (instance_id, name),
    ).fetchone()


def delete_skill(instance_id: Optional[int], name: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM skills WHERE instance_id IS ? AND name=?", (instance_id, name))
        conn.commit()


def count_legacy_items(instance_id: int) -> int:
    """Rows predating the sessions feature (session_id IS NULL) for one
    instance — surfaced as a synthetic "Before sessions" bucket rather than
    backfilled, since bucketing thousands of historical rows on upgrade is a
    materially riskier migration than this file's usual additive ALTERs."""
    conn = get_conn()
    msgs = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE instance_id=? AND session_id IS NULL", (instance_id,)
    ).fetchone()["c"]
    jobs = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE instance_id=? AND session_id IS NULL", (instance_id,)
    ).fetchone()["c"]
    return msgs + jobs


def get_legacy_items(instance_id: int) -> dict[str, list[sqlite3.Row]]:
    conn = get_conn()
    return {
        "messages": conn.execute(
            "SELECT * FROM messages WHERE instance_id=? AND session_id IS NULL ORDER BY id ASC",
            (instance_id,),
        ).fetchall(),
        "jobs": conn.execute(
            "SELECT * FROM jobs WHERE instance_id=? AND session_id IS NULL ORDER BY id ASC",
            (instance_id,),
        ).fetchall(),
    }


def get_session_items(session_id: int) -> dict[str, list[sqlite3.Row]]:
    conn = get_conn()
    return {
        "messages": conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall(),
        "jobs": conn.execute(
            "SELECT * FROM jobs WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall(),
    }


def _delete_attachment_files(rows) -> None:
    """Best-effort cleanup of the attachment/thumbnail files backing a set
    of message rows about to be deleted — avoids leaving orphaned files
    under data/attachments behind every chat delete. Never raises: a
    missing or already-gone file just means there's nothing to clean up."""
    from bot import attachments

    for row in rows:
        keys = row.keys() if hasattr(row, "keys") else row
        for col, root in (("attachment_path", attachments.ATTACHMENTS_DIR), ("thumbnail_path", attachments.THUMBS_DIR)):
            rel = row[col] if col in keys and row[col] else None
            if not rel:
                continue
            try:
                full = (root / rel).resolve()
                if full.is_relative_to(root.resolve()) and full.is_file():
                    full.unlink()
            except OSError:
                pass


def delete_session(session_id: int) -> bool:
    """Permanently deletes one Sessions-tab bucket and every message/job
    filed under it (plus their attachment files). Returns False if the
    session doesn't exist."""
    conn = get_conn()
    with _lock:
        if conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
            return False
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM messages WHERE session_id=?", (session_id,)
        ).fetchall()
        _delete_attachment_files(msg_rows)
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
        return True


def clear_legacy_items(instance_id: int) -> int:
    """Deletes every pre-sessions-feature message/job for `instance_id`
    (session_id IS NULL — the "Before sessions" bucket) plus their
    attachment files. Returns the number of messages removed."""
    conn = get_conn()
    with _lock:
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM messages WHERE instance_id=? AND session_id IS NULL",
            (instance_id,),
        ).fetchall()
        _delete_attachment_files(msg_rows)
        cur = conn.execute("DELETE FROM messages WHERE instance_id=? AND session_id IS NULL", (instance_id,))
        conn.execute("DELETE FROM jobs WHERE instance_id=? AND session_id IS NULL", (instance_id,))
        conn.commit()
        return cur.rowcount


def export_session_data(session_id: int) -> Optional[dict]:
    """One session's full backup document — metadata plus every message
    and job filed under it, plain-dict rows ready to JSON-serialize."""
    session = get_session(session_id)
    if session is None:
        return None
    items = get_session_items(session_id)
    return {
        "session": dict(session),
        "messages": [dict(r) for r in items["messages"]],
        "jobs": [dict(r) for r in items["jobs"]],
    }


def export_legacy_data(instance_id: int) -> dict:
    items = get_legacy_items(instance_id)
    return {
        "session": {"id": f"legacy-{instance_id}", "instance_id": instance_id, "title": "Before sessions"},
        "messages": [dict(r) for r in items["messages"]],
        "jobs": [dict(r) for r in items["jobs"]],
    }


def delete_chat_messages(instance_id: int, chat_id: Any, platform: Optional[str] = None) -> int:
    """Deletes every logged message for one Chat-tab conversation (a given
    bot instance + platform-native chat id), plus their attachment files.
    Scoped to `messages` only — the Sessions/Jobs history for that same
    chat is a separate view (Sessions tab) and is left untouched, matching
    exactly what the Chat tab itself displays. Returns the number removed."""
    conn = get_conn()
    with _lock:
        clauses = ["instance_id=?", "chat_id=?"]
        params: list[Any] = [instance_id, str(chat_id)]
        if platform is not None:
            clauses.append("platform=?")
            params.append(platform)
        where = " AND ".join(clauses)
        msg_rows = conn.execute(f"SELECT attachment_path, thumbnail_path FROM messages WHERE {where}", params).fetchall()
        _delete_attachment_files(msg_rows)
        cur = conn.execute(f"DELETE FROM messages WHERE {where}", params)
        conn.commit()
        return cur.rowcount


def delete_instance_messages(instance_id: int) -> int:
    """Deletes every logged message for one bot instance across every chat
    it's ever talked to, plus their attachment files — the Chat tab shows
    one merged timeline per instance regardless of chat_id, so "clear this
    bot's history" means all of it, not one chat_id. Returns the number
    removed."""
    conn = get_conn()
    with _lock:
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM messages WHERE instance_id=?", (instance_id,)
        ).fetchall()
        _delete_attachment_files(msg_rows)
        cur = conn.execute("DELETE FROM messages WHERE instance_id=?", (instance_id,))
        conn.commit()
        return cur.rowcount


def export_instance_messages_data(instance_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM messages WHERE instance_id=? ORDER BY id ASC", (instance_id,)).fetchall()
    return [dict(r) for r in rows]


def export_chat_messages_data(instance_id: int, chat_id: Any, platform: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    clauses = ["instance_id=?", "chat_id=?"]
    params: list[Any] = [instance_id, str(chat_id)]
    if platform is not None:
        clauses.append("platform=?")
        params.append(platform)
    where = " AND ".join(clauses)
    rows = conn.execute(f"SELECT * FROM messages WHERE {where} ORDER BY id ASC", params).fetchall()
    return [dict(r) for r in rows]


def clear_server_chat_messages(conversation_id: int) -> int:
    """Deletes every message in one Server Chat conversation, plus their
    attachment/thumbnail files. The conversation row itself stays — group
    and direct rooms are structural (auto-recreated on device pairing),
    only their history is being cleared. Returns the number removed."""
    conn = get_conn()
    with _lock:
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM server_chat_messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchall()
        _delete_attachment_files(msg_rows)
        cur = conn.execute("DELETE FROM server_chat_messages WHERE conversation_id=?", (conversation_id,))
        conn.commit()
        return cur.rowcount


def export_server_chat_data(conversation_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM server_chat_messages WHERE conversation_id=? ORDER BY id ASC", (conversation_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- jobs ----

def create_job(
    action_type: str,
    backend: str,
    user_id: int,
    prompt: str,
    instance_id: Optional[int] = None,
    swarm_run_id: Optional[str] = None,
    chat_id: Optional[Any] = None,
) -> int:
    conn = get_conn()
    with _lock:
        chat_key = str(chat_id) if chat_id is not None else None
        session_id = _get_or_create_session(conn, instance_id, chat_key, prompt)
        cur = conn.execute(
            "INSERT INTO jobs (action_type, backend, status, user_id, prompt, created_at, instance_id, swarm_run_id, chat_id, session_id) "
            "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (action_type, backend, user_id, prompt, _now(), instance_id, swarm_run_id, chat_key, session_id),
        )
        conn.commit()
        return cur.lastrowid


def mark_job_running(job_id: int, backend: Optional[str] = None) -> None:
    conn = get_conn()
    with _lock:
        if backend:
            conn.execute(
                "UPDATE jobs SET status='running', backend=?, started_at=? WHERE id=?",
                (backend, _now(), job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?", (_now(), job_id)
            )
        conn.commit()


def mark_job_retrying(job_id: int, backend: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE jobs SET status='retrying', backend=? WHERE id=?", (backend, job_id)
        )
        conn.commit()


def mark_job_done(
    job_id: int,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
    tokens: Optional[int] = None,
) -> None:
    conn = get_conn()
    with _lock:
        row = conn.execute("SELECT started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        finished = _now()
        duration_ms = None
        if row and row["started_at"]:
            started = datetime.fromisoformat(row["started_at"])
            duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        conn.execute(
            "UPDATE jobs SET status=?, result=?, error=?, tokens=?, finished_at=?, duration_ms=? "
            "WHERE id=?",
            (status, result, error, tokens, finished, duration_ms, job_id),
        )
        conn.commit()


def get_job(job_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(
    limit: int = 50,
    status: Optional[str] = None,
    instance_id: Optional[int] = None,
    swarm_run_id: Optional[str] = None,
) -> list[sqlite3.Row]:
    conn = get_conn()
    clauses = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status=?")
        params.append(status)
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
    if swarm_run_id is not None:
        clauses.append("swarm_run_id=?")
        params.append(swarm_run_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", params).fetchall()


# ------------------------------------------------------------- logging ----

def log_connection_event(component: str, event: str, detail: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO connections_log (ts, component, event, detail) VALUES (?, ?, ?, ?)",
            (_now(), component, event, detail),
        )
        conn.commit()


def log_telemetry(component: str, metric: str, value: float) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO telemetry_events (ts, component, metric, value) VALUES (?, ?, ?, ?)",
            (_now(), component, metric, value),
        )
        conn.commit()


def log_mcp_event(server: str, event: str, detail: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO mcp_events (ts, server, event, detail) VALUES (?, ?, ?, ?)",
            (_now(), server, event, detail),
        )
        conn.commit()


def log_audit(actor: str, action: str, detail: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
            (_now(), actor, action, detail),
        )
        conn.commit()


def record_config_version(version: int, actor: str, summary: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO config_history (ts, version, actor, summary) VALUES (?, ?, ?, ?)",
            (_now(), version, actor, summary),
        )
        conn.commit()


def list_config_history(limit: int = 20) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM config_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# ------------------------------------------------------------ users -------

def add_allowed_user(telegram_id: int, name: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO allowed_users (telegram_id, name, added_at) VALUES (?, ?, ?)",
            (telegram_id, name, _now()),
        )
        conn.commit()


def remove_allowed_user(telegram_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM allowed_users WHERE telegram_id=?", (telegram_id,))
        conn.commit()


def list_allowed_users() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM allowed_users ORDER BY added_at").fetchall()


# --------------------------------------------------------------- pairing --
# See bot/pairing.py for the rate-limiting/expiry constants and policy —
# this layer is pure storage.

def create_pairing_code(
    instance_id: int, code: str, user_id: str, user_name: str, chat_id: str, expires_at: str
) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO pairing_codes (instance_id, code, user_id, user_name, chat_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (instance_id, code, str(user_id), user_name or "", str(chat_id), _now(), expires_at),
        )
        conn.commit()
        return cur.lastrowid


def get_pairing_code(code: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM pairing_codes WHERE code=?", (code,)).fetchone()


def get_pairing_code_by_id(pairing_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM pairing_codes WHERE id=?", (pairing_id,)).fetchone()


def count_recent_pairing_requests(instance_id: int, user_id: str, since_iso: str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pairing_codes WHERE instance_id=? AND user_id=? AND created_at>?",
        (instance_id, str(user_id), since_iso),
    ).fetchone()
    return row["n"] if row else 0


def count_pending_pairing_codes(instance_id: int, user_id: Optional[str] = None) -> int:
    conn = get_conn()
    clauses = ["instance_id=?", "approved_at IS NULL", "denied_at IS NULL", "expires_at>?"]
    params: list[Any] = [instance_id, _now()]
    if user_id is not None:
        clauses.append("user_id=?")
        params.append(str(user_id))
    row = conn.execute(f"SELECT COUNT(*) AS n FROM pairing_codes WHERE {' AND '.join(clauses)}", params).fetchone()
    return row["n"] if row else 0


def list_pending_pairing_codes(instance_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if instance_id is not None:
        return conn.execute(
            "SELECT * FROM pairing_codes WHERE instance_id=? AND approved_at IS NULL AND denied_at IS NULL "
            "AND expires_at>? ORDER BY created_at DESC",
            (instance_id, _now()),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM pairing_codes WHERE approved_at IS NULL AND denied_at IS NULL AND expires_at>? "
        "ORDER BY created_at DESC",
        (_now(),),
    ).fetchall()


def approve_pairing_code(pairing_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE pairing_codes SET approved_at=? WHERE id=?", (_now(), pairing_id))
        conn.commit()


def deny_pairing_code(pairing_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE pairing_codes SET denied_at=? WHERE id=?", (_now(), pairing_id))
        conn.commit()


# ------------------------------------------------- support bot training ---
# User-added phrases layered on top of training_data.py's hand-authored
# EXAMPLES — see bot/support_bot/model.py's load_examples()/reload().
def list_support_bot_phrases() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM support_bot_phrases ORDER BY intent, id").fetchall()


def add_support_bot_phrase(phrase: str, intent: str) -> int:
    phrase = (phrase or "").strip()
    intent = (intent or "").strip()
    if not phrase or not intent:
        raise ValueError("both phrase and intent are required")
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO support_bot_phrases (phrase, intent, created_at) VALUES (?, ?, ?)",
            (phrase, intent, _now()),
        )
        conn.commit()
        return cur.lastrowid


def delete_support_bot_phrase(phrase_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM support_bot_phrases WHERE id=?", (phrase_id,))
        conn.commit()


def log_support_bot_classification(
    text: str,
    tfidf_intent: str,
    tfidf_confidence: float,
    nn_intent: str,
    nn_confidence: float,
    final_intent: str,
    final_confidence: float,
    source: str,
    agreed: bool,
) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO support_bot_classifications "
            "(ts, text, tfidf_intent, tfidf_confidence, nn_intent, nn_confidence, final_intent, final_confidence, source, agreed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), text, tfidf_intent, tfidf_confidence, nn_intent, nn_confidence, final_intent, final_confidence, source, 1 if agreed else 0),
        )
        conn.commit()


def get_support_bot_classification_stats(limit: int = 500) -> dict[str, Any]:
    """Self-monitoring summary for the Training tab — computed from the
    last `limit` real classifications, not a static guess. Empty/zeroed
    fields if nothing's been classified yet."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM support_bot_classifications ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    total = len(rows)
    if total == 0:
        return {
            "total": 0, "agreement_rate": 0.0, "unknown_rate": 0.0,
            "avg_tfidf_confidence": 0.0, "avg_nn_confidence": 0.0, "avg_final_confidence": 0.0,
            "source_counts": {}, "recent": [],
        }
    agreed = sum(1 for r in rows if r["agreed"])
    unknown = sum(1 for r in rows if r["final_intent"] == "unknown")
    source_counts: dict[str, int] = {}
    for r in rows:
        source_counts[r["source"]] = source_counts.get(r["source"], 0) + 1
    return {
        "total": total,
        "agreement_rate": round(agreed / total, 3),
        "unknown_rate": round(unknown / total, 3),
        "avg_tfidf_confidence": round(sum(r["tfidf_confidence"] for r in rows) / total, 3),
        "avg_nn_confidence": round(sum(r["nn_confidence"] for r in rows) / total, 3),
        "avg_final_confidence": round(sum(r["final_confidence"] for r in rows) / total, 3),
        "source_counts": source_counts,
        "recent": [dict(r) for r in rows[:15]],
    }


# ---------------------------------------------------------- api keys ------
# Mobile/device credentials — see SCHEMA's api_keys comment. Plaintext only
# ever exists in create_api_key()'s return value; every other accessor sees
# hashes/metadata only.

def create_api_key(label: str, kind: str = "device") -> tuple[int, str]:
    plaintext = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO api_keys (label, key_hash, created_at, kind) VALUES (?, ?, ?, ?)",
            (label, key_hash, _now(), kind),
        )
        conn.commit()
        return cur.lastrowid, plaintext


def list_api_keys(kind: Optional[str] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if kind is not None:
        return conn.execute(
            "SELECT id, label, created_at, last_used_at, revoked_at, kind FROM api_keys WHERE kind=? ORDER BY created_at DESC",
            (kind,),
        ).fetchall()
    return conn.execute("SELECT id, label, created_at, last_used_at, revoked_at, kind FROM api_keys ORDER BY created_at DESC").fetchall()


def update_api_key_label(key_id: int, label: str) -> None:
    """Renames a paired device's label — the dashboard's Paired Devices
    "Save Devices List Data" action, so a device auto-paired under a generic
    name (or a stale manually-typed one) can be corrected in place instead
    of revoking and re-pairing just to fix a name."""
    label = (label or "").strip()
    if not label:
        raise ValueError("label can't be empty")
    conn = get_conn()
    with _lock:
        cur = conn.execute("UPDATE api_keys SET label=? WHERE id=?", (label, key_id))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"api key {key_id} not found")


def revoke_api_key(key_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (_now(), key_id))
        conn.execute("DELETE FROM push_tokens WHERE api_key_id=?", (key_id,))
        conn.execute("DELETE FROM device_presence WHERE api_key_id=?", (key_id,))
        conn.commit()


def get_api_key(key_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT id, label, created_at, last_used_at, revoked_at, kind FROM api_keys WHERE id=?", (key_id,)
    ).fetchone()


# ------------------------------------------------------- peer servers -----
# Other BotServer installations this one is linked to — see SCHEMA's
# peer_servers comment and bot/peers.py for the linking handshake.

def create_peer_server(name: str, base_url: str, outbound_api_key: str, inbound_api_key_id: int) -> int:
    """Upserts by base_url when it's non-empty: re-linking the same address
    (the admin re-runs the link form after a restart, a DB reset on one
    side, or just clicking it twice) replaces the stale row in place —
    revoking the credential it's replacing — instead of accumulating
    duplicate rows with one dangling, never-used api_keys credential each
    time. An empty base_url (a peer that can't call us back) can't be
    deduped this way and always inserts a fresh row."""
    conn = get_conn()
    with _lock:
        existing = None
        if base_url:
            existing = conn.execute(
                "SELECT id, inbound_api_key_id FROM peer_servers WHERE base_url=?", (base_url,)
            ).fetchone()
        if existing is not None:
            if existing["inbound_api_key_id"] != inbound_api_key_id:
                conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (_now(), existing["inbound_api_key_id"]))
            conn.execute(
                "UPDATE peer_servers SET name=?, outbound_api_key=?, inbound_api_key_id=?, "
                "linked_at=?, last_seen_at=NULL, last_error=NULL WHERE id=?",
                (name, outbound_api_key, inbound_api_key_id, _now(), existing["id"]),
            )
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO peer_servers (name, base_url, outbound_api_key, inbound_api_key_id, linked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, base_url, outbound_api_key, inbound_api_key_id, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_peer_servers() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM peer_servers ORDER BY linked_at DESC").fetchall()


def get_peer_server(peer_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM peer_servers WHERE id=?", (peer_id,)).fetchone()


def mark_peer_server_ok(peer_id: int) -> None:
    """Called after every successful proxied call to a peer (see
    bot/peers.py) so the dashboard can show whether a linked server is
    actually reachable right now without a separate polling loop."""
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE peer_servers SET last_seen_at=?, last_error=NULL WHERE id=?", (_now(), peer_id))
        conn.commit()


def mark_peer_server_error(peer_id: int, error: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE peer_servers SET last_error=? WHERE id=?", (error, peer_id))
        conn.commit()


def delete_peer_server(peer_id: int) -> Optional[sqlite3.Row]:
    """Unlinks a peer: revokes the credential it used to call us (so it
    can't reach us again with the old key) and removes our record of it.
    Returns the deleted row (the caller may still want its outbound_api_key
    to attempt a courtesy unlink call to the peer itself) or None if it
    didn't exist."""
    conn = get_conn()
    with _lock:
        row = conn.execute("SELECT * FROM peer_servers WHERE id=?", (peer_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (_now(), row["inbound_api_key_id"]))
        conn.execute("DELETE FROM peer_servers WHERE id=?", (peer_id,))
        conn.commit()
        return row


# ---------------------------------------------------- server pairing tokens
# See server_pairing_tokens' SCHEMA comment and bot/peers.py: the one
# secret that actually crosses the network to link two servers, instead of
# the real DASHBOARD_TOKEN. Plaintext only ever exists in
# create_server_pairing_token()'s return value, same discipline as
# create_api_key().

def create_server_pairing_token(ttl_s: int = 600) -> tuple[str, str]:
    """Mints a fresh pairing token, invalidating whatever was still pending
    — only one is ever meant to be outstanding at a time, so an admin who
    generates a new one doesn't have to remember to separately revoke the
    old one too. Returns (plaintext, expires_at)."""
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    now = _now()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat(timespec="seconds")
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM server_pairing_tokens WHERE used_at IS NULL")
        conn.execute(
            "INSERT INTO server_pairing_tokens (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (token_hash, now, expires_at),
        )
        conn.commit()
    return plaintext, expires_at


def consume_server_pairing_token(plaintext: str) -> bool:
    """Validates a pairing token and marks it used in one atomic step —
    it's either accepted exactly once or not at all, closing the window a
    separate check-then-use would leave for a replay. Also opportunistically
    clears out old used/expired rows so the table never grows unbounded."""
    if not plaintext:
        return False
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT id, expires_at FROM server_pairing_tokens WHERE token_hash=? AND used_at IS NULL",
            (token_hash,),
        ).fetchone()
        if row is None:
            return False
        if datetime.fromisoformat(row["expires_at"]) < now_dt:
            return False
        cur = conn.execute(
            "UPDATE server_pairing_tokens SET used_at=? WHERE id=? AND used_at IS NULL", (now, row["id"])
        )
        conn.execute(
            "DELETE FROM server_pairing_tokens WHERE used_at IS NOT NULL OR expires_at < ?", (now,)
        )
        conn.commit()
        return cur.rowcount == 1


def prune_old_data(days: int) -> dict[str, int]:
    """Deletes rows older than `days` from the highest-volume,
    lowest-long-term-value tables — see config/backends.yaml's `retention`
    comment for the reasoning and bot/retention.py for the daily
    background task that calls this. Deliberately narrow: audit_log (a
    security trail), chat/session history, and config_history are never
    touched here — this is only for tables that exist to answer "what
    happened recently," not "what happened ever." `jobs` additionally
    excludes anything not yet in a terminal state, so a long-running or
    stuck job can never be deleted out from under itself regardless of
    its age. Returns {table: rows_deleted}."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = get_conn()
    removed: dict[str, int] = {}
    with _lock:
        cur = conn.execute(
            "DELETE FROM jobs WHERE created_at<? AND status NOT IN ('queued','running','retrying')",
            (cutoff,),
        )
        removed["jobs"] = cur.rowcount
        cur = conn.execute("DELETE FROM telemetry_events WHERE ts<?", (cutoff,))
        removed["telemetry_events"] = cur.rowcount
        cur = conn.execute("DELETE FROM connections_log WHERE ts<?", (cutoff,))
        removed["connections_log"] = cur.rowcount
        cur = conn.execute("DELETE FROM support_bot_classifications WHERE ts<?", (cutoff,))
        removed["support_bot_classifications"] = cur.rowcount
        conn.commit()
    return removed


def purge_revoked_keys() -> int:
    """Permanently deletes every already-revoked api_keys row — the
    dashboard's "Clear Revoked Devices" action. Revoking already tears down
    a device's presence/push rows immediately (see revoke_api_key above);
    this just removes the now-inert row itself so the Paired Devices list
    doesn't accumulate dead entries forever. Returns the number removed."""
    conn = get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM api_keys WHERE revoked_at IS NOT NULL")
        conn.commit()
        return cur.rowcount


def list_devices() -> list[sqlite3.Row]:
    """Paired, unrevoked devices with their live presence, if any — used by
    /api/devices and the WebSocket broadcaster. "Online" is left for the
    caller to compute from last_seen, so both can share the exact same
    freshness window without this layer hardcoding one."""
    conn = get_conn()
    return conn.execute(
        "SELECT ak.id, ak.label, ak.created_at, ak.last_used_at, "
        "dp.platform, dp.app_version, dp.device_model, dp.os_version, dp.last_seen "
        "FROM api_keys ak LEFT JOIN device_presence dp ON dp.api_key_id = ak.id "
        "WHERE ak.revoked_at IS NULL AND ak.kind='device' ORDER BY ak.created_at DESC"
    ).fetchall()


def upsert_push_token(api_key_id: int, fcm_token: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO push_tokens (api_key_id, fcm_token, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(fcm_token) DO UPDATE SET api_key_id=excluded.api_key_id, updated_at=excluded.updated_at",
            (api_key_id, fcm_token, _now()),
        )
        conn.commit()


def list_push_tokens() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT pt.* FROM push_tokens pt JOIN api_keys ak ON ak.id = pt.api_key_id WHERE ak.revoked_at IS NULL"
    ).fetchall()


def create_apk_push(
    api_key_id: int,
    apk_path: str,
    version_label: Optional[str] = None,
    origin_api_key_id: Optional[int] = None,
    mesh_token: Optional[str] = None,
) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO apk_pushes (api_key_id, apk_path, version_label, created_at, origin_api_key_id, mesh_token) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (api_key_id, apk_path, version_label, _now(), origin_api_key_id, mesh_token),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_apk_push(api_key_id: int) -> Optional[sqlite3.Row]:
    """The newest not-yet-downloaded push for this device, or None. Only
    the newest matters — an older undownloaded push for the same device is
    superseded, not queued behind it."""
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM apk_pushes WHERE api_key_id=? AND downloaded_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (api_key_id,),
    ).fetchone()


def get_apk_push(push_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM apk_pushes WHERE id=?", (push_id,)).fetchone()


def mark_apk_push_downloaded(push_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE apk_pushes SET downloaded_at=? WHERE id=?", (_now(), push_id))
        conn.commit()


def get_device_presence(api_key_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM device_presence WHERE api_key_id=?", (api_key_id,)).fetchone()


def redeem_mesh_token(push_id: int, token: str) -> bool:
    """Single-use check for a mesh transfer: the token must match the one
    minted for this exact push and not have been redeemed before. Called by
    the *origin* device's own mesh listener (via the server, since that's
    the only party who knows what token it handed out) before it starts
    streaming its APK to whoever presents the token — without this, any
    device that guessed or replayed a push_id could pull another device's
    installed APK indefinitely."""
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT mesh_token, mesh_token_used_at FROM apk_pushes WHERE id=?", (push_id,)
        ).fetchone()
        if row is None or not row["mesh_token"] or row["mesh_token"] != token or row["mesh_token_used_at"]:
            return False
        now = _now()
        # Redemption *is* the transfer starting — there's no separate
        # "download complete" signal in pure P2P mode (this server never
        # sees the bytes), so downloaded_at is stamped here too, same as
        # mark_apk_push_downloaded() does for the server-relay path.
        conn.execute(
            "UPDATE apk_pushes SET mesh_token_used_at=?, downloaded_at=? WHERE id=?",
            (now, now, push_id),
        )
        conn.commit()
        return True


SERVER_CHAT_DESKTOP_DEVICE_ID = 0


def device_label(device_id: int) -> str:
    if device_id == SERVER_CHAT_DESKTOP_DEVICE_ID:
        return "Desktop"
    row = get_conn().execute(
        "SELECT label, revoked_at FROM api_keys WHERE id=?", (device_id,)
    ).fetchone()
    if row is None:
        return f"device {device_id}"
    return row["label"] + (" (revoked)" if row["revoked_at"] else "")


def ensure_server_chat_group() -> int:
    """The single permanent "Server Chat" room every device sees — created
    once, reused forever after."""
    conn = get_conn()
    with _lock:
        row = conn.execute("SELECT id FROM server_chat_conversations WHERE kind='group' LIMIT 1").fetchone()
        if row is not None:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO server_chat_conversations (kind, participant_a, participant_b, created_at) "
            "VALUES ('group', NULL, NULL, ?)",
            (_now(),),
        )
        conn.commit()
        return cur.lastrowid


def ensure_direct_conversation(device_a: int, device_b: int) -> int:
    """One row per unordered device pair — participant_a is always the
    smaller id so (3, 7) and (7, 3) resolve to the same conversation."""
    lo, hi = (device_a, device_b) if device_a <= device_b else (device_b, device_a)
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT id FROM server_chat_conversations WHERE kind='direct' AND participant_a=? AND participant_b=?",
            (lo, hi),
        ).fetchone()
        if row is not None:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO server_chat_conversations (kind, participant_a, participant_b, created_at) "
            "VALUES ('direct', ?, ?, ?)",
            (lo, hi, _now()),
        )
        conn.commit()
        return cur.lastrowid


def backfill_server_chat_conversations() -> None:
    """Fills in direct conversations for devices paired before Server Chat
    existed — a full mesh across desktop + every non-revoked device.
    ensure_direct_conversation() is idempotent, so this is safe to call on
    every startup, not just once."""
    keys = [r["id"] for r in list_api_keys(kind="device") if not r["revoked_at"]]
    devices = [SERVER_CHAT_DESKTOP_DEVICE_ID] + keys
    for i, a in enumerate(devices):
        for b in devices[i + 1:]:
            ensure_direct_conversation(a, b)


def create_conversations_for_new_device(device_id: int) -> None:
    """Called right after a new paired device's key is created — opens a
    direct conversation between it and the desktop app, and between it and
    every other already-paired (non-revoked) device, so every device can
    reach every other device from the moment it's added without anyone
    having to manually start a chat."""
    ensure_direct_conversation(device_id, SERVER_CHAT_DESKTOP_DEVICE_ID)
    for row in list_api_keys(kind="device"):
        if row["id"] != device_id and not row["revoked_at"]:
            ensure_direct_conversation(device_id, row["id"])


def is_conversation_participant(conversation_id: int, device_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT kind, participant_a, participant_b FROM server_chat_conversations WHERE id=?", (conversation_id,)).fetchone()
    if row is None:
        return False
    if row["kind"] == "group":
        return True
    return device_id in (row["participant_a"], row["participant_b"])


def list_server_chat_conversations(device_id: int) -> list[dict]:
    """Every conversation `device_id` can see: the group room, plus every
    direct conversation it's a participant of. Each is annotated with a
    display title (the room name, or the other device's current label)
    and its own last-message preview so a device list can render without
    N+1 follow-up requests."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM server_chat_conversations WHERE kind='group' OR participant_a=? OR participant_b=? "
        "ORDER BY id",
        (device_id, device_id),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d["kind"] == "group":
            d["title"] = "Server Chat"
            d["peer_device_id"] = None
        else:
            peer_id = d["participant_b"] if d["participant_a"] == device_id else d["participant_a"]
            d["title"] = device_label(peer_id)
            d["peer_device_id"] = peer_id
        last = conn.execute(
            "SELECT text, ts, attachment_name FROM server_chat_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
            (d["id"],),
        ).fetchone()
        d["last_message"] = dict(last) if last else None
        out.append(d)
    return out


def create_server_chat_message(
    conversation_id: int,
    sender_device_id: int,
    text: str,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
    attachment_mime: Optional[str] = None,
    attachment_size: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO server_chat_messages "
            "(conversation_id, sender_device_id, ts, text, attachment_path, attachment_name, "
            "attachment_mime, attachment_size, thumbnail_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, sender_device_id, _now(), text, attachment_path, attachment_name,
             attachment_mime, attachment_size, thumbnail_path),
        )
        conn.commit()
        return cur.lastrowid


def list_server_chat_messages(conversation_id: int, after_id: int = 0, limit: int = 100) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM server_chat_messages WHERE conversation_id=? AND id > ? ORDER BY id LIMIT ?",
        (conversation_id, after_id, limit),
    ).fetchall()


def get_server_chat_message(message_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM server_chat_messages WHERE id=?", (message_id,)).fetchone()


def verify_api_key(
    plaintext: str,
    platform: Optional[str] = None,
    app_version: Optional[str] = None,
    device_model: Optional[str] = None,
    os_version: Optional[str] = None,
    local_ip: Optional[str] = None,
    mesh_port: Optional[int] = None,
) -> Optional[int]:
    """Also upserts device_presence on every successful call — piggybacking
    presence tracking on the auth check every mobile request already makes,
    rather than a separate heartbeat endpoint. All device fields are
    optional (COALESCE keeps whatever was last known if this particular
    caller didn't send them) since not every route bothers threading device
    headers through. `device_model`/`os_version` are the real hardware model
    and OS release (e.g. "Pixel 8 Pro" / "Android 14") — distinct from the
    user-typed pairing label, so devices are identifiable even when someone
    left the label as something generic. `local_ip` is the caller's address
    as this server itself observed it (request.client.host) — only
    meaningful when the caller is actually on the same LAN as the server,
    which is exactly the case the mesh transfer feature needs: it's used to
    let a *different* device dial this one directly instead of relaying
    through the server. `mesh_port` is which local TCP port this device's
    own mesh listener (if any) is currently bound to, self-reported since
    the server has no other way to know it."""
    if not plaintext:
        return None
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (key_hash,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (_now(), row["id"]))
        conn.execute(
            "INSERT INTO device_presence "
            "(api_key_id, platform, app_version, device_model, os_version, local_ip, mesh_port, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(api_key_id) DO UPDATE SET "
            "platform=COALESCE(excluded.platform, device_presence.platform), "
            "app_version=COALESCE(excluded.app_version, device_presence.app_version), "
            "device_model=COALESCE(excluded.device_model, device_presence.device_model), "
            "os_version=COALESCE(excluded.os_version, device_presence.os_version), "
            "local_ip=COALESCE(excluded.local_ip, device_presence.local_ip), "
            "mesh_port=excluded.mesh_port, "
            "last_seen=excluded.last_seen",
            (row["id"], platform, app_version, device_model, os_version, local_ip, mesh_port, _now()),
        )
        conn.commit()
        return row["id"]


# -------------------------------------------------------------- chat ------

def log_message(
    chat_id: Any,
    direction: str,
    source: str,
    text: str,
    platform: str = "telegram",
    user_id: Optional[Any] = None,
    username: str = "",
    instance_id: Optional[int] = None,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
    attachment_mime: Optional[str] = None,
    attachment_size: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
) -> int:
    conn = get_conn()
    with _lock:
        session_id = _get_or_create_session(conn, instance_id, chat_id, text)
        cur = conn.execute(
            "INSERT INTO messages (ts, platform, chat_id, user_id, username, direction, source, text, instance_id, "
            "attachment_path, attachment_name, attachment_mime, attachment_size, thumbnail_path, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), platform, str(chat_id), str(user_id) if user_id is not None else None, username, direction, source, text, instance_id,
             attachment_path, attachment_name, attachment_mime, attachment_size, thumbnail_path, session_id),
        )
        conn.commit()
        return cur.lastrowid


def get_message(message_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()


def list_messages(
    limit: int = 100,
    platform: Optional[str] = None,
    chat_id: Optional[Any] = None,
    after_id: Optional[int] = None,
    instance_id: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Oldest-first, capped to the most recent `limit` — the natural order
    for a chat view (append at the bottom). `after_id` supports incremental
    polling: only rows newer than the last one the caller already has."""
    conn = get_conn()
    clauses = []
    params: list[Any] = []
    if platform is not None:
        clauses.append("platform=?")
        params.append(platform)
    if chat_id is not None:
        clauses.append("chat_id=?")
        params.append(str(chat_id))
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
    if after_id is not None:
        clauses.append("id>?")
        params.append(after_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if after_id is not None:
        # incremental: everything newer, oldest-first, no need to cap+reverse
        params.append(limit)
        return conn.execute(
            f"SELECT * FROM messages {where} ORDER BY id ASC LIMIT ?", params
        ).fetchall()
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM messages {where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    return list(reversed(rows))


# --------------------------------------------------------- dashboard KPIs -

def get_overview() -> dict[str, Any]:
    conn = get_conn()
    running = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='running'").fetchone()["c"]
    queued = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='queued'").fetchone()["c"]
    completed_today = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='success' AND date(created_at)=date('now')"
    ).fetchone()["c"]
    failed_today = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='failed' AND date(created_at)=date('now')"
    ).fetchone()["c"]
    week = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs WHERE created_at >= datetime('now','-7 days') "
        "AND status IN ('success','failed') GROUP BY status"
    ).fetchall()
    succ = sum(r["c"] for r in week if r["status"] == "success")
    fail = sum(r["c"] for r in week if r["status"] == "failed")
    success_rate = round(100 * succ / (succ + fail), 1) if (succ + fail) else 100.0
    avg_dur = conn.execute(
        "SELECT AVG(duration_ms) a FROM jobs WHERE status='success' "
        "AND created_at >= datetime('now','-1 day')"
    ).fetchone()["a"]
    tokens_today = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) t FROM jobs WHERE date(created_at)=date('now')"
    ).fetchone()["t"]
    return {
        "jobs_running": running,
        "jobs_queued": queued,
        "completed_today": completed_today,
        "failed_today": failed_today,
        "success_rate_7d": success_rate,
        "avg_duration_ms": round(avg_dur) if avg_dur else 0,
        "tokens_today": tokens_today,
    }


def get_usage_summary(instance_id: int) -> dict[str, Any]:
    """Per-instance token/job usage for /usage — get_overview() above is
    global, this is the one bot's own numbers."""
    conn = get_conn()
    row_today = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) tok, COUNT(*) n FROM jobs WHERE instance_id=? AND date(created_at)=date('now')",
        (instance_id,),
    ).fetchone()
    row_total = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) tok, COUNT(*) n FROM jobs WHERE instance_id=?", (instance_id,)
    ).fetchone()
    return {
        "tokens_today": row_today["tok"],
        "jobs_today": row_today["n"],
        "tokens_total": row_total["tok"],
        "jobs_total": row_total["n"],
    }


def get_insights(instance_id: int, days: int = 7) -> dict[str, Any]:
    """Daily job counts/success-rate/tokens for /insights [days]."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT date(created_at) d, status, COUNT(*) n, COALESCE(SUM(tokens),0) tok "
        "FROM jobs WHERE instance_id=? AND created_at >= datetime('now', ?) GROUP BY d, status ORDER BY d",
        (instance_id, f"-{days} days"),
    ).fetchall()
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = by_day.setdefault(r["d"], {"success": 0, "failed": 0, "other": 0, "tokens": 0})
        entry["tokens"] += r["tok"]
        if r["status"] == "success":
            entry["success"] = r["n"]
        elif r["status"] == "failed":
            entry["failed"] = r["n"]
        else:
            entry["other"] += r["n"]
    messages_row = conn.execute(
        "SELECT COUNT(*) n FROM messages WHERE instance_id=? AND direction='in' AND ts >= datetime('now', ?)",
        (instance_id, f"-{days} days"),
    ).fetchone()
    return {"days": days, "by_day": by_day, "messages_in": messages_row["n"]}


def get_jobs_timeseries_24h() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:00', created_at) hour, status, COUNT(*) c "
        "FROM jobs WHERE created_at >= datetime('now','-24 hours') "
        "GROUP BY hour, status ORDER BY hour"
    ).fetchall()
    buckets: dict[str, dict[str, int]] = {}
    for r in rows:
        buckets.setdefault(r["hour"], {"completed": 0, "failed": 0})
        if r["status"] == "success":
            buckets[r["hour"]]["completed"] += r["c"]
        elif r["status"] == "failed":
            buckets[r["hour"]]["failed"] += r["c"]
    return [{"hour": h, **v} for h, v in sorted(buckets.items())]


def get_jobs_by_backend_today() -> dict[str, int]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT backend, COUNT(*) c FROM jobs WHERE date(created_at)=date('now') "
        "GROUP BY backend"
    ).fetchall()
    return {r["backend"]: r["c"] for r in rows}


def get_latency_by_backend() -> dict[str, dict[str, float]]:
    conn = get_conn()
    out: dict[str, dict[str, float]] = {}
    for backend in ("api", "cli", "ui"):
        rows = [
            r["value"]
            for r in conn.execute(
                "SELECT value FROM telemetry_events WHERE component=? AND metric='latency_ms' "
                "AND ts >= datetime('now','-6 hours') ORDER BY value",
                (backend,),
            ).fetchall()
        ]
        if rows:
            p50 = rows[len(rows) // 2]
            p95 = rows[min(len(rows) - 1, int(len(rows) * 0.95))]
            out[backend] = {"p50_ms": p50, "p95_ms": p95}
        else:
            out[backend] = {"p50_ms": 0, "p95_ms": 0}
    return out


def get_table_counts() -> dict[str, int]:
    conn = get_conn()
    tables = ["jobs", "connections_log", "telemetry_events", "mcp_events", "audit_log", "messages", "bot_instances", "swarms", "swarm_runs"]
    return {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables
    }


def get_db_size_bytes() -> int:
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            size += p.stat().st_size
    return size


# Same whitelist as get_table_counts() above — kept separate because this
# one gates which table names api_export() will ever interpolate into SQL.
EXPORTABLE_TABLES = [
    "jobs", "connections_log", "telemetry_events", "mcp_events", "audit_log",
    "messages", "bot_instances", "swarms", "swarm_runs",
]


def export_table(table: str) -> list[dict]:
    if table not in EXPORTABLE_TABLES:
        raise ValueError(f"unknown or non-exportable table: {table}")
    conn = get_conn()
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


def get_recent_connection_events(limit: int = 20) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM connections_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def vacuum(actor: str = "dashboard", detail: str = "manual VACUUM triggered") -> None:
    conn = get_conn()
    with _lock:
        conn.execute("VACUUM;")
        conn.commit()
    log_audit(actor=actor, action="vacuum", detail=detail)


def days_since_last_vacuum() -> Optional[float]:
    """None if a VACUUM has never been logged (manual or automatic) —
    callers should treat that as "due", not "recent"."""
    conn = get_conn()
    row = conn.execute(
        "SELECT ts FROM audit_log WHERE action='vacuum' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    last = datetime.fromisoformat(row["ts"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 86400


# -------------------------------------------------------------- swarms ----
# Thin SQL wrappers only — `config`/`steps` are passed through as raw JSON
# text, same as jobs.prompt/messages.text; encoding/decoding is the
# caller's job (bot/swarm/engine.py, dashboard/server.py), matching how
# this module stays a plain SQL layer everywhere else.

def create_swarm(name: str, strategy: str, config_json: str, enabled: bool = True) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO swarms (name, strategy, config, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, strategy, config_json, 1 if enabled else 0, _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid


def update_swarm(swarm_id: int, **fields: Any) -> None:
    columns, params = [], []
    for key in ("name", "strategy", "config", "enabled"):
        if key in fields:
            columns.append(f"{key}=?")
            params.append((1 if fields[key] else 0) if key == "enabled" else fields[key])
    if not columns:
        return
    columns.append("updated_at=?")
    params.append(_now())
    params.append(swarm_id)
    conn = get_conn()
    with _lock:
        conn.execute(f"UPDATE swarms SET {', '.join(columns)} WHERE id=?", params)
        conn.commit()


def delete_swarm(swarm_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM swarms WHERE id=?", (swarm_id,))
        conn.commit()


def get_swarm(swarm_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM swarms WHERE id=?", (swarm_id,)).fetchone()


def list_swarms() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM swarms ORDER BY id").fetchall()


def create_swarm_run(swarm_id: int, swarm_run_id: str, prompt: str, requested_by: str = "") -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO swarm_runs (swarm_id, swarm_run_id, status, prompt, requested_by, created_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (swarm_id, swarm_run_id, prompt, requested_by, _now()),
        )
        conn.commit()
        return cur.lastrowid


def update_swarm_run(
    swarm_run_id: str,
    status: Optional[str] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
    steps_json: Optional[str] = None,
    finished: bool = False,
) -> None:
    columns, params = [], []
    if status is not None:
        columns.append("status=?")
        params.append(status)
    if result is not None:
        columns.append("result=?")
        params.append(result)
    if error is not None:
        columns.append("error=?")
        params.append(error)
    if steps_json is not None:
        columns.append("steps=?")
        params.append(steps_json)
    conn = get_conn()
    with _lock:
        if finished:
            row = conn.execute("SELECT created_at FROM swarm_runs WHERE swarm_run_id=?", (swarm_run_id,)).fetchone()
            finished_at = _now()
            duration_ms = None
            if row and row["created_at"]:
                started = datetime.fromisoformat(row["created_at"])
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            columns += ["finished_at=?", "duration_ms=?"]
            params += [finished_at, duration_ms]
        if not columns:
            return
        params.append(swarm_run_id)
        conn.execute(f"UPDATE swarm_runs SET {', '.join(columns)} WHERE swarm_run_id=?", params)
        conn.commit()


def get_swarm_run(swarm_run_id: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM swarm_runs WHERE swarm_run_id=?", (swarm_run_id,)).fetchone()


def list_swarm_runs(swarm_id: Optional[int] = None, limit: int = 50) -> list[sqlite3.Row]:
    conn = get_conn()
    if swarm_id is not None:
        return conn.execute(
            "SELECT * FROM swarm_runs WHERE swarm_id=? ORDER BY id DESC LIMIT ?", (swarm_id, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM swarm_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
