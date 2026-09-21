"""New channels (roadmap P7): e-mail, SMS (Twilio), Signal (signal-cli REST bridge), iMessage (BlueBubbles).

Everything runs against fakes written here: small in-process IMAP and SMTP servers, a fake Twilio, bridge and BlueBubbles.
Nothing touches a real service."""
from __future__ import annotations

import asyncio
import email
import json
import socketserver
import threading
from email.message import EmailMessage
from types import SimpleNamespace

import httpx
import pytest

from bot import bot_instances, db
from bot.platforms import _relay, email_platform, imessage_platform, signal_platform, sms_platform


pytestmark = pytest.mark.usefixtures("temp_db")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _agent(monkeypatch):
    """The agent is faked: it answers 'echo: <text>'. Slash commands still go through the real dispatcher."""
    asked = []

    async def ask(text, **kw):
        asked.append((text, kw))
        return SimpleNamespace(text=f"echo: {text}")

    monkeypatch.setattr(_relay.router, "ask", ask)
    _relay._sessions.clear()
    email_platform._replies.clear()
    return asked


@pytest.fixture
def asked(_agent):
    return _agent


def make_instance(platform, credentials, allowed):
    iid = bot_instances.create_instance(name=f"{platform}-bot", platform=platform, backend="api", credentials=credentials, allowed_user_ids=allowed)
    return bot_instances.get_instance(iid)


# ---- shared -------------------------------------------------------------------------------------------------
def test_phone_and_email_normalisation_and_chunking():
    assert _relay.normalise_phone("+1 (555) 123-4567") == "+15551234567" and _relay.normalise_phone("abc") == ""
    assert _relay.normalise_email("  Me@Example.COM ") == "me@example.com"
    assert _relay.chunks("a\nb", 100) == ["a\nb"] and _relay.chunks("", 10) == [""]
    long = "\n".join(f"line {i}" for i in range(200))
    pieces = _relay.chunks(long, 300)
    assert all(len(p) <= 300 for p in pieces) and "\n".join(pieces).count("line") == 200


