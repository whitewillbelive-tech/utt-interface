"""Explicit native D0 browsing in the approved runtime, never a browser API.

This layer calls an explicitly supplied D0Client. Only the current returned page
is retained by the session; detail results belong to the caller. It is NOT
UTT-ACCESS-READ/0.1.0: native offsets do not establish immutable
snapshots or cursors. Observed duplicates, ambiguous stringified IDs, changed
totals and short pages invalidate continuation. Equal-size swaps and other
unobserved source changes can still escape detection; consistency is always
UNVERIFIED. Restarting is a new native search, not recovery of an old snapshot.

No environment lookup, path discovery, automatic connection, synthetic fallback,
logging, browser exposure, export, source-body substitution or approval occurs.
Use only in the approved data runtime; native evidence must stay there. The
existing D0Client.read() boundary remains unsupported and is not called here.
"""
import copy
import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, field

from app.d0_adapter import D0Client, D0Error, D0Evidence, NATIVE_SORTS
from app.d0_projection import inspect_evidence


_SAFE_FAILURES = frozenset(('AUTH_REQUIRED', 'ACCESS_DENIED', 'INVALID_REQUEST',
                            'INVALID_RESPONSE', 'RESPONSE_TOO_LARGE', 'CONNECTION_UNAVAILABLE'))


class D0BrowseError(Exception):
    """Safe classification only; no query, address, ID, token or body retained."""
    def __init__(self, code, *, restart_required=False):
        self.code = code
        self.restart_required = restart_required
        super().__init__('Native D0 browse failed; restart the search.' if restart_required
                         else 'Native D0 browse request rejected.')


@dataclass(frozen=True, repr=False)
class D0BrowseCapture:
    """One attempted native call, with exact parameters and optional evidence."""
    kind: str
    _parameters: dict = field(repr=False)
    _evidence: D0Evidence | None = field(repr=False)
    error_code: str | None = None

    @property
    def parameters(self):
        return copy.deepcopy(self._parameters)

    @property
    def evidence(self):
        if self._evidence is None:
            return None
        item = self._evidence
        return D0Evidence(item.kind, item.raw_body, item.document)

    def __repr__(self):
        return ('D0BrowseCapture(kind=' + repr(self.kind) + ', evidence='
                + str(self._evidence is not None) + ', content=<private>)')


@dataclass(frozen=True, repr=False)
class D0BrowseResult:
    """Native response and structural inspection, with no semantic promotion."""
    _capture: D0BrowseCapture = field(repr=False)
    status: str
    consistency: str = field(default='UNVERIFIED', init=False)

    @property
    def parameters(self):
        return self._capture.parameters

    @property
    def evidence(self):
        return self._capture.evidence

    @property
    def document(self):
        return self._capture._evidence.document

    @property
    def raw_body(self):
        return self._capture._evidence.raw_body

    @property
    def inspection(self):
        return inspect_evidence(self._capture._evidence)

    def __repr__(self):
        return 'D0BrowseResult(status=' + repr(self.status) + ', consistency=UNVERIFIED, content=<private>)'


@dataclass(frozen=True, repr=False)
class D0BrowsePage(D0BrowseResult):
    offset: int
    limit: int
    total: int
    count: int
    has_next: bool

    def __repr__(self):
        return 'D0BrowsePage(consistency=UNVERIFIED, content=<private>)'


