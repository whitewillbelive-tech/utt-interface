"""Explicit server-side HTTPS transport for the draft read contract.

Importing this module reads no environment settings and opens no connection.
Only an approved runtime may call load_transport_from_env(). No credentials,
upstream bodies, addresses, or exception messages are included in failures.
"""
import json
import math
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ConfigError(ValueError):
    def __init__(self):
        super().__init__('Read transport configuration is missing or invalid.')


class TransportError(Exception):
    def __init__(self, code='CONNECTION_UNAVAILABLE', http_status=503):
        self.code = code
        self.http_status = http_status
        super().__init__('Read transport failed.')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint_valid(url):
    if not isinstance(url, str) or not url or not url.isascii():
        return False
    if any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
        return (parsed.scheme == 'https' and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and '?' not in url and '#' not in url
                and not parsed.netloc.endswith(':')
                and (port is None or 1 <= port <= 65535))
    except (TypeError, ValueError):
        return False


def _number_valid(timeout, max_response_bytes):
    return (type(timeout) in (int, float) and 0.1 <= timeout <= 60
            and math.isfinite(timeout)
            and type(max_response_bytes) is int
            and 1024 <= max_response_bytes <= 67108864)


def _http_failure(status):
    if status in (401, 403):
        return TransportError(http_status=status)
    # A bare 404 can mean a missing route, not a missing record. Domain errors
    # require a validated read envelope until a source status mapping is agreed.
    return TransportError()


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key')
        value[key] = item
    return value


def _nonfinite(_):
    raise ValueError('Nonfinite JSON number')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Nonfinite JSON number')
    return number


class HttpReadTransport:
    def __init__(self, url, auth_headers, timeout=10,
                 max_response_bytes=8388608, *, opener=None):
        if (not _endpoint_valid(url) or not callable(auth_headers)
                or not _number_valid(timeout, max_response_bytes)):
            raise ConfigError() from None
        self._url = url
        self._auth_headers = auth_headers
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        # Default TLS verification, system trust, and inherited proxies remain
        # enabled. Replacing only redirect handling prevents credential forwarding.
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    def __call__(self, q, sort='id_asc'):
        try:
            headers = self._auth_headers()
            if not isinstance(headers, dict) or not headers:
                raise ValueError()
            reserved = {'host', 'content-length', 'transfer-encoding',
                        'content-type', 'accept', 'connection', 'proxy-authorization'}
            for name, value in headers.items():
                if (not isinstance(name, str)
                        or not re.fullmatch(r'[A-Za-z0-9!#$%&\'*+.^_`|~-]+', name)
                        or name.lower() in reserved
                        or not isinstance(value, str) or not value
                        or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                    raise ValueError()
            body = json.dumps({'request': q, 'sort': sort},
                              ensure_ascii=False, allow_nan=False).encode('utf-8')
            req = Request(self._url, data=body,
                          headers={'Accept': 'application/json',
                                   'Content-Type': 'application/json', **headers},
                          method='POST')
            with self._opener.open(req, timeout=self._timeout) as response:
                status = response.status
                if status != 200:
                    raise _http_failure(status)
                if response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    raise TransportError()
                declared = response.headers.get('Content-Length')
                if declared is not None:
                    length = int(declared)
                    if length < 0 or length > self._max_response_bytes:
                        raise TransportError()
                raw = response.read(self._max_response_bytes + 1)
                if len(raw) > self._max_response_bytes:
                    raise TransportError()
                result = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                                    parse_constant=_nonfinite, parse_float=_finite_float)
                if not isinstance(result, dict):
                    raise TransportError()
                return result
        except HTTPError as exc:
            failure = _http_failure(exc.code)
            exc.close()
            raise failure from None
        except TransportError:
            raise
        except Exception:
            raise TransportError() from None


def load_transport_from_env(environ=None):
    """Load explicit runtime settings without discovering locations or connecting.

    The startup caller must explicitly enable this factory. The approval marker
    records that deployment choice; it does not itself grant network permission.
    Authentication is read again for each request so runtime token rotation works.
    """
    settings = os.environ if environ is None else environ
    try:
        if settings.get('UTT_RUNTIME_APPROVED') != '1':
            raise ValueError()
        url = settings['UTT_READ_URL']
        token = settings['UTT_READ_TOKEN']
        if (not isinstance(token, str) or not token
                or any(ord(c) <= 32 or ord(c) > 126 for c in token)):
            raise ValueError()
        timeout = float(settings.get('UTT_READ_TIMEOUT_SECONDS', '10'))
        limit = int(settings.get('UTT_READ_MAX_RESPONSE_BYTES', '8388608'))

        def auth_headers():
            current = settings.get('UTT_READ_TOKEN')
            if (not isinstance(current, str) or not current
                    or any(ord(c) <= 32 or ord(c) > 126 for c in current)):
                raise TransportError()
            return {'Authorization': 'Bearer ' + current}

        return HttpReadTransport(url, auth_headers, timeout, limit)
    except Exception:
        raise ConfigError() from None
