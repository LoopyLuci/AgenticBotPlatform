"""bot/bot_instances.py's per-write backup pruning — regression coverage
for a real bug found live: BACKUP_DIR was never test-isolated (only the
DB path was), so every test creating/updating/deleting a bot instance
leaked a real JSON snapshot into the shared data/bot_instances_backups/
directory forever. That grew to 47,000+ files over this project's
history, and — since the dashboard's Bots-tab backups table has no row
cap — turned into a ~47,000-row table that made the whole dashboard
sluggish to resize/scroll. Fixed two ways: tests/conftest.py's temp_db
fixture now redirects BACKUP_DIR into tmp_path, and backup_instances()
itself now prunes to a configured max count on every write, so the
directory can never grow unbounded again regardless of call volume.
"""
from __future__ import annotations

from bot import bot_instances


def _make_instance(name="worker"):
    return bot_instances.create_instance(
        name=name, platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


def test_backup_dir_is_isolated_from_the_real_data_directory(temp_db, tmp_path):
    _make_instance()
    assert bot_instances.BACKUP_DIR == tmp_path / "bot_instances_backups"
    assert list(bot_instances.BACKUP_DIR.glob("instances-*.json"))


def test_backups_are_pruned_to_the_configured_max_count(temp_db):
    for i in range(12):
        bot_instances.backup_instances(reason=f"manual {i}")
    remaining = list(bot_instances.BACKUP_DIR.glob("instances-*.json"))
    assert len(remaining) == 12  # under the default cap (50) — nothing pruned yet

    removed = bot_instances._prune_old_backups(keep=5)
    remaining = list(bot_instances.BACKUP_DIR.glob("instances-*.json"))
    assert removed == 7
    assert len(remaining) == 5


def test_backup_instances_never_exceeds_the_cap_regardless_of_call_volume(temp_db, monkeypatch):
    from bot.config import config

    # config.current returns a deep copy on every access (see bot/config.py) —
    # patch the manager's real backing dict directly, not a throwaway copy.
    monkeypatch.setitem(config._data.setdefault("retention", {}), "bot_instances_backups_max_count", 3)
    iid = _make_instance()
    for i in range(10):
        bot_instances.update_instance(iid, actor="test", name=f"worker-{i}")
    remaining = list(bot_instances.BACKUP_DIR.glob("instances-*.json"))
    assert len(remaining) <= 3


def test_prune_keeps_the_most_recent_files(temp_db):
    import time

    paths = []
    for i in range(5):
        paths.append(bot_instances.backup_instances(reason=f"manual {i}"))
        time.sleep(0.01)  # ensure distinct mtimes on fast filesystems

    bot_instances._prune_old_backups(keep=2)
    remaining = {p.name for p in bot_instances.BACKUP_DIR.glob("instances-*.json")}
    assert remaining == {paths[-1].name, paths[-2].name}
