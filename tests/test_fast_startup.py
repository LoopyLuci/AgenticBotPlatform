"""Start-up must not wait on anything slow: the neural net is not trained at import, and the dashboard is started before
MCP servers and chat platforms (the desktop window waits on the dashboard to finish loading)."""
from __future__ import annotations

import inspect
import re
import subprocess
import sys

from bot import main as bot_main
from bot.support_bot import hybrid, nn_model
from bot.support_bot.nn_model import NeuralIntentClassifier

EXAMPLES = [("show my bots", "list_bots"), ("list bots", "list_bots"), ("restart the server", "restart"),
            ("please restart", "restart"), ("what is the status", "status"), ("status please", "status")]


def test_a_deferred_classifier_is_untrained_until_it_is_needed():
    clf = NeuralIntentClassifier(defer=True)
    assert clf._mlp is None and clf._deferred is True


def test_it_trains_on_first_predict_and_only_once(monkeypatch):
    monkeypatch.setattr(nn_model, "_load_examples", lambda: list(EXAMPLES))
    trained = []
    real_train = NeuralIntentClassifier.train

    def counting_train(self, examples):
        trained.append(len(examples))
        return real_train(self, examples)

    monkeypatch.setattr(NeuralIntentClassifier, "train", counting_train)
    clf = NeuralIntentClassifier(defer=True)
    intent, _ = clf.predict("show my bots")
    assert intent == "list_bots"
    clf.predict("what is the status")
    assert trained == [len(EXAMPLES)]


def test_loading_a_saved_model_means_it_never_trains(monkeypatch):
    donor = NeuralIntentClassifier(EXAMPLES)
    clf = NeuralIntentClassifier(defer=True)
    monkeypatch.setattr(NeuralIntentClassifier, "train", lambda *a, **k: (_ for _ in ()).throw(AssertionError("trained")))
    clf.load_state(donor.export_state())
    assert clf.predict("show my bots")[0] == "list_bots"
    clf.ensure_trained()  # a no-op once loaded


def test_export_state_of_a_deferred_classifier_trains_it_first(monkeypatch):
    monkeypatch.setattr(nn_model, "_load_examples", lambda: list(EXAMPLES))
    state = NeuralIntentClassifier(defer=True).export_state()
    assert state["classes"] == sorted({i for _, i in EXAMPLES})


def test_a_lazily_trained_model_matches_an_eagerly_trained_one(monkeypatch):
    monkeypatch.setattr(nn_model, "_load_examples", lambda: list(EXAMPLES))
    eager = NeuralIntentClassifier(EXAMPLES)
    lazy = NeuralIntentClassifier(defer=True)
    for text in ("show my bots", "restart the server", "status please", "something unrelated entirely"):
        assert lazy.predict(text) == eager.predict(text)


def test_importing_the_support_bot_does_not_train_the_net():
    """Run in a fresh interpreter so this really is the import, not a warmed-up module."""
    code = (
        "import bot.support_bot.nn_model as m; "
        "print(m.nn_model._deferred, m.nn_model._mlp is None)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip().splitlines()[-1] == "True True"


def test_hybrid_still_classifies_after_warm_up():
    hybrid.warm_up()
    assert hybrid.classify("show me the status").intent != ""


def test_the_dashboard_starts_before_mcp_servers_and_chat_platforms():
    src = inspect.getsource(bot_main.run)
    order = {name: src.index(name) for name in (
        "build_app()", "dashboard_supervisor_task = asyncio.create_task",
        "mcp_client.connect_all_enabled()", "platform_supervisor.start_all_enabled",
    )}
    assert order["build_app()"] < order["dashboard_supervisor_task = asyncio.create_task"]
    assert order["dashboard_supervisor_task = asyncio.create_task"] < order["mcp_client.connect_all_enabled()"]
    assert order["mcp_client.connect_all_enabled()"] < order["platform_supervisor.start_all_enabled"]
    assert re.search(r"support_warmup_task = asyncio\.create_task", src)
