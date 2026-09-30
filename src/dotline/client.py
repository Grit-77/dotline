"""HTTP client with bounded polling and no credential-bearing redirects."""

import json
import time
import urllib.error
import urllib.request

from .config import Config
from .store import validate_text


class ClientError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, config: Config):
        self.config = config
        # Ignore ambient proxy settings: the bearer must go only to the configured origin.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path: str, body: dict | None = None, *, auth: bool = True) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if data is not None and not 1 <= len(data) <= 16384:
            raise ClientError("encoded body must contain 1 to 16384 bytes")
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.config.token()
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(self.config.client_url + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise ClientError(f"HTTP {error.code}; check the URL, token file and request limits") from None
        except (OSError, ValueError):
            raise ClientError("request failed; check the URL and server availability") from None
        if not isinstance(result, dict):
            raise ClientError("server returned an invalid response")
        return result

    def send(self, text: str, topic: str = "") -> dict:
        validate_text(text)
        if not isinstance(topic, str) or len(topic) > 120:
            raise ClientError("topic must contain at most 120 characters")
        return self.request("/v1/messages", {"text": text, "topic": topic})

    def replies(self, after: int = 0) -> list[dict]:
        if after < 0:
            raise ClientError("after must be nonnegative")
        return self.request(f"/v1/replies?after={after}")["replies"]

    def wait(self, message_id: int, minutes: float = 10, *, clock=time.monotonic, sleep=time.sleep) -> dict:
        if message_id < 1 or not 0 < minutes <= 10080:
            raise ClientError("id must be positive; minutes must be between 0 and 10080")
        deadline = clock() + minutes * 60
        after = 0
        while True:
            replies = self.replies(after)
            for reply in replies:
                if reply["to"] == message_id:
                    return reply
                after = max(after, reply["id"])
            remaining = deadline - clock()
            if remaining <= 0:
                raise ClientError("timed out waiting for a reply")
            sleep(min(20, remaining))

    def health(self) -> dict:
        return self.request("/v1/health", auth=False)
