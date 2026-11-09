"""User-started Windows Dropbox OAuth; no file access or persisted credentials.

The two redirect values are supplied by the user; equality cannot prove what is
registered in Dropbox. Only the user can inspect that pre-existing setting.
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import socket
from socketserver import ThreadingMixIn
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from app.dropbox_auth import AuthAttempt, OAuthError, TOKEN_ENDPOINT, _redirect


def validate_redirect(configured: str, registered: str) -> tuple[int, str]:
    """Require exact explicit user-confirmed existing registration, no DNS."""
    if (not isinstance(configured, str) or not isinstance(registered, str)
            or len(configured) > 2048 or len(registered) > 2048):
        raise OAuthError('INVALID_REDIRECT')
    try:
        configured_bytes, registered_bytes = configured.encode('ascii'), registered.encode('ascii')
    except UnicodeError:
        raise OAuthError('INVALID_REDIRECT') from None
    if not hmac.compare_digest(configured_bytes, registered_bytes):
        raise OAuthError('REDIRECT_MISMATCH')
    _redirect(configured)
    uri = urlsplit(configured)
    if uri.hostname != '127.0.0.1' or uri.netloc != f'127.0.0.1:{uri.port}':
        raise OAuthError('INVALID_REDIRECT')
    return uri.port, uri.path or '/'


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def token_transport(endpoint: str, form: dict[str, str], *, connection_factory=None):
    """POST only to Dropbox's fixed TLS endpoint; no proxy or redirects."""
    if endpoint != TOKEN_ENDPOINT or set(form) != {'code', 'grant_type', 'client_id', 'redirect_uri', 'code_verifier'}:
        raise OAuthError('TOKEN_EXCHANGE_FAILED')
    factory = connection_factory or http.client.HTTPSConnection
    connection = None
    try:
        context = ssl.create_default_context()
        connection = factory('api.dropboxapi.com', 443, timeout=10, context=context)
        body = urlencode(form).encode('ascii')
        connection.request('POST', '/oauth2/token', body=body, headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
            'Cache-Control': 'no-store',
        })
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('HTTP error')
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError('response too large')
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique_json)
    except Exception:
        raise OAuthError('TOKEN_EXCHANGE_FAILED') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class _LocalServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        self.state_lock = threading.Lock()
        self.client_lock = threading.Lock()
        self.clients = set()
        super().__init__(*args, **kwargs)

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        conn, address = super().get_request()
        conn.settimeout(2)
        with self.client_lock:
            if len(self.clients) >= 8:
                conn.close()
                raise OSError('too many local clients')
            self.clients.add(conn)
        return conn, address

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self.client_lock:
                self.clients.discard(request)

    def server_close(self):
        super().server_close()
        with self.client_lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        # Never log paths, headers, callback parameters or exception traces.
        pass


class _CallbackHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'UTT'
    sys_version = ''

    def log_message(self, format, *args):
        pass

    def _respond(self, status, body=b'', location=None):
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        if location is not None:
            self.send_header('Location', location)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        self._respond(code)

    def do_GET(self):
        server = self.server
        hosts = self.headers.get_all('Host', [])
        if self.client_address[0] != '127.0.0.1' or hosts != [server.expected_host]:
            self._respond(403)
            return
        if self.path == server.completion_path:
            self._respond(200, b'<!doctype html><title>Complete</title><p>Return to the terminal.</p>')
            with server.state_lock:
                if server.claimed:
                    server.finished = True
            return
        if self.path.split('?', 1)[0] != server.callback_path:
            self._respond(404)
            return
        with server.state_lock:
            if not server.claimed:
                try:
                    server.attempt.accept_callback(self.path)
                    server.claimed = True
                except OAuthError as exc:
                    if exc.code in ('AUTH_CANCELLED', 'AUTH_FAILED', 'ATTEMPT_EXPIRED'):
                        server.claimed = True
                        server.callback_error = exc.code
        # No code or state in Location, body, log, or Referrer. Even malformed
        # callbacks are redirected away from their potentially sensitive query.
        self._respond(303, location=server.completion_path)

    def do_POST(self):
        self._respond(405)


def run_flow(app_key: str, configured_redirect: str, registered_redirect: str, *,
             browser_open=webbrowser.open, transport=token_transport, timeout=300):
    """Run one explicit attempt; all returned credentials stay in memory."""
    port, path = validate_redirect(configured_redirect, registered_redirect)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise OAuthError('INVALID_TIMEOUT')
    attempt = AuthAttempt(app_key, configured_redirect)
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
        # After a callback, wait briefly for the clean page; never hold the
        # listener open for the rest of the authorization timeout.
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
        # Stop accepting callbacks and interrupt incomplete local requests
        # before any token exchange is started.
        server.server_close()
        return attempt.exchange(transport)
    finally:
        attempt.cancel()
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Start a local read-only Dropbox authorization on Windows.')
    parser.add_argument('--diagnose', action='store_true', help='Test only the local callback and browser, without Dropbox.')
    options = parser.parse_args(argv)
    if sys.platform != 'win32':
        print('This command is available on Windows only.')
        return 2
    try:
        configured = input('Local redirect URI: ')
        if options.diagnose:
            validate_redirect(configured, configured)

            def local_browser(auth_url):
                state = parse_qs(urlsplit(auth_url).query)['state'][0]
                url = configured + '?' + urlencode({'state': state, 'code': 'synthetic-code'})
                return webbrowser.open(url)

            def synthetic_exchange(endpoint, form):
                return {'access_token': 'synthetic-token', 'token_type': 'bearer',
                        'scope': 'files.metadata.read files.content.read', 'expires_in': 60}

            print('Opening only a local synthetic callback in the browser; no Dropbox request will be sent.')
            session = run_flow('synthetic_key', configured, configured, browser_open=local_browser,
                               transport=synthetic_exchange, timeout=30)
            try:
                if not session.is_valid():
                    raise OAuthError('TOKEN_EXPIRED')
                print('Local callback and synthetic browser flow: PASS.')
            finally:
                session.close()
            return 0
        registered = input('Exact URI already registered in the Dropbox app: ')
        validate_redirect(configured, registered)
        app_key = getpass.getpass('Existing app key (local input): ')
        if input('Start authorization in the browser? Type START: ') != 'START':
            print('Cancelled.')
            return 2
        session = run_flow(app_key, configured, registered)
        try:
            if not session.is_valid():
                raise OAuthError('TOKEN_EXPIRED')
            print('Read-only scopes verified: files.metadata.read files.content.read')
            print('Token is valid for approximately', max(0, int(session.expires_at - time.monotonic())), 'seconds.')
        finally:
            session.close()
        return 0
    except KeyboardInterrupt:
        print('\nCancelled.')
        return 2
    except (EOFError, OAuthError) as exc:
        code = exc.code if isinstance(exc, OAuthError) else 'INPUT_CANCELLED'
        print('Authorization did not complete:', code)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