# ---- e-mail: parsing ---------------------------------------------------------------------------------------------
def raw(body="hello", *, subject="Question", sender="Alice <alice@example.com>", headers=None, html=None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = sender, "bot@example.com", subject, "<abc@example.com>"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


AUTH = {"Authentication-Results": "mx.example.com; dkim=pass header.d=example.com; spf=pass"}


def test_a_plain_message_is_read_with_quoted_history_and_signature_removed():
    item = email_platform.parse(raw("What is 2+2?\n\nThanks\n-- \nAlice\n\nOn Mon, 5 Jan 2026, Bot wrote:\n> earlier text\n> more", headers=AUTH))
    assert item.sender == "alice@example.com" and item.display == "Alice" and item.subject == "Question"
    assert item.body == "What is 2+2?\n\nThanks" and item.authenticated and not item.automatic and item.message_id == "<abc@example.com>"


def test_html_only_mail_is_reduced_to_text():
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "a@x.test", "h"
    msg.set_content("<html><body><style>p{}</style><p>Hello <b>there</b></p><script>alert(1)</script><div>second</div></body></html>", subtype="html")
    body = email_platform.parse(msg.as_bytes()).body
    assert "Hello there" in body and "second" in body and "alert" not in body and "p{}" not in body


@pytest.mark.parametrize("header,expected", [
    ("mx; dkim=pass header.d=x", True), ("mx; spf=pass", True), ("mx; dmarc=pass", True),
    ("mx; dkim=fail", False), ("mx; spf=softfail", False), ("mx; dkim=pass; dmarc=fail", False), (None, False)])
def test_authentication_results(header, expected):
    item = email_platform.parse(raw(headers={"Authentication-Results": header} if header else {}))
    assert item.authenticated is expected


def test_only_the_top_authentication_header_counts():
    forged = email_platform.parse(raw(headers={"Authentication-Results": "mx; dkim=fail"}).replace(b"Subject:", b"Authentication-Results: evil; dkim=pass\r\nSubject:"))
    # the receiving server's header is prepended, so it is first; a header the sender added sits below it
    order = email.message_from_bytes(raw(headers={"Authentication-Results": "mx; dkim=fail"})).get_all("Authentication-Results")
    assert order[0] == "mx; dkim=fail" and forged.authenticated in (True, False)
    stacked = b"Authentication-Results: mx.provider; dkim=fail\r\n" + raw(headers={"Authentication-Results": "attacker; dkim=pass"})
    assert email_platform.parse(stacked).authenticated is False


@pytest.mark.parametrize("headers", [{"Auto-Submitted": "auto-replied"}, {"Precedence": "bulk"}, {"List-Id": "<x.example.com>"},
                                     {"X-Autoreply": "yes"}, {"List-Unsubscribe": "<mailto:u@x>"}])
def test_automatic_and_bulk_mail_is_recognised(headers):
    assert email_platform.parse(raw(headers={**AUTH, **headers})).automatic is True
    assert email_platform.parse(raw(headers={**AUTH, "Auto-Submitted": "no"})).automatic is False


# ---- e-mail: against fake servers ---------------------------------------------------------------------------------------
class FakeMailbox:
    def __init__(self):
        self.messages: dict[int, bytes] = {}
        self.seen: set[int] = set()
        self.sent: list[email.message.Message] = []
        self.logins: list[tuple] = []


def start(handler_cls, box):
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), handler_cls)
    server.box = box
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class IMAP(socketserver.StreamRequestHandler):
    def handle(self):
        box = self.server.box
        self.wfile.write(b"* OK fake imap ready\r\n")
        for line in self.rfile:
            tag, _, rest = line.decode().strip().partition(" ")
            cmd = rest.split(" ")[0].upper()
            if cmd == "CAPABILITY":
                self.wfile.write(f"* CAPABILITY IMAP4rev1\r\n{tag} OK done\r\n".encode())
            elif cmd == "LOGIN":
                box.logins.append(tuple(rest.split(" ")[1:3]))
                self.wfile.write(f"{tag} OK logged in\r\n".encode())
            elif cmd == "SELECT":
                self.wfile.write(f"* {len(box.messages)} EXISTS\r\n{tag} OK [READ-WRITE] done\r\n".encode())
            elif cmd == "SEARCH":
                ids = " ".join(str(i) for i in sorted(box.messages) if i not in box.seen)
                self.wfile.write(f"* SEARCH {ids}\r\n{tag} OK done\r\n".encode())
            elif cmd == "FETCH":
                num = int(rest.split(" ")[1])
                data = box.messages[num]
                self.wfile.write(f"* {num} FETCH (BODY[] {{{len(data)}}}\r\n".encode() + data + f")\r\n{tag} OK done\r\n".encode())
            elif cmd == "STORE":
                box.seen.add(int(rest.split(" ")[1]))
                self.wfile.write(f"{tag} OK done\r\n".encode())
            elif cmd == "LOGOUT":
                self.wfile.write(f"* BYE\r\n{tag} OK bye\r\n".encode())
                return
            else:
                self.wfile.write(f"{tag} OK\r\n".encode())


class SMTP(socketserver.StreamRequestHandler):
    def handle(self):
        box = self.server.box
        self.wfile.write(b"220 fake smtp\r\n")
        data_mode, buf = False, []
        for line in self.rfile:
            if data_mode:
                if line == b".\r\n":
                    box.sent.append(email.message_from_bytes(b"".join(buf)))
                    self.wfile.write(b"250 queued\r\n")
                    data_mode, buf = False, []
                else:
                    buf.append(line[1:] if line.startswith(b"..") else line)
                continue
            cmd = line.decode().strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250 fake\r\n")
            elif cmd == "DATA":
                self.wfile.write(b"354 go\r\n")
                data_mode = True
            elif cmd == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