class D0BrowseSession:
    """Ordered native search -> offset pages -> current-page constrained detail.

    Pass the exact page returned by search/next_page to advance or open detail.
    A new search invalidates older pages. Calls are serialized; page objects are
    immutable and old/foreign/fabricated pages cannot select another query.
    Each returned page/detail contains its exact native query and evidence.
    There is no session-wide response collection or automatic content cache.
    Duplicate tracking retains bounded, fixed-size per-session HMAC digests;
    raw IDs from earlier pages are not retained in the seen set.
    """
    consistency = 'UNVERIFIED'

    def __init__(self, client, *, page_size=30, max_seen=100000):
        if (not isinstance(client, D0Client) or type(page_size) is not int
                or not 1 <= page_size <= 1000 or type(max_seen) is not int
                or not 1 <= max_seen <= 1000000):
            raise D0BrowseError('INVALID_REQUEST')
        self._client = client
        self._size = page_size
        self._max_seen = max_seen
        self._lock = threading.RLock()
        self._current = None
        self._query = None
        self._seen_key = secrets.token_bytes(32)
        self._seen = set()
        self._state = 'IDLE'

    @property
    def state(self):
        with self._lock:
            return self._state

    def __repr__(self):
        return 'D0BrowseSession(consistency=UNVERIFIED, content=<private>)'

    def _stop(self, code='RESTART_REQUIRED'):
        self._current = None
        self._state = 'RESTART_REQUIRED'
        raise D0BrowseError(code, restart_required=True)

    def _call(self, kind, parameters):
        evidence, failure = None, None
        try:
            if kind == 'records':
                evidence = self._client.records(**copy.deepcopy(parameters))
            else:
                evidence = self._client.detail(parameters['id'])
            # Inspect every successful native document; retain optional malformed
            # fields/NULL and issues as evidence rather than repairing them.
            inspect_evidence(evidence)
        except D0Error as exc:
            failure = exc.code if exc.code in _SAFE_FAILURES else 'CONNECTION_UNAVAILABLE'
        except Exception:
            failure = 'INVALID_RESPONSE'
        capture = D0BrowseCapture(kind, copy.deepcopy(parameters), evidence, failure)
        if failure is not None:
            self._current = None
            self._state = 'RESTART_REQUIRED'
            raise D0BrowseError(failure, restart_required=True) from None
        return capture

    def _require_current(self, page):
        if self._state == 'RESTART_REQUIRED':
            raise D0BrowseError('RESTART_REQUIRED', restart_required=True)
        if type(page) is not D0BrowsePage or page is not self._current:
            raise D0BrowseError('STALE_PAGE')

    def _page(self, offset, previous_total=None):
        parameters = {**self._query, 'offset': offset, 'limit': self._size}
        capture = self._call('records', parameters)
        document = capture._evidence.document
        rows, total = document['results'], document['total']
        count = len(rows)
        if (previous_total is not None and total != previous_total
                or offset + count > total
                or count < self._size and offset + count < total):
            self._stop()
        # D0 detail query stringifies native integer/string IDs. Reject observed
        # collisions including 1 vs '1', not merely identical Python values.
        # Store only fixed-size keyed digests, never variable-length raw IDs.
        # Strict UTF-8 rejects lone surrogates rather than replacing characters
        # and accidentally merging distinct native identifiers.
        try:
            identities = [hmac.new(self._seen_key, str(row['id']).encode('utf-8'),
                                   hashlib.sha256).digest() for row in rows]
        except (UnicodeError, ValueError):
            identities = None
        if identities is None:
            # Raise after leaving the handler so no private encoding exception
            # is attached as the public error's implicit exception context.
            self._stop('INVALID_RESPONSE')
        if len(identities) != len(set(identities)) or self._seen.intersection(identities):
            self._stop()
        if len(self._seen) + len(identities) > self._max_seen:
            self._stop('SEEN_LIMIT_REACHED')
        self._seen.update(identities)
        page = D0BrowsePage(capture, 'NATIVE_PAGE', offset, self._size, total,
                            count, offset + count < total)
        self._current = page
        self._state = 'ACTIVE' if page.has_next else 'EXHAUSTED'
        return page

    def search(self, *, q=None, tag=None, project=None, source=None, sort='priority'):
        """Start a new native query, preserving null/empty/whitespace exactly."""
        if (any(value is not None and not isinstance(value, str) for value in (q, tag, project, source))
                or not isinstance(sort, str) or sort not in NATIVE_SORTS):
            raise D0BrowseError('INVALID_REQUEST')
        with self._lock:
            self._current = None
            self._query = dict(q=q, tag=tag, project=project, source=source, sort=sort)
            self._seen = set()
            self._state = 'IDLE'
            return self._page(0)

    def next_page(self, page):
        with self._lock:
            self._require_current(page)
            if not page.has_next:
                raise D0BrowseError('NO_NEXT_PAGE')
            if len(self._seen) >= self._max_seen:
                self._stop('SEEN_LIMIT_REACHED')
            return self._page(page.offset + page.count, page.total)

    def detail(self, page, record_id):
        """Request raw detail for an exactly typed ID on the current page.

        The current-page constraint limits requested IDs; it does not establish
        the native response's identity semantics. An explicit canonical_record.id
        with a valid native ID that differs in type or
        value is conservatively rejected. That may reject legitimate different
        native identifiers until Work confirms the mapping. Missing/null/empty
        or malformed identity values remain untouched and unverified. Even an
        equal ID does not prove binding; alternative identity paths are unknown.
        Raw canonical/comparison/source evidence stays structurally UNVERIFIED.
        """
        with self._lock:
            self._require_current(page)
            if not self._client._valid_id(record_id) or not any(
                    type(row['id']) is type(record_id) and row['id'] == record_id
                    for row in page.document['results']):
                raise D0BrowseError('UNSEEN_RECORD')
            capture = self._call('detail', {'id': record_id})
            canonical = capture._evidence.document['canonical_record']
            if 'id' in canonical and self._client._valid_id(canonical['id']):
                identity = canonical['id']
                if type(identity) is not type(record_id) or identity != record_id:
                    self._stop('DETAIL_IDENTITY_CONFLICT')
            return D0BrowseResult(capture, 'IDENTITY_UNVERIFIED')
