"""Read providers: explicit transport injection and separate generated fiction."""
import copy
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from typing import Protocol
from fixtures.synthetic.generate import create, contract_examples
from app.validation import ResponseError, validate_response
from app.transport import TransportError

VERSION = 'UTT-ACCESS-READ/0.1.0'
OPERATIONS = ('capabilities', 'projects', 'search', 'detail', 'related_history')
FIELDS = {'contract_version', 'operation', 'snapshot_id', 'dataset_id', 'project_id',
          'record_id', 'query', 'document_role', 'review_status', 'page_size', 'cursor'}
SORTS = {'id_asc': 'asc', 'id_desc': 'desc'}
MESSAGES = {'INVALID_REQUEST': '잘못된 읽기 요청입니다.', 'UNSUPPORTED_CONTRACT': '지원하지 않는 계약입니다.',
            'VERSION_MISMATCH': '기준 버전 또는 페이지가 오래되었습니다. 처음부터 다시 조회하세요.',
            'CONNECTION_UNAVAILABLE': '읽기 공급자가 연결되지 않았습니다.',
            'ITEM_NOT_FOUND': '이 기준 버전에서 항목을 찾을 수 없습니다.',
            'SOURCE_MISSING': '이 기준 버전에서 출처를 찾을 수 없습니다.',
            'INTERNAL_ERROR': '읽기 공급자의 응답이 계약과 일치하지 않습니다.'}


def request(operation, **values):
    data = dict.fromkeys(FIELDS)
    data.update(contract_version=VERSION, operation=operation, page_size=3)
    data.update(values)
    return data


def error(operation, code):
    return dict(contract_version=VERSION, operation=operation if operation in OPERATIONS else 'capabilities',
                status='ERROR', snapshot=None, result=None,
                error=dict(code=code, message=MESSAGES[code], retryable=code == 'CONNECTION_UNAVAILABLE'))


def validate(q):
    if not isinstance(q, dict) or set(q) != FIELDS:
        return 'INVALID_REQUEST'
    if q['contract_version'] != VERSION:
        return 'UNSUPPORTED_CONTRACT'
    if q['operation'] not in OPERATIONS or type(q['page_size']) is not int or not 1 <= q['page_size'] <= 100:
        return 'INVALID_REQUEST'
    for k in FIELDS - {'contract_version', 'operation', 'page_size'}:
        if q[k] is not None and (not isinstance(q[k], str) or (k != 'query' and not q[k])):
            return 'INVALID_REQUEST'
    if q['operation'] != 'capabilities' and not (q['snapshot_id'] and q['dataset_id']):
        return 'INVALID_REQUEST'
    if q['operation'] in ('detail', 'related_history') and not q['record_id']:
        return 'INVALID_REQUEST'


class DataProvider(Protocol):
    def read(self, q: dict, sort: str = 'id_asc') -> dict: ...


class ReadOnlyAdapter:
    """Validate explicit transport output, without discovery, cache or fallback."""
    def __init__(self, transport=None):
        self.transport = transport

    def read(self, q, sort='id_asc'):
        return self.read_http(q, sort)[1]

    def read_http(self, q, sort='id_asc'):
        # Keep HTTP status per call; shared mutable status would race across users.
        code = validate(q)
        if code or not isinstance(sort, str) or sort not in SORTS:
            return 400, error(q.get('operation') if isinstance(q, dict) else None, code or 'INVALID_REQUEST')
        if self.transport is None:
            return 503, error(q['operation'], 'CONNECTION_UNAVAILABLE')
        try:
            response = self.transport(copy.deepcopy(q), sort)
            validate_response(response, q, sort)
            if response['status'] == 'ERROR':
                # Preserve the error code/retry policy, never upstream diagnostics.
                response = copy.deepcopy(response)
                response['error']['message'] = MESSAGES[response['error']['code']]
            return 200, response
        except ResponseError as exc:
            return 200, error(q['operation'], exc.code)
        except TransportError as exc:
            # The UI consumes business errors from the read envelope. Authentication
            # and connection failures retain their HTTP status outside that enum.
            status = exc.http_status if exc.http_status in (401, 403, 503) else 200
            return status, error(q['operation'], exc.code)
        except Exception:
            return 503, error(q['operation'], 'CONNECTION_UNAVAILABLE')


