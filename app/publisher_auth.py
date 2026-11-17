"""Separate short-lived Dropbox publisher authorization using PKCE.

The normal read-only OAuth path and its exact two-scope grant are unchanged.
This module requests a distinct three-scope grant only after an explicit caller
starts the Windows flow.  Tokens remain in memory and are never logged or
returned.  App Folder type is user-confirmed, not proven by the token response.
Python process integrity is assumed; private methods are not an OS boundary.
"""
from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
import webbrowser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.dropbox_auth import (
    AuthAttempt, OAuthError, TOKEN_ENDPOINT, _APP_KEY_RE, _VERIFIER_RE,
    _challenge, _synchronized, token_transport,
)

PUBLISHER_SCOPES = frozenset({
    'files.metadata.read', 'files.content.read', 'files.content.write',
})
_TRUSTED_TOKEN_TRANSPORT = token_transport
_TRUSTED_CLOCK = time.monotonic
_PUBLISHER_SESSION_SEAL = object()


class PublisherSession:
    """An in-memory publisher grant, with no token getter or persistence."""

    __slots__ = ('__token', '__app_key', '__clock', '__deadline', '__trusted',
                 '__lock', '__generation')

    def __init__(self, token, app_key, clock, deadline, trusted, *, _seal=None):
        if _seal is not _PUBLISHER_SESSION_SEAL:
            raise OAuthError('INVALID_SESSION_CONSTRUCTION')
        self.__token = token
        self.__app_key = app_key
        self.__clock = clock
        self.__deadline = deadline
        self.__trusted = trusted
        self.__lock = threading.RLock()
        self.__generation = 0

    def __repr__(self):
        return '<PublisherSession redacted>'

    def is_valid(self):
        with self.__lock:
            return bool(self.__token) and self.__clock() < self.__deadline

    def close(self):
        with self.__lock:
            self.__token = ''
            self.__generation += 1

    def _publisher_call(self, callback, operation, *, confirmed_app_key, app_folder_confirmation):
        """Send only through the publisher's fixed dispatcher, not an arbitrary callback.

        Close and request execution are serialized.  A failure after the
        request begins is uncertain and must be reconciled before retrying.
        """
        try:
            from app.publisher_transport import _dispatch, PublisherTransportError
        except ImportError:
            raise OAuthError('PUBLISH_TRANSPORT_UNAVAILABLE') from None
        if callback is not _dispatch:
            raise OAuthError('PUBLISH_TRANSPORT_UNTRUSTED')
        with self.__lock:
            if not self.__token or self.__clock() >= self.__deadline:
                raise OAuthError('AUTH_EXPIRED')
            if not self.__trusted:
                raise OAuthError('PUBLISH_AUTH_UNVERIFIED')
            if app_folder_confirmation != 'USER_CONFIRMED':
                raise OAuthError('APP_FOLDER_UNCONFIRMED')
            if (not isinstance(confirmed_app_key, str) or not _APP_KEY_RE.fullmatch(confirmed_app_key)
                    or not hmac.compare_digest(self.__app_key, confirmed_app_key)):
                raise OAuthError('APP_KEY_MISMATCH')
            try:
                answer = callback(self.__token, operation)
            except PublisherTransportError as exc:
                if exc.code == 'AUTH_REQUIRED':
                    self.__token = ''
                    self.__generation += 1
                raise
            except Exception:
                # Never propagate exception text or captured request details.
                raise OAuthError('PUBLISH_REQUEST_UNCERTAIN') from None
            if not self.__token or self.__clock() >= self.__deadline:
                raise OAuthError('AUTH_EXPIRED_AFTER_REQUEST')
            return answer


