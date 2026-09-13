import httpx
import pytest

from app.notifier import LogNotifier, TelegramNotifier, build_notifier


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_telegram_send_posts_to_bot_api(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = _mock_transport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))

    n = TelegramNotifier("123:ABC", "-100200")
    assert n.send("<b>hello</b>") is True
    assert seen["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert b'"chat_id": "-100200"' in seen["json"] or b'"chat_id":"-100200"' in seen["json"]
    assert b"hello" in seen["json"]


def test_telegram_send_returns_false_on_api_error(monkeypatch):
    transport = _mock_transport(lambda req: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    assert TelegramNotifier("bad", "1").send("x") is False


def test_telegram_send_returns_false_on_network_error(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "post", boom)
    assert TelegramNotifier("t", "c").send("x") is False


def test_telegram_requires_token_and_chat():
    with pytest.raises(ValueError):
        TelegramNotifier("", "1")


def test_build_notifier_falls_back_to_log():
    assert isinstance(build_notifier("", ""), LogNotifier)
    assert isinstance(build_notifier("t", "c"), TelegramNotifier)


def test_run_expiry_check_endpoint_uses_mocked_telegram(client, monkeypatch):
    from app import main as main_module

    sent = []

    class Recorder:
        channel = "telegram"

        def send(self, text):
            sent.append(text)
            return True

    monkeypatch.setattr(main_module, "build_notifier", lambda token, chat: Recorder())

    from datetime import date, timedelta

    expiry = (date.today() + timedelta(days=7)).isoformat()
    r = client.post(
        "/api/credentials",
        json={
            "name": "DocuSign RSA key",
            "provider": "DocuSign",
            "owner": "integrations",
            "expiry_date": expiry,
            "kind": "signing_key",
        },
    )
    assert r.status_code == 201
    assert r.json()["status"] == "critical"
    assert r.json()["days_remaining"] == 7

    r = client.post("/api/actions/run-expiry-check")
    assert r.status_code == 200
    body = r.json()
    assert len(body["notified"]) == 1 and body["notified"][0]["threshold"] == 7
    assert len(sent) == 1 and "DocuSign RSA key" in sent[0]

    r = client.post("/api/actions/run-expiry-check")
    assert r.json()["notified"] == [] and r.json()["skipped_duplicates"] == 1
    assert len(sent) == 1

    r = client.get("/api/notifications")
    assert len(r.json()) == 1 and r.json()[0]["channel"] == "telegram"