class SyntheticProvider:
    """Generated fiction with opaque process-local, expiring page handles.

    Handle state contains only offset, keyed context HMAC and issue/expiry times.
    No raw filters or client-readable fingerprint are embedded in the token.
    This cursor policy does not establish any native D0 snapshot guarantees.
    """
    _CURSOR_TTL_SECONDS = 15 * 60
    _MAX_CURSOR_HANDLES = 10000
    _CURSOR_HANDLE = re.compile(r'^[A-Za-z0-9_-]{43}$')

    class _CursorUnavailable(Exception):
        pass

    def __init__(self, copies=1):
        if type(copies) is not int or not 1 <= copies <= 10000:
            raise ValueError('copies must be 1..10000')
        self.lock = threading.RLock()
        self.key = secrets.token_bytes(32)
        self._cursors = {}
        self.examples = contract_examples(create())
        self.dataset = copy.deepcopy(self.examples['capabilities']['snapshot'])
        self.dataset['snapshot_id'] = f'synthetic-ui-v1-copies-{copies}'
        self.projects = self.examples['projects']['result']['projects']
        self.components = [dict(component_id='synthetic-component-reading', dataset_id=self.dataset['dataset_id'],
                                name='가상 읽기 구성요소', parent_component_id=None, relationship_status='OBSERVED')]
        self.component_relation = dict(relation_id='synthetic-component-project-link', from_id='synthetic-component-reading',
                                      to_id=self.projects[0]['project_id'], from_dataset_id=self.dataset['dataset_id'],
                                      to_dataset_id=self.dataset['dataset_id'], relation_kind='READING_CONTEXT',
                                      scope='PROJECT', assertion_status='OBSERVED', evidence_ids=[])
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.execute('CREATE TABLE records (id TEXT PRIMARY KEY COLLATE BINARY, title TEXT, role TEXT, review TEXT, search TEXT, data TEXT, base INTEGER)')
        self.db.execute('CREATE TABLE links (record_id TEXT, project_id TEXT)')
        self.db.execute('CREATE INDEX link_project ON links(project_id,record_id)')
        self.base = [r for k in ('search_page_1', 'search_page_2', 'search_page_3')
                     for r in self.examples[k]['result']['records']]
        # Separate, explicit UI fiction; the original fixtures remain unchanged.
        self.base[0]['source_refs'].append(dict(self.base[0]['source_refs'][0], source_id='synthetic-source-alternate'))
        for copy_index in range(copies):
            for i, original in enumerate(self.base):
                r = copy.deepcopy(original)
                if copy_index:
                    suffix = f'-copy-{copy_index:05d}'
                    r['record_id'] += suffix
                    r['title'] += f' · 합성 {copy_index}'
                    for link in r['project_relations']:
                        link['from_id'] = r['record_id']; link['relation_id'] += suffix
                body = r['raw_text']
                r.update(content_availability='NOT_REQUESTED', raw_text=None, raw_text_sha256=None)
                self.db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?)',
                                (r['record_id'], r['title'], r['document_role'], r['state']['review'],
                                 (r['title']+' '+(body or '')).casefold(), json.dumps(r, ensure_ascii=False), i))
                self.db.executemany('INSERT INTO links VALUES (?,?)', [(r['record_id'], l['to_id']) for l in r['project_relations']])
        self.db.commit()

    def close(self):
        with self.lock:
            self._cursors.clear()
            self.db.close()

    def _ok(self, op, result):
        return dict(contract_version=VERSION, operation=op, status='OK', snapshot=copy.deepcopy(self.dataset), result=copy.deepcopy(result), error=None)

    def _fingerprint(self, q, sort):
        raw = json.dumps({**q, 'cursor': None, 'ui_sort': sort}, sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        return hmac.digest(self.key, b'UTT-synthetic-cursor-context\0' + raw, 'sha256')

    def _expire_cursors(self, now):
        # Lookup, expiration and issuance run under self.lock. Only expired
        # entries are removed; live back-navigation handles are never evicted.
        for token, state in list(self._cursors.items()):
            if now >= state[3]:
                del self._cursors[token]

    def _cursor(self, offset, fingerprint):
        now = time.monotonic()
        self._expire_cursors(now)
        for token, state in self._cursors.items():
            if (state[2] <= now and state[0] == offset
                    and hmac.compare_digest(state[1], fingerprint)):
                # A replay does not consume capacity or renew its original TTL.
                return token
        if len(self._cursors) >= self._MAX_CURSOR_HANDLES:
            raise self._CursorUnavailable()
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            if token not in self._cursors:
                self._cursors[token] = (offset, fingerprint, now, now + self._CURSOR_TTL_SECONDS)
                return token
        # A failing/colliding entropy source must not overwrite another handle.
        raise self._CursorUnavailable()

    def _offset(self, cursor, fingerprint):
        if cursor is None:
            return 0
        try:
            if not self._CURSOR_HANDLE.fullmatch(cursor):
                raise ValueError()
            now = time.monotonic()
            self._expire_cursors(now)
            state = self._cursors.get(cursor)
            if (state is None or not state[2] <= now < state[3]
                    or not hmac.compare_digest(state[1], fingerprint)):
                raise ValueError()
            return state[0]
        except (ValueError, TypeError):
            raise ValueError('invalid cursor') from None

    def _page(self, q, sort, offset, has_more, mixed=False):
        fields = ['item_kind:asc', 'dataset_id:asc', 'item_id:asc'] if mixed else ['dataset_id:asc', 'record_id:'+SORTS[sort]]
        return dict(page_size=q['page_size'], sort=fields,
                    next_cursor=self._cursor(offset+q['page_size'], self._fingerprint(q, sort)) if has_more else None)

    def read(self, q, sort='id_asc'):
        code = validate(q)
        if code:
            return error(q.get('operation') if isinstance(q, dict) else None, code)
        op = q['operation']
        if not isinstance(sort, str) or sort not in SORTS:
            return error(op, 'INVALID_REQUEST')
        with self.lock:
            if q['dataset_id'] not in (None, self.dataset['dataset_id']):
                return error(op, 'VERSION_MISMATCH' if q['cursor'] is not None else 'ITEM_NOT_FOUND')
            if q['snapshot_id'] not in (None, self.dataset['snapshot_id']):
                return error(op, 'VERSION_MISMATCH')
            try:
                offset = self._offset(q['cursor'], self._fingerprint(q, sort))
            except ValueError:
                return error(op, 'VERSION_MISMATCH')
            try:
                return self._read(q, sort, offset)
            except self._CursorUnavailable:
                return error(op, 'CONNECTION_UNAVAILABLE')

    def _read(self, q, sort, offset):
        op = q['operation']; size = q['page_size']
        if op == 'capabilities':
            return self._ok(op, dict(dataset=self.dataset, operations=list(OPERATIONS), max_page_size=100,
                                     sort_order=['dataset_id:asc', 'record_id:'+SORTS[sort]], writes_supported=False))
        if op == 'projects':
            projects = [p for p in self.projects if q['project_id'] in (None, p['project_id'])]
            mixed = [('component', c['component_id'], c) for c in self.components
                     if q['project_id'] in (None, self.component_relation['to_id'])]
            mixed += [('project', p['project_id'], p) for p in projects]
            if q['project_id'] in (None, self.component_relation['to_id']):
                mixed += [('relation', self.component_relation['relation_id'], self.component_relation)]
            mixed.sort(key=lambda x: (x[0], x[1]))
            page = mixed[offset:offset+size]
            result = {name: [copy.deepcopy(v) for kind, _, v in page if kind == name[:-1]] for name in ('projects','components','relations')}
            result['page'] = self._page(q, sort, offset, offset+size < len(mixed), True)
            return self._ok(op, result)
        if op == 'search':
            where = ['1=1']; args = []
            for field, col in [('document_role', 'role'), ('review_status', 'review')]:
                if q[field] is not None:
                    where.append(f'{col} = ?'); args.append(q[field])
            if q['query']:
                where.append('instr(search, ?) > 0'); args.append(q['query'].casefold())
            if q['project_id']:
                where.append('id IN (SELECT record_id FROM links WHERE project_id=?)'); args.append(q['project_id'])
            rows = self.db.execute('SELECT data FROM records WHERE '+' AND '.join(where)+' ORDER BY id '+SORTS[sort]+' LIMIT ? OFFSET ?', args+[size+1, offset]).fetchall()
            return self._ok(op, dict(records=[json.loads(r[0]) for r in rows[:size]],
                                     page=self._page(q, sort, offset, len(rows)>size)))
        row = self.db.execute('SELECT data,base FROM records WHERE id=?', (q['record_id'],)).fetchone()
        if row is None:
            return error(op, 'ITEM_NOT_FOUND')
        record = json.loads(row[0]); original = self.base[row[1]]
        if op == 'detail':
            for key in ('content_availability', 'raw_text', 'raw_text_sha256'):
                record[key] = original[key]
            return self._ok(op, dict(record=record, related_projects=[p for p in self.projects if any(l['to_id']==p['project_id'] for l in record['project_relations'])], sources=record['source_refs']))
        # History belongs only to the fixture's sixth record; keep immutable events.
        mixed = []
        if row[1] == 5:
            suffix = record['record_id'][len(original['record_id']):]
            for event in self.examples['history']['result']['events']:
                event = copy.deepcopy(event)
                event['event_id'] += suffix; event['record_id'] = record['record_id']
                if event['supersedes']: event['supersedes'] += suffix
                mixed.append(('event', event['event_id'], event))
            peer_id = self.base[0]['record_id']+suffix
            peer = json.loads(self.db.execute('SELECT data FROM records WHERE id=?', (peer_id,)).fetchone()[0])
            mixed.append(('record', peer_id, peer))
        mixed.sort(key=lambda x: (x[0],x[1]))
        page = mixed[offset:offset+size]
        return self._ok(op, dict(related_records=[v for k,_,v in page if k=='record'], relations=[],
                                 events=[v for k,_,v in page if k=='event'], history_status='AVAILABLE',
                                 page=self._page(q, sort, offset, offset+size<len(mixed), True)))