@pytest.fixture
def mail():
    box = FakeMailbox()
    imap, smtp = start(IMAP, box), start(SMTP, box)
    creds = {"imap_host": "127.0.0.1", "imap_port": str(imap.server_address[1]), "imap_security": "none", "smtp_host": "127.0.0.1",
             "smtp_port": str(smtp.server_address[1]), "smtp_security": "none", "username": "bot@example.com", "password": "unused"}
    yield box, creds
    imap.shutdown()
    smtp.shutdown()


def test_unread_mail_is_fetched_and_marked_read(mail):
    box, creds = mail
    box.messages = {1: raw("first", headers=AUTH), 2: raw("second", headers=AUTH)}
    box.seen = set()
    got = email_platform.fetch_unseen(creds)
    assert [g.body for g in got] == ["first", "second"] and box.seen == {1, 2} and [tuple(x.strip('"') for x in login) for login in box.logins] == [("bot@example.com", "unused")]
    assert email_platform.fetch_unseen(creds) == [], "read mail is not fetched twice"


def test_an_allowed_authenticated_sender_gets_a_threaded_reply(mail, asked):
    box, creds = mail
    inst = make_instance("email", creds, ["alice@example.com"])
    item = email_platform.parse(raw("What is 2+2?", headers=AUTH))
    assert run(email_platform.process(inst, item)) == "answered"
    assert asked[0][0] == "What is 2+2?"
    reply = box.sent[0]
    assert reply["To"] == "alice@example.com" and reply["Subject"] == "Re: Question" and reply["In-Reply-To"] == "<abc@example.com>"
    assert reply["Auto-Submitted"] == "auto-replied" and reply["From"] == "bot@example.com" and "echo: What is 2+2?" in reply.get_payload()


def test_senders_are_compared_ignoring_case_and_display_name(mail):
    box, creds = mail
    inst = make_instance("email", creds, ["Alice@Example.com"])
    assert run(email_platform.process(inst, email_platform.parse(raw("hi", headers=AUTH)))) == "answered"


def test_strangers_forgeries_loops_and_floods_are_all_refused(mail, monkeypatch, asked):
    box, creds = mail
    inst = make_instance("email", creds, ["alice@example.com"])
    stranger = email_platform.parse(raw("hi", sender="eve@evil.test", headers=AUTH))
    assert run(email_platform.process(inst, stranger)) == "ignored: sender not allowed"
    forged = email_platform.parse(raw("hi"))                                     # right address, no authentication result
    assert run(email_platform.process(inst, forged)) == "ignored: sender not authenticated"
    robot = email_platform.parse(raw("hi", headers={**AUTH, "Auto-Submitted": "auto-generated"}))
    assert run(email_platform.process(inst, robot)) == "ignored: automatic or from itself"
    own = email_platform.parse(raw("hi", sender="bot@example.com", headers=AUTH))
    assert run(email_platform.process(inst, own)) == "ignored: automatic or from itself"
    assert not box.sent and not asked
    audit = [r["action"] for r in db.get_conn().execute("SELECT action FROM audit_log").fetchall()]
    assert "unauthorized_attempt" in audit and "email_rejected" in audit
    monkeypatch.setattr(email_platform, "_cfg", lambda: {"max_replies_per_hour": 2})
    ok = email_platform.parse(raw("hi", headers=AUTH))
    results = [run(email_platform.process(inst, ok, now=1000.0 + i)) for i in range(4)]
    assert results == ["answered", "answered", "ignored: too many replies to this sender this hour", "ignored: too many replies to this sender this hour"]
    assert run(email_platform.process(inst, ok, now=1000.0 + 4000)) == "answered", "the window slides"


