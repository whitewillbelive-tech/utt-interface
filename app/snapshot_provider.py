"""Read one explicitly selected normalized interchange snapshot, without writes.

UTT-NORMALIZED-SNAPSHOT/0.1.0 is a new interchange format, not the native D0
schema. No D0 field mapping or actual source compatibility is implied. The input
contains the existing read-contract objects; dataset omits snapshot_id, which is
derived from the exact file bytes. Every request checks those same bytes again.
This bounded, in-memory implementation makes no production-scale claim.
Opaque identifiers must be supplied as safe identifiers, never operational
addresses, paths or credentials. No native identifier mapping is inferred.

Search uses NFKC + casefold substring matching over title, display_summary and
AVAILABLE raw_text. Null, empty and normalized whitespace queries do not filter.
Document/review filters have no approved native mapping and are rejected. Project
filtering requires complete AVAILABLE, USER_CONFIRMED local membership metadata.

Pagination uses process-local random 256-bit handles. Only the offset, a keyed
context HMAC and monotonic issue/expiry times stay in the bounded handle store;
no query/filter text or client-readable fingerprint is put in a cursor. Handles
are reusable for 15 minutes without renewing their expiry. At 10,000 live handles
new contexts/pages fail explicitly with CONNECTION_UNAVAILABLE until entries
expire; existing handles are not evicted to make room. These cursor guarantees
apply to this captured normalized image, not native D0 snapshot/ABA detection.
"""
import copy
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator
from app.provider import VERSION, OPERATIONS, SORTS, error, validate
from app.validation import _SCHEMA, validate_response

FORMAT = 'UTT-NORMALIZED-SNAPSHOT/0.1.0'
_BASE_FIELDS = {'format', 'dataset', 'records', 'projects', 'components', 'relations', 'history'}
_KINDS = {'record': 'record_id', 'project': 'project_id', 'component': 'component_id',
          'relation': 'relation_id', 'history': 'event_id'}
_VALIDATORS = {kind: Draft202012Validator({'$ref': '#/$defs/' + kind,
                                        '$defs': _SCHEMA['$defs']})
               for kind in (*_KINDS, 'dataset')}
_TARGET = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
_CURSOR_HANDLE = re.compile(r'^[A-Za-z0-9_-]{43}$')
_CURSOR_TTL_SECONDS = 15 * 60
_MAX_CURSOR_HANDLES = 10000


@dataclass(frozen=True, slots=True, repr=False)
class _CursorState:
    offset: int
    context_hmac: bytes
    issued_at: float
    expires_at: float


class SnapshotError(Exception):
    def __init__(self, code='CONNECTION_UNAVAILABLE'):
        self.code = code
        super().__init__('Normalized read snapshot is unavailable or invalid.')


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _reject_constant(_):
    raise ValueError()


def _normalize(text):
    return unicodedata.normalize('NFKC', text).casefold()


def _nonempty(value):
    return isinstance(value, str) and bool(value)


