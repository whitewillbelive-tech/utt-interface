"""TEST-ONLY D0 HTTP fixture backed by one explicit temporary fictional file.

This is not an application provider, production launcher, native-schema model,
or source of snapshot guarantees. No import starts a server or opens a file.
The caller supplies a newly created test file marked SYNTHETIC_D0_ONLY. Every
data request reads that same file afresh so tests can exercise mutation, atomic
replacement, restart, duplicate pages and omitted rows. There is no directory
search, external connection, write endpoint, export, or logging.

Synthetic search policy is explicit: None/absent and '' disable a filter;
whitespace remains a literal query. q/source names use SQLite LIKE with escaped
backslash, percent and underscore (SQLite's default ASCII case behavior, no
Unicode normalization or casefold). tag/project use exact string membership;
source IDs use exact equality, independently of partial source-name matching.
These fixture choices are not independent confirmation of a native D0 server.
"""
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

MARKER = 'SYNTHETIC_D0_ONLY'
_FAULTS = frozenset(('normal', 'duplicate', 'omit', 'cross_page_duplicate', 'cross_page_omit'))


def fictional_payload():
    """Fresh fiction with native-looking raw fields, never copied source data."""
    records = []
    for number in range(1, 8):
        ident = f'synthetic-d0-{number:03d}'
        project = 'synthetic-project-a' if number % 2 else 'synthetic-project-b'
        tag = 'SYNTHETIC_ODD' if number % 2 else 'SYNTHETIC_EVEN'
        title = f'가상 D0 {number} Alpha'
        if number == 1:
            title += '  두칸 🧭'
        body = f'가상 본문 {number}: 실제 자료를 사용하지 않은 독립 시험 문자열.'
        if number == 1:
            body += ' literal 100% under_score path\\part  두칸'
        if number == 2:
            body += ' ASCII alphabet A_B and 100percent and path/part'
        source = {'source_id': f'synthetic-source-{1 if number < 4 else 2}',
                  'source_name': '가상 출처  A' if number < 4 else '가상 출처 B'}
        row = {'id': ident, 'title': title, 'priority': 8 - number,
               'state': 'SYNTHETIC_LOCAL', 'review_state': 'SYNTHETIC_UNVERIFIED',
               'project_text': project, 'tags': [tag], 'native_nullable': None,
               'native_empty': '', 'native_unmapped': {'index': number}}
        detail = {
            'canonical_record': {'id': ident, 'title': title, 'original_ids': [ident],
                                 'content': {'display_blocks': [
                                     {'text': body, 'original_fragment': None,
                                      'locator': {'target': 'SYNTHETIC_B_BODY', 'unit': 'native-unit',
                                                  'start': 0, 'end': len(body)},
                                      'transformations': []}]}},
            'comparison': {'body_comparison': 'SYNTHETIC_ONLY',
                           'a_current_remote_status': 'NOT_RUN', 'native_null': None},
            'stored_body': body,
            'sources': [
                {**source, 'availability': 'UNKNOWN', 'version': None,
                 'evidence': [{'evidence_id': 'synthetic-evidence', 'locators': [None, {'start': 0, 'end': 1}]},
                              {'evidence_id': 'synthetic-evidence', 'locators': []}]},
                {**source, 'source_name': '', 'availability': 'INACCESSIBLE', 'version': ''},
            ],
            'projects': [{'project_id': project, 'native_confirmation': 'SYNTHETIC_CANDIDATE'}],
            'b_relations': [{'record_id': ident, 'project_id': project,
                             'evidence_ids': ['synthetic-evidence', 'synthetic-evidence']}],
            'baseline_relations': [],
            'lineage': {'candidates': [{'parent': None}, {'parent': ''}]},
            'baseline_evidence': {'native_values': [None, '', False, 0]},
            'baseline_manuscript': None, 'baseline_segments': [],
            'native_unmapped': {'a/b': {'~field': [None, '', [], {}]}},
        }
        records.append({'row': row, 'detail': detail, 'tag_ids': [tag],
                        'project_ids': [project], 'source_refs': [source]})
    return {'fixture_marker': MARKER, 'records': records}


