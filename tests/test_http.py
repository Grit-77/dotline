import http.client
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from dotline.client import Client
from dotline.server import RateLimit


def request(api, method="GET", path="/v1/messages", body=None, *, auth=True, token=None):
    server, config, _ = api
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    headers = {}
    if auth:
        headers["Authorization"] = "Bearer " + (config.token() if token is None else token)
    if isinstance(body, dict) or isinstance(body, list):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    status = response.status
    response_headers = dict(response.getheaders())
    raw = response.read()
    connection.close()
    return status, response_headers, json.loads(raw) if raw else None


def test_auth_refusal(api):
    status, headers, body = request(api, auth=False)
    assert status == 401
    assert headers["Cache-Control"] == "no-store"
    assert body == {"error": "authentication required"}


def test_bad_token(api):
    assert request(api, token="invalid-test-bearer")[0] == 401


def test_health_is_public(api):
    assert request(api, path="/v1/health", auth=False)[2] == {"ok": True}
    assert Client(api[1]).health() == {"ok": True}


@pytest.mark.parametrize("body", [b"", b"x" * 16385])
def test_body_limits_413(api, body):
    assert request(api, "POST", body=body)[0] == 413


@pytest.mark.parametrize("text", ["", "x" * 8001, 123, None])
def test_text_limits_400(api, text):
    assert request(api, "POST", body={"text": text})[0] == 400


@pytest.mark.parametrize("body", [b"not-json", [], {"text": "valid", "topic": "x" * 121}, {"text": "valid", "topic": 7}, b'\xff'])
def test_invalid_body_or_topic_400(api, body):
    assert request(api, "POST", body=body)[0] == 400


def test_character_and_byte_boundaries(api):
    assert request(api, "POST", body={"text": "x" * 8000, "topic": "t" * 120})[0] == 201
    # 8000 multibyte characters fit the character limit but exceed the byte limit.
    assert request(api, "POST", body={"text": "界" * 8000})[0] == 413


def test_rate_limit_429(api):
    for _ in range(30):
        assert request(api, path="/v1/health", auth=False)[0] == 200
    status, headers, _ = request(api, path="/v1/health", auth=False)
    assert status == 429
    assert headers["Retry-After"] == "60"
    assert headers["Cache-Control"] == "no-store"


def test_rate_limit_lapses_after_a_minute():
    now = [0.0]
    limiter = RateLimit(lambda: now[0])
    assert all(limiter.allow() for _ in range(30))
    assert not limiter.allow()
    now[0] = 60
    assert limiter.allow()


def test_send_reply_read_round_trip(api):
    _, config, store = api
    client = Client(config)
    sent = client.send('Hello "Claude"\n你好 👋', "review")
    assert sent["id"] == 1 and sent["ts"]
    store.claim(sent["id"], "session-one")
    reply = store.reply(sent["id"], "Done — ready for review", session="session-one")
    assert client.replies() == [reply]
    assert client.replies(reply["id"]) == []
    assert client.wait(sent["id"], 0.1) == reply
    status, _, body = request(api, path="/v1/messages?after=0")
    assert status == 200
    assert body["messages"][0]["topic"] == "review"
    assert request(api, path="/v1/messages?after=1")[2] == {"messages": []}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CUSTOM"])
def test_unsupported_method_405(api, method):
    status, headers, _ = request(api, method)
    assert status == 405
    assert headers["Cache-Control"] == "no-store"


def test_unknown_path_404(api):
    assert request(api, path="/unknown")[0] == 404


@pytest.mark.parametrize("after", ["-1", "abc", "", "1&after=2"])
def test_invalid_cursor_400(api, after):
    assert request(api, path="/v1/replies?after=" + after)[0] == 400


def test_logs_never_include_token_body_or_query(api, caplog):
    caplog.set_level(logging.INFO, logger="dotline.http")
    token = api[1].token()
    private_text = "private-message-that-must-not-be-logged"
    request(api, "POST", body={"text": private_text})
    # Even a malicious client putting secrets into the URL cannot make logs echo it.
    request(api, path="/unknown?credential=" + token)
    assert token not in caplog.text
    assert private_text not in caplog.text
    assert "credential" not in caplog.text
    assert len(caplog.records) == 2
    assert [record.getMessage() for record in caplog.records] == ["POST /v1/messages 201", "GET unknown 404"]


def test_concurrent_http_appends_assign_unique_ids(api):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: request(api, "POST", body={"text": f"message-{n}"}), range(12)))
    assert all(item[0] == 201 for item in results)
    assert sorted(item[2]["id"] for item in results) == list(range(1, 13))
    assert len(api[2].messages_after()) == 12


def inbox_lines(api):
    return [line for line in api[2].inbox.read_text(encoding="utf-8").splitlines() if line]


def test_first_post_with_a_client_id_answers_201_and_stores_it_on_the_record(api):
    status, _, body = request(api, "POST", body={"text": "hello", "client_id": "send-0001"})
    assert status == 201
    assert body == {"id": 1, "ts": body["ts"]}
    assert api[2].messages_after()[0]["client_id"] == "send-0001"


def test_repeat_of_a_client_id_answers_200_duplicate_with_the_same_id_and_adds_no_line(api):
    first_status, _, first = request(api, "POST", body={"text": "hello", "client_id": "send-0001"})
    status, _, again = request(api, "POST", body={"text": "hello", "client_id": "send-0001"})
    assert first_status == 201
    assert status == 200
    assert again == {"id": first["id"], "ts": first["ts"], "duplicate": True}
    assert len(inbox_lines(api)) == 1


def test_client_id_alone_decides_a_duplicate_even_when_the_text_differs(api):
    _, _, first = request(api, "POST", body={"text": "original", "client_id": "send-0001"})
    status, _, again = request(api, "POST", body={"text": "changed", "topic": "new", "client_id": "send-0001"})
    assert status == 200 and again["id"] == first["id"]
    assert [item["text"] for item in api[2].messages_after()] == ["original"]


def test_a_different_client_id_creates_a_second_message(api):
    first = request(api, "POST", body={"text": "hello", "client_id": "send-0001"})
    second = request(api, "POST", body={"text": "hello", "client_id": "send-0002"})
    assert (first[0], second[0]) == (201, 201)
    assert (first[2]["id"], second[2]["id"]) == (1, 2)
    assert len(inbox_lines(api)) == 2


@pytest.mark.parametrize("client_id", ["x" * 65, "", 7, None, ["send-0001"]])
def test_an_invalid_client_id_is_400_and_creates_nothing(api, client_id):
    assert request(api, "POST", body={"text": "hello", "client_id": client_id})[0] == 400
    assert inbox_lines(api) == []


def test_a_64_character_client_id_is_accepted(api):
    assert request(api, "POST", body={"text": "hello", "client_id": "x" * 64})[0] == 201