class SnapshotProvider:
    def __init__(self, path, max_input_bytes=33554432):
        self._lock = threading.RLock()
        self._key = secrets.token_bytes(32)
        self._cursors = {}
        self._stale = False
        try:
            if (not isinstance(path, (str, os.PathLike)) or not os.fspath(path)
                    or type(max_input_bytes) is not int
                    or not 1024 <= max_input_bytes <= 67108864):
                raise ValueError()
            # Resolve only the explicitly configured lexical path. Never inspect
            # parent directories, infer a filename, or search for another file.
            self._path = Path(os.path.abspath(os.fspath(path)))
            self._max_input_bytes = max_input_bytes
            raw = self._read_bytes()
            self._digest = hashlib.sha256(raw).hexdigest()
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                 parse_constant=_reject_constant)
            self._load(payload)
        except SnapshotError:
            raise
        except Exception:
            raise SnapshotError('INVALID_REQUEST') from None

    def _read_bytes(self):
        descriptor = None
        try:
            # Nonblocking + regular-file check avoids hanging on FIFOs/devices.
            # O_NOFOLLOW also rejects a final symlink where the OS supports it.
            if self._path.is_symlink():
                raise OSError()
            flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
            flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            descriptor = os.open(self._path, flags)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > self._max_input_bytes:
                raise OSError()
            with os.fdopen(descriptor, 'rb') as handle:
                descriptor = None
                raw = handle.read(self._max_input_bytes + 1)
                after = os.fstat(handle.fileno())
            current = self._path.stat(follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if (len(raw) > self._max_input_bytes or identity(before) != identity(after)
                    or identity(after) != identity(current)):
                raise OSError()
            return raw
        except Exception:
            raise SnapshotError() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _load(self, payload):
        if (not isinstance(payload, dict) or not _BASE_FIELDS <= set(payload)
                or set(payload) - _BASE_FIELDS - {'source_contents'}
                or payload['format'] != FORMAT):
            raise ValueError()
        descriptor = payload['dataset']
        if not isinstance(descriptor, dict) or 'snapshot_id' in descriptor:
            raise ValueError()
        self._dataset = {**descriptor, 'snapshot_id': 'sha256:' + self._digest}
        if not _VALIDATORS['dataset'].is_valid(self._dataset):
            raise ValueError()
        dataset = self._dataset['dataset_id']
        self._records = self._items(payload['records'], 'record', dataset)
        self._projects = self._items(payload['projects'], 'project', dataset)
        self._components = self._items(payload['components'], 'component', dataset)
        self._relations = self._items(payload['relations'], 'relation', dataset)
        # Different local entity kinds must not compete for the same endpoint ID.
        identities = [*self._records, *self._projects, *self._components]
        if len(identities) != len(set(identities)):
            raise ValueError()
        known_relations = dict(self._relations)
        for component in self._components.values():
            if (component['parent_component_id'] is not None
                    and component['parent_component_id'] not in self._components):
                raise ValueError()
        for record in self._records.values():
            refs = record['source_refs']
            if (len({s['source_id'] for s in refs}) != len(refs)
                    or any(s['dataset_id'] != dataset for s in refs)):
                raise ValueError()
            if any(s['location'] is not None and not _TARGET.fullmatch(s['location']['target']) for s in refs):
                # A locator target is a safe symbolic identifier, never a path.
                # Reject incompatible input instead of silently changing origin.
                raise ValueError()
            if (record['raw_text_sha256'] is not None
                    and hashlib.sha256(record['raw_text'].encode('utf-8')).hexdigest()
                    != record['raw_text_sha256']):
                raise ValueError()
            for link in record['project_relations']:
                if not self._anchored(link, record['record_id']):
                    raise ValueError()
                if link['relation_id'] in known_relations and known_relations[link['relation_id']] != link:
                    raise ValueError()
                known_relations[link['relation_id']] = link
        if not isinstance(payload['history'], list):
            raise ValueError()
        self._history = {}
        event_ids = set()
        for item in payload['history']:
            if (not isinstance(item, dict)
                    or set(item) != {'record_id', 'history_status', 'related_record_ids', 'relations', 'events'}
                    or item['record_id'] not in self._records
                    or item['record_id'] in self._history
                    or item['history_status'] not in ('AVAILABLE', 'MISSING', 'UNKNOWN', 'INACCESSIBLE')
                    or not isinstance(item['related_record_ids'], list)
                    or any(not isinstance(i, str) or i not in self._records for i in item['related_record_ids'])
                    or len(set(item['related_record_ids'])) != len(item['related_record_ids'])):
                raise ValueError()
            events = self._items(item['events'], 'history', dataset)
            if event_ids.intersection(events):
                raise ValueError()
            event_ids.update(events)
            relations = self._items(item['relations'], 'relation', dataset)
            if any(event['record_id'] != item['record_id'] for event in events.values()):
                raise ValueError()
            for link in relations.values():
                if not self._anchored(link, item['record_id']):
                    raise ValueError()
                if link['relation_id'] in known_relations and known_relations[link['relation_id']] != link:
                    raise ValueError()
                known_relations[link['relation_id']] = link
            self._history[item['record_id']] = item
        local_ids = set(identities) | event_ids
        local_ids.update(s['source_id'] for record in self._records.values() for s in record['source_refs'])
        for link in known_relations.values():
            if any(link[prefix + '_dataset_id'] == dataset and link[prefix + '_id'] not in local_ids
                   for prefix in ('from', 'to')):
                raise ValueError()
        self._source_contents = self._load_sources(payload.get('source_contents', []))
        self._search_text = {ident: '\n'.join(_normalize(part) for part in
                            (r['title'], r['display_summary'] or '', r['raw_text'] or ''))
                             for ident, r in self._records.items()}

    def _items(self, values, kind, dataset):
        if not isinstance(values, list):
            raise ValueError()
        result = {}
        for item in values:
            if not _VALIDATORS[kind].is_valid(item):
                raise ValueError()
            ident = item[_KINDS[kind]]
            if ident in result:
                raise ValueError()
            if kind == 'relation':
                if not any(item[key] == dataset for key in ('from_dataset_id', 'to_dataset_id')):
                    raise ValueError()
            elif item['dataset_id'] != dataset:
                raise ValueError()
            result[ident] = item
        return result

    def _load_sources(self, values):
        from app.source_read import VERSION as SOURCE_VERSION, source_result
        if not isinstance(values, list):
            raise ValueError()
        result = {}
        for item in values:
            if (not isinstance(item, dict)
                    or set(item) != {'record_id', 'source_id', 'source_version', 'content'}
                    or any(not _nonempty(item[k]) for k in ('record_id', 'source_id', 'source_version'))
                    or item['record_id'] not in self._records):
                raise ValueError()
            key = (item['record_id'], item['source_id'], item['source_version'])
            source = next((s for s in self._records[item['record_id']]['source_refs']
                           if s['source_id'] == item['source_id'] and s['version'] == item['source_version']), None)
            content = item['content']
            if key in result or source is None or not isinstance(content, dict):
                raise ValueError()
            q = {'contract_version': SOURCE_VERSION,
                 'dataset_id': self._dataset['dataset_id'], 'snapshot_id': self._dataset['snapshot_id'],
                 'record_id': item['record_id'], 'source_id': item['source_id'],
                 'source_version': item['source_version']}
            source_result(q, source, content)
            if content['availability'] == 'AVAILABLE' and source['availability'] != 'AVAILABLE':
                raise ValueError()
            result[key] = content
        return result

    def _anchored(self, link, ident):
        dataset = self._dataset['dataset_id']
        return ((link['from_dataset_id'], link['from_id']) == (dataset, ident)
                or (link['to_dataset_id'], link['to_id']) == (dataset, ident))

    def _ensure_current(self):
        if self._stale:
            raise SnapshotError('VERSION_MISMATCH')
        try:
            raw = self._read_bytes()
        except Exception:
            # Observed loss of the selected input invalidates this captured
            # session, even when identical bytes later reappear at the path.
            # Preserve the first safe connection error; subsequent reads must
            # explicitly restart instead of silently resuming an old cursor.
            self._stale = True
            raise
        if hashlib.sha256(raw).hexdigest() != self._digest:
            self._stale = True
            raise SnapshotError('VERSION_MISMATCH')

    def _ok(self, operation, result):
        return dict(contract_version=VERSION, operation=operation, status='OK',
                    snapshot=copy.deepcopy(self._dataset), result=copy.deepcopy(result), error=None)

    def _fingerprint(self, q, sort):
        raw = json.dumps({**q, 'cursor': None, 'sort': sort}, sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        return hmac.digest(self._key, b'UTT-normalized-cursor-context\0' + raw, 'sha256')

    def _expire_cursors(self, now):
        # All callers hold _lock. Expiration is the only automatic removal;
        # capacity must never evict a still-valid back/previous-page handle.
        for token, state in list(self._cursors.items()):
            if now >= state.expires_at:
                del self._cursors[token]

    def _cursor(self, offset, fingerprint):
        now = time.monotonic()
        self._expire_cursors(now)
        # Repeated/back navigation to the same page reuses its live handle. It
        # does not consume capacity or extend the original expiry.
        for token, state in self._cursors.items():
            if (state.issued_at <= now and state.offset == offset
                    and hmac.compare_digest(state.context_hmac, fingerprint)):
                return token
        if len(self._cursors) >= _MAX_CURSOR_HANDLES:
            raise SnapshotError('CONNECTION_UNAVAILABLE')
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            if token not in self._cursors:
                self._cursors[token] = _CursorState(offset, fingerprint, now,
                                                    now + _CURSOR_TTL_SECONDS)
                return token
        raise SnapshotError('CONNECTION_UNAVAILABLE')

    def _offset(self, q, sort):
        if q['cursor'] is None:
            return 0
        try:
            token = q['cursor']
            if not _CURSOR_HANDLE.fullmatch(token):
                raise ValueError()
            now = time.monotonic()
            self._expire_cursors(now)
            state = self._cursors.get(token)
            if (state is None or not state.issued_at <= now < state.expires_at
                    or not hmac.compare_digest(state.context_hmac, self._fingerprint(q, sort))):
                raise ValueError()
            if state.offset < 0 or state.offset % q['page_size']:
                raise ValueError()
            return state.offset
        except Exception:
            raise SnapshotError('VERSION_MISMATCH') from None

    def _page(self, q, sort, offset, total):
        return {'page_size': q['page_size'],
                'sort': (['dataset_id:asc', 'record_id:' + SORTS[sort]] if q['operation'] == 'search'
                         else ['item_kind:asc', 'dataset_id:asc', 'item_id:asc']),
                'next_cursor': (self._cursor(offset + q['page_size'], self._fingerprint(q, sort))
                                if offset + q['page_size'] < total else None)}

    @staticmethod
    def _listed(record):
        result = copy.deepcopy(record)
        result.update(content_availability='NOT_REQUESTED', raw_text=None, raw_text_sha256=None)
        return result

    def _scope(self, q):
        if q['dataset_id'] not in (None, self._dataset['dataset_id']):
            raise SnapshotError('VERSION_MISMATCH' if q.get('cursor') is not None else 'ITEM_NOT_FOUND')
        if q['snapshot_id'] not in (None, self._dataset['snapshot_id']):
            raise SnapshotError('VERSION_MISMATCH')

    def read(self, q, sort='id_asc'):
        operation = q.get('operation') if isinstance(q, dict) else None
        try:
            code = validate(q)
            if (code or not isinstance(sort, str) or sort not in SORTS
                    or q['document_role'] is not None or q['review_status'] is not None):
                return error(operation, code or 'INVALID_REQUEST')
            with self._lock:
                self._ensure_current()
                self._scope(q)
                result = self._read(q, sort)
                validate_response(result, q, sort)
                return result
        except SnapshotError as exc:
            return error(operation, exc.code)
        except Exception:
            return error(operation, 'INTERNAL_ERROR')

    def _read(self, q, sort):
        operation = q['operation']
        if operation == 'capabilities':
            if any(q[k] is not None for k in ('project_id', 'record_id', 'query', 'cursor')):
                raise SnapshotError('INVALID_REQUEST')
            return self._ok(operation, {'dataset': self._dataset, 'operations': list(OPERATIONS),
                                       'max_page_size': 100, 'writes_supported': False,
                                       'sort_order': ['dataset_id:asc', 'record_id:' + SORTS[sort]]})
        offset = self._offset(q, sort)
        size = q['page_size']
        if operation == 'search':
            if q['record_id'] is not None:
                raise SnapshotError('INVALID_REQUEST')
            query = None if q['query'] is None else _normalize(q['query'])
            query = None if query is None or not query.strip() else query
            rows = list(self._records.values())
            if q['project_id'] is not None:
                project = q['project_id']
                if project not in self._projects:
                    raise SnapshotError('INVALID_REQUEST')
                for record in rows:
                    if (record['project_relations_status'] != 'AVAILABLE'
                            or any(link['assertion_status'] != 'USER_CONFIRMED'
                                   for link in record['project_relations'])):
                        raise SnapshotError('INVALID_REQUEST')
                rows = [record for record in rows if any(self._anchored(link, project)
                        for link in record['project_relations'])]
            if query is not None:
                rows = [r for r in rows if query in self._search_text[r['record_id']]]
            rows.sort(key=lambda r: r['record_id'], reverse=sort == 'id_desc')
            return self._ok(operation, {'records': [self._listed(r) for r in rows[offset:offset + size]],
                                       'page': self._page(q, sort, offset, len(rows))})
        if operation == 'projects':
            if q['query'] is not None or q['record_id'] is not None:
                raise SnapshotError('INVALID_REQUEST')
            projects = list(self._projects.values())
            relations = list(self._relations.values())
            components = list(self._components.values())
            if q['project_id'] is not None:
                if q['project_id'] not in self._projects:
                    raise SnapshotError('ITEM_NOT_FOUND')
                projects = [self._projects[q['project_id']]]
                relations = [r for r in relations if self._anchored(r, q['project_id'])]
                components = [c for c in components if any(self._anchored(r, c['component_id']) for r in relations)]
            mixed = [('component', c['component_id'], c) for c in components]
            mixed += [('project', p['project_id'], p) for p in projects]
            mixed += [('relation', r['relation_id'], r) for r in relations]
            mixed.sort(key=lambda row: (row[0], row[1]))
            page = mixed[offset:offset + size]
            return self._ok(operation, {**{key: [item for kind, _, item in page if kind == key[:-1]]
                                          for key in ('components', 'projects', 'relations')},
                                       'page': self._page(q, sort, offset, len(mixed))})
        record = self._records.get(q['record_id'])
        if record is None:
            raise SnapshotError('ITEM_NOT_FOUND')
        if q['project_id'] is not None or q['query'] is not None:
            raise SnapshotError('INVALID_REQUEST')
        if operation == 'detail':
            if q['cursor'] is not None:
                raise SnapshotError('INVALID_REQUEST')
            projects = [p for p in self._projects.values()
                        if any(self._anchored(link, p['project_id']) for link in record['project_relations'])]
            projects.sort(key=lambda p: p['project_id'])
            return self._ok(operation, {'record': record, 'related_projects': projects,
                                       'sources': record['source_refs']})
        history = self._history.get(record['record_id'])
        status = 'UNKNOWN' if history is None else history['history_status']
        mixed = []
        if history is not None:
            mixed += [('event', e['event_id'], e) for e in history['events']]
            mixed += [('record', ident, self._listed(self._records[ident])) for ident in history['related_record_ids']]
            mixed += [('relation', link['relation_id'], link) for link in history['relations']]
        mixed.sort(key=lambda row: (row[0], row[1]))
        page = mixed[offset:offset + size]
        return self._ok(operation, {'related_records': [item for kind, _, item in page if kind == 'record'],
                                   'relations': [item for kind, _, item in page if kind == 'relation'],
                                   'events': [item for kind, _, item in page if kind == 'event'],
                                   'history_status': status, 'page': self._page(q, sort, offset, len(mixed))})

    def read_source(self, q):
        from app.source_read import source_error, source_result, validate_source_request
        code = validate_source_request(q)
        if code:
            return source_error(code)
        try:
            with self._lock:
                self._ensure_current()
                self._scope(q)
                record = self._records.get(q['record_id'])
                if record is None:
                    return source_error('ITEM_NOT_FOUND')
                source = next((s for s in record['source_refs'] if s['source_id'] == q['source_id']), None)
                if source is None:
                    return source_error('SOURCE_MISSING' if record['source_status'] in ('AVAILABLE', 'MISSING')
                                        else 'NOT_SUPPORTED')
                if source['version'] != q['source_version']:
                    return source_error('VERSION_MISMATCH')
                content = self._source_contents.get((q['record_id'], q['source_id'], q['source_version']))
                if content is None:
                    return source_error('NOT_SUPPORTED')
                return source_result(q, copy.deepcopy(source), copy.deepcopy(content))
        except SnapshotError as exc:
            return source_error(exc.code)
        except Exception:
            return source_error('INTERNAL_ERROR')
