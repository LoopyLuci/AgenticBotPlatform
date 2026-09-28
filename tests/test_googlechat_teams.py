"""Google Chat and Microsoft Teams channels (bot/platforms/googlechat_platform.py, teams_platform.py, _jwt.py).

No real Google Workspace or Azure account is available here, so the providers are played by local stand-ins that do what
the documentation says they do, with real cryptography: RS256 tokens signed by a generated key and published as Google's
x509 certificate map and as the Bot Framework's JWKS (with endorsements), a Google token endpoint that verifies the service
account's signed assertion, an Entra token endpoint, and the Chat API / Bot Connector recording what the bot posts."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from bot import bot_instances, db, validators
from bot.platforms import _jwt, _relay, googlechat_platform as gc, teams_platform as teams

pytestmark = pytest.mark.usefixtures("temp_db")

APP_ID = "11111111-2222-3333-4444-555555555555"
TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ALICE_OID = "99999999-8888-7777-6666-555555555555"
SERVICE_URL = "https://smba.example.test/amer/"
PROJECT = "123456789012"


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


PROVIDER_KEY, OTHER_KEY, SA_KEY = _rsa(), _rsa(), _rsa()


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _cert(key) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


SERVICE_ACCOUNT = {"type": "service_account", "client_email": "abp-bot@proj.iam.gserviceaccount.com", "private_key_id": "sa1",
                   "private_key": _pem(SA_KEY), "token_uri": "https://oauth2.googleapis.com/token"}


class Provider:
    """Google's and Microsoft's servers, as far as the bot can see them."""

    def __init__(self):
        self.posts: list[tuple[str, dict, dict]] = []      # (url, headers, json body)
        self.token_requests: list[dict] = []
        self.kids = {"k1": PROVIDER_KEY}
        self.endorsements = ["msteams"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://www.googleapis.com/service_accounts/v1/metadata/x509/") or url == "https://www.googleapis.com/oauth2/v1/certs":
            return httpx.Response(200, json={kid: _cert(k) for kid, k in self.kids.items()})
        if url == "https://login.botframework.com/v1/.well-known/openidconfiguration":
            return httpx.Response(200, json={"issuer": teams.ISSUER, "jwks_uri": "https://login.botframework.com/v1/.well-known/keys"})
        if url == "https://login.botframework.com/v1/.well-known/keys":
            keys = []
            for kid, k in self.kids.items():
                jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key()))
                keys.append({**jwk, "kid": kid, "endorsements": self.endorsements})
            return httpx.Response(200, json={"keys": keys})
        if url == "https://oauth2.googleapis.com/token":
            form = parse_qs(request.content.decode())
            claims = jwt.decode(form["assertion"][0], SA_KEY.public_key(), algorithms=["RS256"], audience="https://oauth2.googleapis.com/token")
            assert claims["iss"] == SERVICE_ACCOUNT["client_email"] and claims["scope"] == gc.SCOPE
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            self.token_requests.append({"to": "google", **claims})
            return httpx.Response(200, json={"access_token": "google-access", "expires_in": 3600})
        if url.startswith("https://login.microsoftonline.com/"):
            form = parse_qs(request.content.decode())
            if form.get("client_secret") != ["right-secret"]:
                return httpx.Response(401, json={"error": "invalid_client"})
            self.token_requests.append({"to": url, "client_id": form["client_id"][0], "scope": form["scope"][0]})
            return httpx.Response(200, json={"access_token": "ms-access", "expires_in": 3600})
        if url.startswith("https://chat.googleapis.com/") or url.startswith(SERVICE_URL):
            self.posts.append((url, dict(request.headers), json.loads(request.content or b"{}")))
            return httpx.Response(200, json={"name": "ok", "id": "ok"})
        return httpx.Response(404)


@pytest.fixture
def provider(monkeypatch, tmp_path):
    p = Provider()
    transport = httpx.MockTransport(p.handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    monkeypatch.setattr(gc, "PROJECT_KEYS", _jwt.KeySet(gc.PROJECT_KEYS.url))
    monkeypatch.setattr(gc, "GOOGLE_ID_KEYS", _jwt.KeySet(gc.GOOGLE_ID_KEYS.url))
    monkeypatch.setattr(teams, "CONNECTOR_KEYS", _jwt.KeySet(teams.CONNECTOR_KEYS.url, discovery=True))
    gc._tokens.clear()
    teams._tokens.clear()
    monkeypatch.setattr(teams, "_store", lambda: tmp_path / "teams_conversations.json")

    class _Reply:
        def __init__(self, text):
            self.text = text

    async def fake_ask(text, **kw):
        return _Reply(f"**Answer** to: {text}")
    monkeypatch.setattr(_relay.router, "ask", fake_ask)
    return p


def token(claims: dict, *, key=PROVIDER_KEY, kid="k1", alg="RS256") -> str:
    now = int(time.time())
    return jwt.encode({"iat": now, "exp": now + 600, **claims}, _pem(key) if alg == "RS256" else "a-shared-secret-long-enough-for-hs256-ok", algorithm=alg,
                      headers={"kid": kid})


def run(coro):
    return asyncio.run(coro)


# ---- Google Chat ---------------------------------------------------------------------------------------------------
def chat_bot(audience=PROJECT, allowed=("alice@example.com",)) -> int:
    return bot_instances.create_instance(name=f"Chat {audience[-4:]}", platform="googlechat", backend="native_agent",
                                         credentials={"service_account_json": json.dumps(SERVICE_ACCOUNT), "audience": audience},
                                         allowed_user_ids=list(allowed))


def chat_event(text="@ABP what is up", arg=" what is up", email="alice@example.com", etype="MESSAGE", sender_type="HUMAN"):
    user = {"name": "users/42", "displayName": "Alice", "email": email, "type": sender_type}
    return {"type": etype, "eventTime": "2026-09-28T12:00:00Z", "user": user,
            "space": {"name": "spaces/AAA", "spaceType": "SPACE", "type": "ROOM"},
            "message": {"name": "spaces/AAA/messages/1", "sender": user, "text": text, "argumentText": arg,
                        "thread": {"name": "spaces/AAA/threads/T1"}, "space": {"name": "spaces/AAA"}}}


def project_token(**over):
    return "Bearer " + token({"iss": gc.CHAT_ISSUER, "aud": PROJECT, **over})


def test_a_verified_chat_message_is_answered_in_its_thread(provider):
    chat_bot()
    instance, event, why = run(gc.check(chat_event(), project_token()))
    assert why == "" and event["text"] == "what is up" and event["space"] == "spaces/AAA" and event["thread"] == "spaces/AAA/threads/T1"
    run(gc.deliver(instance, event))
    (url, headers, body), = provider.posts
    assert url == "https://chat.googleapis.com/v1/spaces/AAA/messages?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    assert headers["authorization"] == "Bearer google-access"
    assert body == {"text": "*Answer* to: what is up", "thread": {"name": "spaces/AAA/threads/T1"}}
    assert provider.token_requests and provider.token_requests[0]["to"] == "google"
    msgs = [dict(r) for r in db.get_conn().execute("SELECT direction, text FROM messages WHERE platform='googlechat'")]
    assert [m["direction"] for m in msgs] == ["in", "out"]


def test_the_service_account_token_is_reused_and_long_replies_are_split(provider):
    chat_bot()
    instance, _, _ = run(gc.check(chat_event(), project_token()))
    run(gc.send_text(instance, "spaces/AAA", "x" * 9000))
    run(gc.send_text(instance, "spaces/AAA", "again"))
    assert len(provider.posts) == 4 and len(provider.token_requests) == 1
    assert "messageReplyOption" not in provider.posts[0][0], "a message outside a thread starts one"


def test_the_endpoint_url_audience_takes_googles_id_token(provider):
    chat_bot(audience="https://bot.example.test/webhooks/googlechat")
    good = "Bearer " + token({"iss": "https://accounts.google.com", "aud": "https://bot.example.test/webhooks/googlechat",
                              "email": gc.CHAT_ISSUER, "email_verified": True})
    assert run(gc.check(chat_event(), good))[2] == ""
    someone_else = "Bearer " + token({"iss": "https://accounts.google.com", "aud": "https://bot.example.test/webhooks/googlechat",
                                      "email": "attacker@example.com", "email_verified": True})
    assert "not issued to Google Chat" in run(gc.check(chat_event(), someone_else))[2]


@pytest.mark.parametrize("auth, fragment", [
    ("", "no bearer token"),
    ("Basic abc", "no bearer token"),
    ("Bearer not-a-jwt", "not a JWT"),
    (lambda: "Bearer " + token({"iss": gc.CHAT_ISSUER, "aud": "999"}), "audience"),
    (lambda: "Bearer " + token({"iss": "someone@else.com", "aud": PROJECT}), "issued by"),
    (lambda: "Bearer " + token({"iss": gc.CHAT_ISSUER, "aud": PROJECT, "exp": int(time.time()) - 400}), "expired"),
    (lambda: "Bearer " + token({"iss": gc.CHAT_ISSUER, "aud": PROJECT}, key=OTHER_KEY), "Signature"),
    (lambda: "Bearer " + token({"iss": gc.CHAT_ISSUER, "aud": PROJECT}, kid="unknown"), "does not publish"),
    (lambda: "Bearer " + token({"iss": gc.CHAT_ISSUER, "aud": PROJECT}, alg="HS256"), "algorithm"),
])
def test_a_request_that_does_not_verify_is_refused(provider, auth, fragment):
    chat_bot()
    instance, event, why = run(gc.check(chat_event(), auth() if callable(auth) else auth))
    assert instance is None and why.startswith("unauthorized") and fragment.lower() in why.lower(), why


def test_clock_skew_within_five_minutes_is_accepted(provider):
    chat_bot()
    assert run(gc.check(chat_event(), project_token(exp=int(time.time()) - 200)))[2] == ""


def test_a_rotated_key_is_fetched(provider):
    chat_bot()
    assert run(gc.check(chat_event(), project_token()))[2] == ""
    provider.kids["k2"] = OTHER_KEY
    gc.PROJECT_KEYS._fetched -= 120                      # the cache is older than the one-minute refetch guard
    rotated = "Bearer " + token({"iss": gc.CHAT_ISSUER, "aud": PROJECT}, key=OTHER_KEY, kid="k2")
    assert run(gc.check(chat_event(), rotated))[2] == ""


def test_only_allowed_people_and_never_bots_are_answered(provider):
    chat_bot(allowed=("alice@example.com", "users/77"))
    assert run(gc.check(chat_event(email="mallory@example.com"), project_token()))[2] == "not allowed"
    audits = [dict(r) for r in db.get_conn().execute("SELECT actor, action FROM audit_log")]
    assert {"actor": "mallory@example.com", "action": "unauthorized_attempt"} in audits
    assert run(gc.check(chat_event(email="ALICE@example.com"), project_token()))[2] == ""
    assert run(gc.check(chat_event(sender_type="BOT"), project_token()))[2] == "from a bot"


def test_a_workspace_add_on_event_is_refused_with_a_reason(provider):
    chat_bot()
    why = run(gc.check({"chat": {"messagePayload": {}}, "commonEventObject": {}}, project_token()))[2]
    assert "Workspace add-on" in why and why.startswith("unauthorized")


def test_the_google_chat_webhook(provider, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    chat_bot()
    delivered = []

    async def fake_deliver(instance, event):
        delivered.append(event["text"])
    monkeypatch.setattr(gc, "deliver", fake_deliver)
    client = TestClient(build_app())
    assert client.post("/webhooks/googlechat", json=chat_event()).status_code == 401
    assert client.post("/webhooks/googlechat", json=chat_event(), headers={"Authorization": project_token(aud="1")}).status_code == 401
    r = client.post("/webhooks/googlechat", json=chat_event(), headers={"Authorization": project_token()})
    assert r.status_code == 200 and r.json() == {} and delivered == ["what is up"]
    r = client.post("/webhooks/googlechat", json=chat_event(etype="ADDED_TO_SPACE"), headers={"Authorization": project_token()})
    assert "Hi!" in r.json()["text"]
    assert client.post("/webhooks/googlechat", content=b"nope", headers={"Authorization": project_token()}).status_code == 400


# ---- Microsoft Teams -----------------------------------------------------------------------------------------------
def teams_bot(tenant=TENANT, allowed=(ALICE_OID,)) -> int:
    creds = {"app_id": APP_ID, "app_password": "right-secret"}
    if tenant:
        creds["tenant_id"] = tenant
    return bot_instances.create_instance(name=f"Teams {tenant[:4] if tenant else 'mt'}", platform="teams", backend="native_agent",
                                         credentials=creds, allowed_user_ids=list(allowed))


def activity(text="<at>ABP</at> deploy&nbsp;status?<br>please", oid=ALICE_OID, atype="message", service_url=SERVICE_URL):
    return {"type": atype, "id": "act-1", "serviceUrl": service_url, "channelId": "msteams",
            "from": {"id": "29:abc", "name": "Alice", "aadObjectId": oid},
            "conversation": {"id": "a:conv-1", "conversationType": "personal", "tenantId": TENANT},
            "recipient": {"id": f"28:{APP_ID}", "name": "ABP"}, "text": text}


def connector_token(**over):
    return "Bearer " + token({"iss": teams.ISSUER, "aud": APP_ID, "serviceUrl": SERVICE_URL, **over})


def test_a_verified_teams_message_is_answered_as_a_reply(provider):
    teams_bot()
    instance, why, status = run(teams.check(activity(), connector_token()))
    assert (why, status) == ("", 200)
    run(teams.deliver(instance, activity()))
    typing, reply = provider.posts
    assert typing[0] == f"{SERVICE_URL}v3/conversations/a:conv-1/activities" and typing[2] == {"type": "typing"}
    assert reply[0] == f"{SERVICE_URL}v3/conversations/a:conv-1/activities/act-1"
    assert reply[1]["authorization"] == "Bearer ms-access"
    assert reply[2] == {"type": "message", "text": "**Answer** to: deploy status?\nplease", "textFormat": "markdown"}
    assert provider.token_requests[0]["to"] == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert provider.token_requests[0]["scope"] == teams.SCOPE and len(provider.token_requests) == 1


def test_a_multi_tenant_bot_gets_its_token_from_botframework_com_and_can_message_later(provider):
    iid = teams_bot(tenant="")
    instance, _, _ = run(teams.check(activity(), connector_token()))
    run(teams.deliver(instance, activity()))
    assert provider.token_requests[0]["to"] == "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    provider.posts.clear()
    run(teams.send_text(bot_instances.get_instance(iid), "a:conv-1", "a scheduled message"))
    (url, _, body), = provider.posts
    assert url == f"{SERVICE_URL}v3/conversations/a:conv-1/activities" and body["text"] == "a scheduled message"


def test_a_wrong_client_secret_sends_nothing(provider):
    iid = teams_bot()
    row = bot_instances.get_instance(iid)
    row["credentials"]["app_password"] = "wrong"
    assert run(teams.post_activity(row, SERVICE_URL, "a:conv-1", {"type": "message", "text": "x"})) is False
    assert provider.posts == []


@pytest.mark.parametrize("auth, status, fragment", [
    ("", 401, "no bearer token"),
    (lambda: connector_token(aud="someone-else"), 401, "audience"),
    (lambda: connector_token(iss="https://sts.windows.net/x/"), 401, "issued by"),
    (lambda: connector_token(serviceUrl="https://evil.example/"), 401, "serviceUrl"),
    (lambda: "Bearer " + token({"iss": teams.ISSUER, "aud": APP_ID, "serviceUrl": SERVICE_URL}, key=OTHER_KEY), 401, "Signature"),
])
def test_a_teams_request_that_does_not_verify_is_refused(provider, auth, status, fragment):
    teams_bot()
    instance, why, got = run(teams.check(activity(), auth() if callable(auth) else auth))
    assert instance is None and got == status and fragment.lower() in why.lower(), why


def test_a_key_not_endorsed_for_the_channel_is_forbidden(provider):
    teams_bot()
    provider.endorsements = ["skype"]
    instance, why, status = run(teams.check(activity(), connector_token()))
    assert instance is None and status == 403 and "endorsed" in why


def test_other_activities_and_other_people_get_no_answer(provider):
    teams_bot()
    assert run(teams.check(activity(atype="conversationUpdate"), connector_token()))[1:] == ("a conversationUpdate activity (nothing to answer)", 200)
    assert run(teams.check(activity(oid="00000000-0000-0000-0000-000000000000"), connector_token()))[1:] == ("not allowed", 200)


def test_the_teams_webhook(provider, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    teams_bot()
    delivered = []

    async def fake_deliver(instance, act):
        delivered.append(act["id"])
    monkeypatch.setattr(teams, "deliver", fake_deliver)
    client = TestClient(build_app())
    assert client.post("/webhooks/teams", json=activity()).status_code == 401
    provider.endorsements = ["skype"]
    assert client.post("/webhooks/teams", json=activity(), headers={"Authorization": connector_token()}).status_code == 403
    provider.endorsements = ["msteams"]
    teams.CONNECTOR_KEYS._keys = {}                      # the provider changed its keys; drop the 24-hour cache
    r = client.post("/webhooks/teams", json=activity(), headers={"Authorization": connector_token()})
    assert r.status_code == 200 and delivered == ["act-1"]
    r = client.post("/webhooks/teams", json=activity(atype="typing"), headers={"Authorization": connector_token()})
    assert r.status_code == 200 and delivered == ["act-1"]


def test_mentions_and_html_are_removed():
    assert teams.clean_text({"text": "<at>Bot</at>&nbsp;hi <b>there</b><br/>line two &amp; more"}) == "hi there\nline two & more"


# ---- setting them up -----------------------------------------------------------------------------------------------
def test_credentials_and_allowed_users_are_checked():
    assert validators.validate_google_service_account(json.dumps(SERVICE_ACCOUNT))[0]
    assert not validators.validate_google_service_account('{"type": "authorized_user"}')[0]
    assert not validators.validate_google_service_account("not json")[0]
    assert validators.validate_chat_audience(PROJECT)[0] and validators.validate_chat_audience("https://x.example/hook")[0]
    assert not validators.validate_chat_audience("http://insecure.example")[0]
    assert validators.validate_guid(APP_ID)[0] and not validators.validate_guid("not-a-guid")[0]
    assert validators.validate_allowed_for("googlechat", ["a@b.co", "users/123"])[0]
    assert not validators.validate_allowed_for("googlechat", ["Alice"])[0]
    assert validators.validate_allowed_for("teams", [ALICE_OID, "29:1abc"])[0]
    assert not validators.validate_allowed_for("teams", ["alice@example.com"])[0]
    with pytest.raises(bot_instances.ValidationError):
        bot_instances.create_instance(name="bad", platform="teams", backend="native_agent", credentials={"app_id": "x", "app_password": "y"},
                                      allowed_user_ids=[ALICE_OID])


def test_the_setup_guides_and_forms_know_both():
    from bot import platform_guides
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for platform in ("googlechat", "teams"):
        guide = platform_guides.PLATFORM_GUIDES[platform] if hasattr(platform_guides, "PLATFORM_GUIDES") else None
        guide = guide or next(v for k, v in vars(platform_guides).items() if isinstance(v, dict) and platform in v)[platform]
        assert guide["fields"] and guide["setup_guide"]
        for ui in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
            assert f'<option value="{platform}">' in (root / ui).read_text(encoding="utf-8")
        for js in ("bot/dashboard/static/dashboard.js", "desktop-app/ui/main.js"):
            assert f"'{platform}'" in (root / js).read_text(encoding="utf-8")
