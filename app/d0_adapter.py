"""Explicit loopback client for the D0 protocol visible in static UI references.

This is a native D0 client, not a proven UTT-ACCESS-READ/0.1.0 adapter. The
reference proves session/records/detail/summary/filters GET calls and native
offset/limit paging. It does not prove an immutable dataset snapshot, stable
relation identities, a history projection, or independent source-body access.
No such properties are fabricated here, and read-contract integration fails
closed without making an HTTP request.

Construct only inside the approved data runtime. The explicit API root must use
HTTP, literal 127.0.0.1 and an explicit port. The API directory is supplied, never
discovered. Only the five evidenced routes below are available. No upstream body,
query, local session token, address or exception details are logged or returned
in exceptions. Successful native JSON is retained losslessly in memory through
D0Evidence; callers must not log or send that evidence outside its approved
runtime. There is no disk cache, export, write, redirect or retry path.
"""
import copy
import hashlib
import http.client
import json
import math
import re
import threading
from urllib.parse import urlencode, urlsplit

NATIVE_SORTS = frozenset(('priority', 'title', 'id', 'status'))
_ROUTES = frozenset(('session', 'records', 'detail', 'summary', 'filters'))
_SESSION_HEADER = 'X-UTT-D0-Session'


class D0ConfigError(ValueError):
    def __init__(self):
        super().__init__('Explicit D0 loopback configuration is missing or invalid.')


class D0Error(Exception):
    """Safe failure classification; never retains upstream diagnostics."""
    def __init__(self, code='CONNECTION_UNAVAILABLE', http_status=None):
        self.code = code
        self.http_status = http_status
        super().__init__('D0 local read failed.')


class D0Evidence:
    """Exact native response bytes plus a defensive parsed view, only in memory.

    This is evidence of one native response, not a dataset snapshot or permission
    to export its fields. Raw JSON members are neither dropped nor reclassified.
    The response hash identifies these bytes only; it is never a snapshot ID.
    """
    __slots__ = ('_kind', '_raw', '_document')

    def __init__(self, kind, raw, document):
        self._kind = kind
        self._raw = bytes(raw)
        self._document = copy.deepcopy(document)

    @property
    def kind(self):
        return self._kind

    @property
    def raw_body(self):
        return self._raw

    @property
    def document(self):
        return copy.deepcopy(self._document)

    @property
    def response_sha256(self):
        return hashlib.sha256(self._raw).hexdigest()

    def __repr__(self):
        return 'D0Evidence(kind=' + repr(self._kind) + ', content=<private>)'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _reject_number(_):
    raise ValueError()


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError()
    return result


def _status_failure(status):
    if status == 401:
        return D0Error('AUTH_REQUIRED', 401)
    if status == 403:
        return D0Error('ACCESS_DENIED', 403)
    # A bare 400/404 can describe a route or server problem. No native domain
    # meaning is established for those, redirects, conflicts or other failures.
    return D0Error('CONNECTION_UNAVAILABLE', status)


