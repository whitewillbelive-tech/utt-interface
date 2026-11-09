"""Dropbox scoped OAuth primitives; no browser, listener, file APIs, or disk storage.

Windows integration is in ``app.dropbox_windows``. Browser loopback redirects
can briefly display the authorization code; they cannot guarantee its absence
from browser history. Development tests use fake transport only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import functools
import threading
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

AUTHORIZATION_ENDPOINT = "https://www.dropbox.com/oauth2/authorize"
TOKEN_ENDPOINT = "https://api.dropboxapi.com/oauth2/token"
READ_SCOPES = frozenset({"files.metadata.read", "files.content.read"})
_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_APP_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SESSION_FACTORY = object()


class OAuthError(Exception):
    """Only a fixed error code is exposed; remote strings never enter errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _redirect(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 2048:
        raise OAuthError("INVALID_REDIRECT")
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        raise OAuthError("INVALID_REDIRECT") from None
    # The redirect must already be registered with Dropbox; do not modify it.
    if (u.scheme != "http" or u.hostname not in ("localhost", "127.0.0.1")
            or not port or not 1024 <= port <= 65535 or u.username is not None
            or u.password is not None or u.query or u.fragment
            or (u.path and not u.path.startswith("/")) or u.path.startswith("//")
            or any(segment in (".", "..") for segment in u.path.split("/"))
            or "%" in u.path or "\\" in u.path or "//" in u.path
            or any(not 0x21 <= ord(c) <= 0x7e for c in value)
            or u.netloc not in (f"localhost:{port}", f"127.0.0.1:{port}")):
        raise OAuthError("INVALID_REDIRECT")
    return value, u.path or "/"


def _challenge(verifier: str) -> str:
    if not isinstance(verifier, str) or not _VERIFIER_RE.fullmatch(verifier):
        raise OAuthError("INVALID_VERIFIER")
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class ReadOnlySession:
    """Memory-only result of a checked response; Python objects are not a trust boundary.

    This says nothing about whether an injected transport is authentic or an
    app is an App Folder app. The constructor seal prevents accidental bypass.
    """

    __slots__ = ("__token", "expires_at", "scopes", "_clock")

    def __init__(self, token: str, expires_at: float, clock: Callable[[], float], *, _seal: object = None):
        if _seal is not _SESSION_FACTORY:
            raise OAuthError("INVALID_SESSION_CONSTRUCTION")
        self.__token = token
        self.expires_at = expires_at
        self.scopes = READ_SCOPES
        self._clock = clock

    def __repr__(self) -> str:
        return "<ReadOnlySession redacted>"

    def is_valid(self) -> bool:
        return bool(self.__token) and self._clock() < self.expires_at

    def close(self) -> None:
        self.__token = ""


def _synchronized(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class AuthAttempt:
    """One-shot PKCE and callback validation; transport is explicitly injected.

    Passing callback data is not itself evidence of safe browser handling or an
    App Folder entitlement. The caller must establish both before deployment.
    """

    def __init__(self, app_key: str, redirect_uri: str, *, clock: Callable[[], float] = time.monotonic):
        if not isinstance(app_key, str) or not _APP_KEY_RE.fullmatch(app_key):
            raise OAuthError("INVALID_APP_KEY")
        self._lock = threading.RLock()
        self._redirect_uri, self._path = _redirect(redirect_uri)
        self._app_key = app_key
        self._clock = clock
        self._state = secrets.token_urlsafe(32)
        self._verifier = secrets.token_urlsafe(64)
        self._challenge = _challenge(self._verifier)
        self._deadline = clock() + 300
        self._code = None
        self._status = "pending"

    def __repr__(self) -> str:
        return "<AuthAttempt redacted>"

    def _pending(self) -> None:
        if self._status != "pending":
            raise OAuthError("ATTEMPT_CLOSED")
        if self._clock() >= self._deadline:
            self.cancel()
            raise OAuthError("ATTEMPT_EXPIRED")

    @_synchronized
    def authorization_url(self) -> str:
        self._pending()
        return AUTHORIZATION_ENDPOINT + "?" + urlencode({
            "client_id": self._app_key, "response_type": "code",
            "redirect_uri": self._redirect_uri,
            "scope": "files.metadata.read files.content.read",
            "token_access_type": "online", "code_challenge_method": "S256",
            "code_challenge": self._challenge, "state": self._state,
        })

    @_synchronized
    def accept_callback(self, target: str) -> None:
        self._pending()
        if (not isinstance(target, str) or len(target) > 8192
                or any(not 0x21 <= ord(c) <= 0x7e for c in target)):
            raise OAuthError("INVALID_CALLBACK")
        try:
            u = urlsplit(target)
            pairs = parse_qsl(u.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
        except ValueError:
            raise OAuthError("INVALID_CALLBACK") from None
        if u.scheme or u.netloc or u.fragment or u.path != self._path:
            raise OAuthError("INVALID_CALLBACK")
        args: dict[str, str] = {}
        for key, value in pairs:
            if key in args:
                raise OAuthError("INVALID_CALLBACK")
            args[key] = value
        supplied_state = args.get("state", "")
        if not supplied_state or not hmac.compare_digest(supplied_state.encode("utf-8"), self._state.encode("ascii")):
            raise OAuthError("STATE_MISMATCH")
        if "error" in args:
            if "code" in args:
                raise OAuthError("INVALID_CALLBACK")
            self.cancel()
            raise OAuthError("AUTH_CANCELLED" if args["error"] == "access_denied" else "AUTH_FAILED")
        code = args.get("code")
        if not code or len(code) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in code):
            raise OAuthError("INVALID_CALLBACK")
        self._code = code
        self._status = "callback"
        self._state = ""

    @_synchronized
    def exchange(self, transport: Callable[[str, dict[str, str]], Any]) -> ReadOnlySession:
        if self._status != "callback":
            raise OAuthError("ATTEMPT_CLOSED")
        if self._clock() >= self._deadline:
            self.cancel()
            raise OAuthError("ATTEMPT_EXPIRED")
        # Close before calling arbitrary transport, preventing retry/reentrancy.
        code, verifier = self._code, self._verifier
        self._code = None
        self._verifier = ""
        self._status = "closed"
        if not _VERIFIER_RE.fullmatch(verifier) or not hmac.compare_digest(_challenge(verifier), self._challenge):
            raise OAuthError("INVALID_VERIFIER")
        form = {"code": code, "grant_type": "authorization_code", "client_id": self._app_key,
                "redirect_uri": self._redirect_uri, "code_verifier": verifier}
        started = self._clock()
        try:
            result = transport(TOKEN_ENDPOINT, form)
        except Exception:
            raise OAuthError("TOKEN_EXCHANGE_FAILED") from None
        finally:
            form.clear()
        if not isinstance(result, dict):
            raise OAuthError("INVALID_TOKEN_RESPONSE")
        scope, token, ttl = result.get("scope"), result.get("access_token"), result.get("expires_in")
        if not isinstance(scope, str):
            raise OAuthError("SCOPE_UNVERIFIED")
        parts = scope.split(" ")
        if len(parts) != len(READ_SCOPES) or frozenset(parts) != READ_SCOPES:
            raise OAuthError("SCOPE_REJECTED")
        if (result.get("token_type", "").lower() != "bearer" if isinstance(result.get("token_type"), str) else True):
            raise OAuthError("INVALID_TOKEN_RESPONSE")
        if isinstance(ttl, str) and 1 <= len(ttl) <= 5 and all('0' <= c <= '9' for c in ttl):
            ttl = int(ttl)
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or isinstance(ttl, bool) or not isinstance(ttl, int)
                or not 0 < ttl <= 86400
                or "refresh_token" in result):
            raise OAuthError("INVALID_TOKEN_RESPONSE")
        expires_at = started + ttl
        if self._clock() >= expires_at:
            raise OAuthError("TOKEN_EXPIRED")
        return ReadOnlySession(token, expires_at, self._clock, _seal=_SESSION_FACTORY)

    @_synchronized
    def cancel(self) -> None:
        self._state = ""
        self._verifier = ""
        self._code = None
        self._status = "closed"


def main() -> int:
    # The core is not itself a user-facing flow; read no local configuration.
    print("Authorization is unavailable in this core; use the Windows-only app.dropbox_windows entrypoint.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