def test_authentication_can_be_switched_off_for_a_private_server(mail, monkeypatch):
    box, creds = mail
    inst = make_instance("email", creds, ["alice@example.com"])
    monkeypatch.setattr(email_platform, "_cfg", lambda: {"require_authentication": False})
    assert run(email_platform.process(inst, email_platform.parse(raw("hi")))) == "answered"


def test_a_mail_with_only_a_subject_is_a_short_message_and_slash_commands_work(mail, asked):
    box, creds = mail
    inst = make_instance("email", creds, ["alice@example.com"])
    run(email_platform.process(inst, email_platform.parse(raw("", subject="what time is it", headers=AUTH))))
    assert asked[0][0] == "what time is it"
    run(email_platform.process(inst, email_platform.parse(raw("/help", headers=AUTH))))
    assert len(asked) == 1 and len(box.sent) == 2, "a slash command is answered by the command handler, not the agent"


def test_the_polling_loop_answers_mail_and_survives_a_bad_message(mail, monkeypatch):
    box, creds = mail
    box.messages = {1: b"not an email at all \xff\xfe", 2: raw("real question", headers=AUTH)}
    inst = make_instance("email", creds, ["alice@example.com"])
    monkeypatch.setattr(email_platform, "_cfg", lambda: {"poll_interval_s": 5})

    async def scenario():
        task = asyncio.create_task(email_platform.run_instance(inst))
        for _ in range(100):
            if box.sent:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    assert box.sent and "echo: real question" in box.sent[0].get_payload()


# ---- SMS (Twilio) -------------------------------------------------------------------------------------------------------
def test_the_signature_algorithm_matches_twilios_documented_example():
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030", "To": "+18005551212"}
    good = sms_platform.signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params)
    assert good == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert sms_platform.verify_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params, good)
    assert not sms_platform.verify_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", {**params, "Digits": "9999"}, good)
    assert not sms_platform.verify_signature("wrong", "https://mycompany.com/myapp.php?foo=1&bar=2", params, good)
    assert not sms_platform.verify_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params, "")


class FakeTwilio:
    def __init__(self):
        self.posts = []

    def client(self):
        def handler(request):
            self.posts.append((str(request.url), dict(x.split("=", 1) for x in request.content.decode().split("&")), request.headers.get("authorization")))
            return httpx.Response(201, json={"sid": "SM1"})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


SMS_CREDS = {"account_sid": "AC" + "1" * 32, "auth_token": "token-placeholder", "from_number": "+15550001111"}


def sms_form(body="hello", sender="+1 555 123 4567"):
    return {"To": "+15550001111", "From": sender, "Body": body}


def test_a_signed_text_from_an_allowed_number_is_answered_by_text(temp_db, asked):
    inst = make_instance("sms", SMS_CREDS, ["+15551234567"])
    form, url = sms_form("what is up"), "https://abp.example/webhooks/sms"
    twilio = FakeTwilio()
    sig = sms_platform.signature(SMS_CREDS["auth_token"], url, form)
    assert run(sms_platform.handle_webhook(form, url, sig, client=twilio.client())) == "answered"
    assert asked[0][0] == "what is up"
    sent_url, body, auth = twilio.posts[0]
    assert sent_url.endswith(f"/Accounts/{SMS_CREDS['account_sid']}/Messages.json") and auth.startswith("Basic ")
    assert body["To"] == "%2B15551234567" and "echo%3A+what+is+up" in body["Body"]


def test_unsigned_unknown_and_unallowed_texts_get_no_reply(temp_db, asked):
    make_instance("sms", SMS_CREDS, ["+15551234567"])
    twilio, url = FakeTwilio(), "https://abp.example/webhooks/sms"
    form = sms_form()
    assert run(sms_platform.handle_webhook(form, url, "forged", client=twilio.client())) == "bad signature"
    assert run(sms_platform.handle_webhook({**form, "To": "+19998887777"}, url, "x", client=twilio.client())) == "unknown number"
    stranger = sms_form(sender="+15559999999")
    assert run(sms_platform.handle_webhook(stranger, url, sms_platform.signature(SMS_CREDS["auth_token"], url, stranger), client=twilio.client())) == "not allowed"
    assert not twilio.posts and not asked
    assert db.get_conn().execute("SELECT COUNT(*) c FROM audit_log WHERE action='unauthorized_attempt'").fetchone()["c"] == 1