def _checked(payload):
    if (not isinstance(payload, dict) or payload.get('fixture_marker') != MARKER
            or set(payload) != {'fixture_marker', 'records'}
            or not isinstance(payload['records'], list) or len(payload['records']) > 10000):
        raise ValueError('Synthetic D0 fixture is invalid.')
    for item in payload['records']:
        if (not isinstance(item, dict)
                or set(item) != {'row', 'detail', 'tag_ids', 'project_ids', 'source_refs'}
                or not isinstance(item['row'], dict) or not isinstance(item['detail'], dict)
                or type(item['row'].get('id')) not in (str, int)
                or not isinstance(item['row'].get('title'), str)
                or not isinstance(item['detail'].get('canonical_record'), dict)
                or not isinstance(item['detail'].get('comparison'), dict)
                or not isinstance(item['detail'].get('stored_body'), (str, type(None)))
                or any(not isinstance(item[key], list) or any(not isinstance(value, str) for value in item[key])
                       for key in ('tag_ids', 'project_ids'))
                or not isinstance(item['source_refs'], list)
                or any(not isinstance(source, dict) or not isinstance(source.get('source_id'), str)
                       or source.get('source_name') is not None and not isinstance(source['source_name'], str)
                       for source in item['source_refs'])):
            raise ValueError('Synthetic D0 fixture is invalid.')
    return payload