class D0Client:
    def __init__(self, api_root, timeout=10, max_response_bytes=8388608):
        try:
            if (not isinstance(api_root, str) or not api_root.isascii()
                    or any(ord(c) <= 32 or ord(c) == 127 for c in api_root)
                    or '?' in api_root or '#' in api_root or '\\' in api_root):
                raise ValueError()
            parsed = urlsplit(api_root)
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                    or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', parsed.netloc)
                    or parsed.port is None or not 1 <= parsed.port <= 65535
                    or not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)+', parsed.path)
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or type(timeout) not in (int, float) or not 0.1 <= timeout <= 60
                    or not math.isfinite(timeout)
                    or type(max_response_bytes) is not int
                    or not 1024 <= max_response_bytes <= 67108864):
                raise ValueError()
            self._port = parsed.port
            self._api_path = parsed.path
            self._timeout = timeout
            self._limit = max_response_bytes
            self._session = None
            self._lock = threading.RLock()
        except Exception:
            raise D0ConfigError() from None

    def reset_session(self):
        """Forget only this in-memory token; do not connect, revoke or retry."""
        with self._lock:
            self._session = None

    def _get(self, route, values=None, session=None):
        if route not in _ROUTES:
            raise D0Error('INVALID_REQUEST')
        connection = None
        try:
            query = urlencode(values or (), encoding='utf-8', errors='strict')
            if len(query.encode('ascii')) > 16384:
                raise D0Error('INVALID_REQUEST')
            path = self._api_path + route + ('?' + query if query else '')
            headers = {'Accept': 'application/json'}
            if session is not None:
                headers[_SESSION_HEADER] = session
            # Literal loopback only. http.client has no proxy, redirects,
            # cookie jar, automatic authentication, or implicit HTTP retries.
            connection = http.client.HTTPConnection('127.0.0.1', self._port,
                                                    timeout=self._timeout)
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise _status_failure(response.status)
            mime = response.getheader('Content-Type', '').split(';')[0].strip().lower()
            if mime != 'application/json' or response.getheader('Content-Encoding', 'identity') != 'identity':
                raise D0Error('INVALID_RESPONSE')
            declared = response.getheader('Content-Length')
            size = None if declared is None else int(declared)
            if size is not None and not 0 <= size <= self._limit:
                raise D0Error('RESPONSE_TOO_LARGE' if size > self._limit else 'INVALID_RESPONSE')
            raw = response.read(self._limit + 1)
            if len(raw) > self._limit:
                raise D0Error('RESPONSE_TOO_LARGE')
            if size is not None and len(raw) != size:
                raise D0Error('INVALID_RESPONSE')
            document = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                  parse_constant=_reject_number, parse_float=_finite_float)
            if not isinstance(document, dict):
                raise D0Error('INVALID_RESPONSE')
            return raw, document
        except D0Error:
            raise
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise D0Error('INVALID_RESPONSE') from None
        except Exception:
            raise D0Error() from None
        finally:
            if connection is not None:
                connection.close()

    def _request(self, route, values=None):
        with self._lock:
            if self._session is None:
                _, document = self._get('session')
                token = document.get('session')
                if (not isinstance(token, str) or not token or len(token) > 4096
                        or any(ord(c) <= 32 or ord(c) > 126 for c in token)):
                    raise D0Error('INVALID_RESPONSE')
                self._session = token
                # The bootstrap body/token is deliberately never an evidence item.
            raw, document = self._get(route, values, self._session)
            return D0Evidence(route, raw, document)

    def records(self, *, q=None, tag=None, project=None, source=None,
                sort='priority', offset=0, limit=30,
                document_role=None, review_status=None):
        """Get one native offset page without mapping, sorting or normalization.

        None omits a query key. Empty and whitespace values are sent byte-for-byte
        as UTF-8 query values; their confirmed D0 no-filter meaning belongs to the
        native server. Native tag/source are not aliases for role/review. Bounds
        below are client resource limits, not claims about server capabilities.
        """
        if (document_role is not None or review_status is not None
                or any(value is not None and not isinstance(value, str)
                       for value in (q, tag, project, source))
                or not isinstance(sort, str) or sort not in NATIVE_SORTS
                or type(offset) is not int or not 0 <= offset <= 9223372036854775807
                or type(limit) is not int or not 1 <= limit <= 1000):
            raise D0Error('INVALID_REQUEST')
        values = [(name, value) for name, value in
                  (('q', q), ('tag', tag), ('project', project), ('source', source))
                  if value is not None]
        values += [('sort', sort), ('offset', str(offset)), ('limit', str(limit))]
        evidence = self._request('records', values)
        doc = evidence.document
        if (type(doc.get('total')) is not int or doc['total'] < 0
                or not isinstance(doc.get('results'), list)
                or len(doc['results']) > limit or len(doc['results']) > doc['total']
                or any(not isinstance(row, dict) or not self._valid_id(row.get('id'))
                       for row in doc['results'])):
            raise D0Error('INVALID_RESPONSE')
        identities = [(type(row['id']), row['id']) for row in doc['results']]
        if len(identities) != len(set(identities)):
            raise D0Error('INVALID_RESPONSE')
        return evidence

    @staticmethod
    def _valid_id(value):
        # Preserve native JSON string or integer IDs; no synthetic stable ID is
        # manufactured. Boolean/object/null IDs cannot form the evidenced query.
        return (isinstance(value, str) and bool(value)) or type(value) is int

    def detail(self, record_id):
        if not self._valid_id(record_id):
            raise D0Error('INVALID_REQUEST')
        evidence = self._request('detail', [('id', str(record_id))])
        doc = evidence.document
        if not isinstance(doc.get('canonical_record'), dict) or not isinstance(doc.get('comparison'), dict):
            raise D0Error('INVALID_RESPONSE')
        # stored_body, locators, original fields, sources, lineage, proposals and
        # relations remain native JSON. Nothing is promoted to 0.1 semantics.
        return evidence

    def summary(self):
        evidence = self._request('summary')
        if not isinstance(evidence.document.get('stats'), dict):
            raise D0Error('INVALID_RESPONSE')
        return evidence

    def filters(self):
        evidence = self._request('filters')
        document = evidence.document
        if any(not isinstance(document.get(key), list) for key in ('tags', 'projects', 'sources')):
            raise D0Error('INVALID_RESPONSE')
        return evidence

    def read(self, q, sort='id_asc'):
        """Fail closed: native offset JSON cannot establish the draft guarantees."""
        from app.provider import error
        operation = q.get('operation') if isinstance(q, dict) else None
        return error(operation, 'UNSUPPORTED_CONTRACT')