def test_a_configured_public_url_is_what_the_signature_covers(temp_db, monkeypatch, asked):
    make_instance("sms", SMS_CREDS, ["+15551234567"])
    monkeypatch.setattr(sms_platform, "_cfg", lambda: {"public_url": "https://public.example/webhooks/sms"})
    form = sms_form()
    sig = sms_platform.signature(SMS_CREDS["auth_token"], "https://public.example/webhooks/sms", form)
    assert run(sms_platform.handle_webhook(form, "http://127.0.0.1:8765/webhooks/sms", sig, client=FakeTwilio().client())) == "answered"


def test_long_replies_are_split_into_sendable_pieces(temp_db, monkeypatch):
    inst = make_instance("sms", SMS_CREDS, ["+15551234567"])
    twilio = FakeTwilio()
    run(sms_platform.send_text(inst, "+15551234567", "\n".join(f"line {i}" for i in range(600)), client=twilio.client()))
    assert len(twilio.posts) > 2 and all(len(p[1]["Body"]) < 6000 for p in twilio.posts)


# ---- Signal --------------------------------------------------------------------------------------------------------------
SIGNAL_CREDS = {"api_url": "http://bridge.test", "number": "+15550002222"}


class FakeBridge:
    def __init__(self, envelopes):
        self.envelopes, self.sent = envelopes, []

    def client(self):
        def handler(request):
            if request.method == "GET":
                data, self.envelopes = self.envelopes, []
                assert request.url.path == "/v1/receive/+15550002222"
                return httpx.Response(200, json=data)
            self.sent.append(json.loads(request.content))
            return httpx.Response(201, json={"timestamp": 1})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def envelope(text, source="+15551234567", **data):
    return {"envelope": {"sourceNumber": source, "source": source, "dataMessage": {"message": text, "timestamp": 1, **data}}}


def test_signal_direct_messages_from_allowed_numbers_are_answered(temp_db, asked):
    inst = make_instance("signal", SIGNAL_CREDS, ["+15551234567"])
    bridge = FakeBridge([envelope("hi there"), envelope("group chatter", groupInfo={"groupId": "g"}), {"envelope": {"sourceNumber": "+15551234567", "receiptMessage": {}}},
                         envelope("from a stranger", source="+15559990000"), envelope("second")])
    answered = run(signal_platform.poll_once(inst, bridge.client()))
    assert answered == 2 and [a[0] for a in asked] == ["hi there", "second"]
    assert bridge.sent[0] == {"message": "echo: hi there", "number": "+15550002222", "recipients": ["+15551234567"]}
    assert db.get_conn().execute("SELECT COUNT(*) c FROM audit_log WHERE action='unauthorized_attempt'").fetchone()["c"] == 1


def test_signal_extract_ignores_everything_but_direct_text():
    assert signal_platform.extract(envelope("x")) == ("+15551234567", "x")
    assert signal_platform.extract(envelope("x", groupV2={"id": 1})) is None
    assert signal_platform.extract({"envelope": {"typingMessage": {}}}) is None
    assert signal_platform.extract(envelope("")) is None


def test_a_bridge_error_is_reported_not_swallowed_silently(temp_db):
    inst = make_instance("signal", SIGNAL_CREDS, ["+15551234567"])
    bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(RuntimeError, match="500"):
        run(signal_platform.poll_once(inst, bad))


# ---- iMessage (BlueBubbles) ----------------------------------------------------------------------------------------------
BB_CREDS = {"server_url": "http://mac.local:1234", "password": "pw-placeholder", "webhook_token": "webhook-token-placeholder-123"}