def write_fixture(path, payload=None):
    """Write a caller-selected test file; parent must already exist."""
    payload = fictional_payload() if payload is None else copy.deepcopy(payload)
    _checked(payload)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class SyntheticD0Server:
    def __init__(self, path):
        self.path = Path(path)
        self._server = self._thread = None
        self._port = 0
        self._session = None
        self._lock = threading.RLock()
        self._requests = []
        self._fault = 'normal'
        self._fault_offset = None
        self._records_count = 0
        # Optional test hook, called before loading a records request's file.
        self.on_records_request = None

    def __repr__(self):
        return f'SyntheticD0Server(running={self._server is not None}, fixture=<fictional>)'

    @property
    def api_root(self):
        if self._server is None:
            raise RuntimeError('Synthetic D0 server is not running.')
        return f'http://127.0.0.1:{self._port}/api/'

    @property
    def requests(self):
        with self._lock:
            return copy.deepcopy(self._requests)

    def clear_requests(self):
        with self._lock:
            self._requests.clear()

    def _load(self):
        # Only this explicit fixture path is read. Symlinks are disallowed even
        # for tests so a test helper cannot follow an accidental fixture alias.
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 4194304:
            raise ValueError('Synthetic D0 fixture is invalid.')
        return _checked(json.loads(self.path.read_text(encoding='utf-8')))

    def mutate_file(self, callback):
        with self._lock:
            payload = copy.deepcopy(self._load())
            replacement = callback(payload)
            write_fixture(self.path, payload if replacement is None else replacement)

    def replace_file(self, payload=None):
        with self._lock:
            replacement = self._load() if payload is None else copy.deepcopy(payload)
            _checked(replacement)
            alternate = self.path.with_name(self.path.name + '.synthetic-replacement')
            try:
                write_fixture(alternate, replacement)
                os.replace(alternate, self.path)
            finally:
                alternate.unlink(missing_ok=True)

    def set_fault(self, mode='normal', *, offset=None):
        if mode not in _FAULTS or offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError('Synthetic D0 fault selection is invalid.')
        with self._lock:
            self._fault, self._fault_offset = mode, offset

    @staticmethod
    def _like(connection, value, needle):
        value = '' if value is None else str(value)
        escaped = needle.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        return bool(connection.execute("SELECT ? LIKE ? ESCAPE '\\'", (value, '%' + escaped + '%')).fetchone()[0])

    def _records(self, payload, query):
        allowed = {'q', 'tag', 'project', 'source', 'sort', 'offset', 'limit'}
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise ValueError('Invalid synthetic request.')
        value = lambda name: query.get(name, [''])[0]
        sort = query.get('sort', ['priority'])[0]
        offset, limit = int(query.get('offset', ['0'])[0]), int(query.get('limit', ['30'])[0])
        if sort not in ('priority', 'title', 'id', 'status') or not 0 <= offset or not 1 <= limit <= 1000:
            raise ValueError('Invalid synthetic request.')
        selected = []
        connection = sqlite3.connect(':memory:')
        try:
            for item in payload['records']:
                row, detail = item['row'], item['detail']
                if value('q') != '' and not any(self._like(connection, part, value('q'))
                        for part in (row['id'], row['title'], detail['stored_body'])):
                    continue
                if value('tag') != '' and value('tag') not in item['tag_ids']:
                    continue
                if value('project') != '' and value('project') not in item['project_ids']:
                    continue
                if value('source') != '' and not any(source['source_id'] == value('source')
                        or self._like(connection, source.get('source_name'), value('source'))
                        for source in item['source_refs']):
                    continue
                selected.append(item)
            # This ordering is an explicit fixture policy only. SQLite's BINARY
            # comparisons preserve the native JSON ID's text/integer distinction.
            connection.execute('CREATE TABLE fixture_order (idx INTEGER, ident, title TEXT, priority INTEGER, state TEXT)')
            connection.executemany('INSERT INTO fixture_order VALUES (?,?,?,?,?)',
                [(index, item['row']['id'], item['row']['title'], item['row'].get('priority', 0),
                  item['row'].get('state')) for index, item in enumerate(selected)])
            expression = {'id': 'ident ASC', 'title': 'title COLLATE BINARY ASC, ident ASC',
                          'priority': 'priority DESC, ident ASC', 'status': 'state COLLATE BINARY ASC, ident ASC'}[sort]
            order = [row[0] for row in connection.execute('SELECT idx FROM fixture_order ORDER BY ' + expression)]
            rows = [copy.deepcopy(selected[index]['row']) for index in order]
        finally:
            connection.close()
        page = rows[offset:offset + limit]
        with self._lock:
            fault = self._fault if self._fault_offset in (None, offset) else 'normal'
        if fault == 'duplicate' and len(page) > 1:
            page[1] = copy.deepcopy(page[0])
        elif fault == 'omit' and page:
            page.pop(0)
        elif fault == 'cross_page_duplicate' and offset > 0 and page:
            page[0] = copy.deepcopy(rows[offset - 1])
        elif fault == 'cross_page_omit' and offset > 0:
            page = rows[offset + 1:offset + limit + 1]
        return {'total': len(rows), 'results': page, 'fixture_marker': MARKER}

    def _dispatch(self, route, query):
        if route == 'records':
            with self._lock:
                self._records_count += 1
                count = self._records_count
            if self.on_records_request is not None:
                self.on_records_request(self, copy.deepcopy(query), count)
        payload = self._load()
        if route == 'records':
            return 200, self._records(payload, query)
        if route == 'detail':
            if set(query) != {'id'} or len(query['id']) != 1:
                return 400, {'error': 'INVALID_SYNTHETIC_REQUEST'}
            found = next((item for item in payload['records'] if str(item['row']['id']) == query['id'][0]), None)
            return (200, copy.deepcopy(found['detail'])) if found is not None else (404, {'error': 'SYNTHETIC_ITEM_ABSENT'})
        if route == 'summary':
            return 200, {'stats': {'records': len(payload['records'])}, 'fixture_marker': MARKER}
        if route == 'filters':
            tags = sorted({tag for item in payload['records'] for tag in item['tag_ids']})
            projects = sorted({project for item in payload['records'] for project in item['project_ids']})
            sources = [copy.deepcopy(source) for item in payload['records'] for source in item['source_refs']]
            return 200, {'tags': [{'tag': tag} for tag in tags],
                         'projects': [{'project_id': ident} for ident in projects], 'sources': sources}
        return 404, {'error': 'SYNTHETIC_ROUTE_ABSENT'}

    def start(self):
        if self._server is not None:
            raise RuntimeError('Synthetic D0 server is already running.')
        self._load()
        self._session = secrets.token_urlsafe(24)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, document):
                raw = json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                supplied = self.headers.get('X-UTT-D0-Session')
                authenticated = isinstance(supplied, str) and hmac.compare_digest(supplied, owner._session)
                with owner._lock:
                    # Request evidence deliberately excludes every auth value.
                    owner._requests.append({'path': parsed.path, 'query': copy.deepcopy(query),
                                            'authenticated': bool(authenticated)})
                if parsed.path == '/api/session':
                    return self.reply(200, {'session': owner._session})
                if not authenticated:
                    return self.reply(403, {'error': 'SYNTHETIC_SESSION_REQUIRED'})
                if not parsed.path.startswith('/api/'):
                    return self.reply(404, {'error': 'SYNTHETIC_ROUTE_ABSENT'})
                try:
                    status, document = owner._dispatch(parsed.path[len('/api/'):], query)
                except Exception:
                    status, document = 500, {'error': 'INVALID_SYNTHETIC_FIXTURE_OR_REQUEST'}
                self.reply(status, document)

            def do_POST(self):
                self.reply(405, {'error': 'SYNTHETIC_READ_ONLY'})

        server = ThreadingHTTPServer(('127.0.0.1', self._port), Handler)
        server.daemon_threads = True
        self._server, self._port = server, server.server_port
        self._thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
            self._server = self._thread = None
            self._session = None

    def restart(self):
        """Same loopback port, fresh private session; no source mutation."""
        self.stop()
        return self.start()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