class PublisherAuthAttempt(AuthAttempt):
    """Reuse the existing one-shot PKCE and callback checks with a write grant."""

    def __init__(self, app_key, redirect_uri, *, clock=time.monotonic):
        super().__init__(app_key, redirect_uri, clock=clock)
        self.__publisher_clock = clock

    def __repr__(self):
        return '<PublisherAuthAttempt redacted>'

    @_synchronized
    def authorization_url(self):
        # Reuse the original construction (client ID, redirect, PKCE, state).
        split = urlsplit(super().authorization_url())
        fields = parse_qsl(split.query, keep_blank_values=True)
        fields = [(k, 'files.metadata.read files.content.read files.content.write' if k == 'scope' else v)
                  for k, v in fields]
        return urlunsplit((split.scheme, split.netloc, split.path, urlencode(fields), ''))

    @_synchronized
    def exchange(self, transport):
        if self._status != 'callback':
            raise OAuthError('ATTEMPT_CLOSED')
        if self.__publisher_clock() >= self._deadline:
            self.cancel()
            raise OAuthError('ATTEMPT_EXPIRED')
        code, verifier, app_key = self._code, self._verifier, self._app_key
        self._code = None
        self._verifier = ''
        self._status = 'closed'
        if (not isinstance(verifier, str) or not _VERIFIER_RE.fullmatch(verifier)
                or not hmac.compare_digest(_challenge(verifier), self._challenge)):
            raise OAuthError('INVALID_VERIFIER')
        form = {'code': code, 'grant_type': 'authorization_code', 'client_id': app_key,
                'redirect_uri': self._redirect_uri, 'code_verifier': verifier}
        started = self.__publisher_clock()
        try:
            response = transport(TOKEN_ENDPOINT, form)
        except Exception:
            raise OAuthError('TOKEN_EXCHANGE_FAILED') from None
        finally:
            form.clear()
        if not isinstance(response, dict):
            raise OAuthError('INVALID_TOKEN_RESPONSE')
        scopes, token, ttl = response.get('scope'), response.get('access_token'), response.get('expires_in')
        if not isinstance(scopes, str):
            raise OAuthError('SCOPE_UNVERIFIED')
        parts = scopes.split(' ')
        if len(parts) != len(PUBLISHER_SCOPES) or frozenset(parts) != PUBLISHER_SCOPES:
            raise OAuthError('SCOPE_REJECTED')
        token_type = response.get('token_type')
        if not isinstance(token_type, str) or token_type.lower() != 'bearer':
            raise OAuthError('INVALID_TOKEN_RESPONSE')
        if isinstance(ttl, str) and 1 <= len(ttl) <= 5 and all('0' <= c <= '9' for c in ttl):
            ttl = int(ttl)
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(not 33 <= ord(c) <= 126 for c in token)
                or isinstance(ttl, bool) or not isinstance(ttl, int)
                or not 0 < ttl <= 86400 or 'refresh_token' in response):
            raise OAuthError('INVALID_TOKEN_RESPONSE')
        deadline = started + ttl
        if self.__publisher_clock() >= deadline:
            raise OAuthError('TOKEN_EXPIRED')
        return PublisherSession(token, app_key, self.__publisher_clock, deadline,
                                transport is _TRUSTED_TOKEN_TRANSPORT
                                and self.__publisher_clock is _TRUSTED_CLOCK,
                                _seal=_PUBLISHER_SESSION_SEAL)


def run_publisher_flow(app_key, configured_redirect, registered_redirect, *,
                       browser_open=webbrowser.open, transport=token_transport, timeout=300):
    """Start one local PKCE attempt; redirect and listener match reader rules."""
    from app.dropbox_windows import validate_redirect, _LocalServer, _CallbackHandler

    port, path = validate_redirect(configured_redirect, registered_redirect)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise OAuthError('INVALID_TIMEOUT')
    attempt = PublisherAuthAttempt(app_key, configured_redirect)
    try:
        server = _LocalServer(('127.0.0.1', port), _CallbackHandler)
    except OSError:
        attempt.cancel()
        raise OAuthError('CALLBACK_UNAVAILABLE') from None
    server.expected_host = f'127.0.0.1:{port}'
    server.callback_path = path
    server.completion_path = '/_utt_done_' + secrets.token_urlsafe(18)
    while server.completion_path == path:
        server.completion_path = '/_utt_done_' + secrets.token_urlsafe(18)
    server.attempt = attempt
    server.claimed = False
    server.finished = False
    server.callback_error = None
    deadline = time.monotonic() + timeout
    try:
        try:
            opened = browser_open(attempt.authorization_url())
        except Exception:
            raise OAuthError('BROWSER_UNAVAILABLE') from None
        if not opened:
            raise OAuthError('BROWSER_UNAVAILABLE')
        completion_deadline = None
        while True:
            now = time.monotonic()
            with server.state_lock:
                claimed, finished = server.claimed, server.finished
            if claimed and completion_deadline is None:
                completion_deadline = min(deadline, now + 10)
            if finished:
                break
            if completion_deadline is not None and now >= completion_deadline:
                raise OAuthError('COMPLETION_UNAVAILABLE')
            if now >= deadline:
                raise OAuthError('AUTH_TIMEOUT')
            server.timeout = min(0.2, deadline - now)
            server.handle_request()
        if server.callback_error:
            raise OAuthError(server.callback_error)
        server.server_close()
        return attempt.exchange(transport)
    finally:
        attempt.cancel()
        server.server_close()