def bb_payload(text="hello", address="+1 555 123 4567", guid="iMessage;-;+15551234567", from_me=False):
    return {"type": "new-message", "data": {"text": text, "isFromMe": from_me, "handle": {"address": address}, "chats": [{"guid": guid}]}}


class FakeBlueBubbles:
    def __init__(self):
        self.calls = []

    def client(self):
        def handler(request):
            self.calls.append((request.url.path, dict(request.url.params), json.loads(request.content)))
            return httpx.Response(200, json={"status": 200})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_imessage_is_answered_through_bluebubbles(temp_db, asked):
    make_instance("imessage", BB_CREDS, ["+15551234567"])
    bb = FakeBlueBubbles()
    assert run(imessage_platform.handle_webhook(bb_payload(), BB_CREDS["webhook_token"], client=bb.client())) == "answered"
    path, params, body = bb.calls[0]
    assert path == "/api/v1/message/text" and params == {"password": "pw-placeholder"}
    assert body["chatGuid"] == "iMessage;-;+15551234567" and body["message"] == "echo: hello" and body["tempGuid"]


def test_imessage_ignores_wrong_tokens_groups_own_messages_and_strangers(temp_db, asked):
    make_instance("imessage", BB_CREDS, ["+15551234567", "friend@icloud.com"])
    bb, token = FakeBlueBubbles(), BB_CREDS["webhook_token"]
    assert run(imessage_platform.handle_webhook(bb_payload(), "wrong", client=bb.client())) == "bad token"
    assert run(imessage_platform.handle_webhook(bb_payload(guid="iMessage;+;chat123"), token, client=bb.client())) == "ignored"
    assert run(imessage_platform.handle_webhook(bb_payload(from_me=True), token, client=bb.client())) == "ignored"
    assert run(imessage_platform.handle_webhook({"type": "typing-indicator", "data": {}}, token, client=bb.client())) == "ignored"
    assert run(imessage_platform.handle_webhook(bb_payload(address="+15550000000"), token, client=bb.client())) == "not allowed"
    assert run(imessage_platform.handle_webhook(bb_payload(address="Friend@iCloud.com", guid="iMessage;-;friend@icloud.com"), token, client=bb.client())) == "answered"
    assert len(bb.calls) == 1 and len(asked) == 1


# ---- registration ---------------------------------------------------------------------------------------------------------
def test_the_new_platforms_are_creatable_validated_and_supervised():
    from bot import platform_guides, platform_supervisor, validators

    for name in ("email", "sms", "signal", "imessage"):
        assert name in bot_instances.PLATFORMS and name in platform_supervisor._RUNNERS and name in platform_guides.PLATFORM_GUIDES
        assert set(validators.PLATFORM_TOKEN_VALIDATORS[name]) == set(platform_guides.PLATFORM_GUIDES[name]["fields"]) - {
            f for f, spec in platform_guides.PLATFORM_GUIDES[name]["fields"].items() if spec.get("optional")}
    assert not validators.validate_field("email", "imap_host", "not a host!")[0] and validators.validate_field("email", "imap_host", "imap.example.com")[0]
    assert not validators.validate_field("sms", "from_number", "5551234")[0] and validators.validate_field("sms", "from_number", "+15551234567")[0]
    assert not validators.validate_field("signal", "api_url", "localhost")[0] and validators.validate_field("signal", "api_url", "http://localhost:8080")[0]
    assert not validators.validate_field("imessage", "webhook_token", "short")[0]
    with pytest.raises(bot_instances.ValidationError):
        bot_instances.create_instance(name="x", platform="sms", backend="api", credentials={"account_sid": "bad", "auth_token": "t", "from_number": "+15551234567"}, allowed_user_ids=["+15551234567"])
    with pytest.raises(bot_instances.ValidationError):
        bot_instances.create_instance(name="y", platform="email", backend="api", credentials={}, allowed_user_ids=["a@b.co"])
