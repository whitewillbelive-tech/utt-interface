"""User-started background publication for an already selected workspace.

The controller owns no token storage and does not provide Dropbox calls to
the browser. A click initiates a separate short-lived write grant. Runtime
scope checks and the native publisher perform all actual publication checks.
"""
from __future__ import annotations

import threading

from app.dropbox_auth import _APP_KEY_RE
from app.dropbox_windows import validate_redirect
from app.publisher_auth import run_publisher_flow
from app.publisher_transport import PublisherTransport

_STATE = 'UTT-WORKSPACE-PUBLISH/0.1'
# Explicit, non-sensitive codes from the in-process publisher/reader/transport.
# Never return a Dropbox error body, filesystem path, token or raw exception.
_PARENT_COMPARISON_CODES = frozenset({
    'CHECK_FAILED', 'DATA_REVISION_CONFLICT', 'DUPLICATE_TABLE', 'EVENT_CHAIN_CONFLICT',
    'EVENT_HISTORY_CONFLICT', 'EVENT_ORDER_CONFLICT', 'EVENT_SCHEMA_UNSUPPORTED', 'FOREIGN_KEY_CONFLICT',
    'FTS_LOGICAL_MISSING', 'FTS_LOGICAL_UNSUPPORTED', 'FTS_SHADOW_UNSUPPORTED', 'INVALID_CATALOG',
    'INVALID_INPUT', 'INVALID_JSON', 'INVALID_LOCAL_CELL', 'INVALID_LOCAL_SCHEMA',
    'INVALID_MANIFEST', 'INVALID_SHARD', 'INVALID_SHARD_STREAM', 'INVALID_TYPED_CELL',
    'INVALID_TYPED_ROW', 'INVALID_WORK_RECORD', 'INVALID_WORK_SCHEMA', 'LIMIT_EXCEEDED',
    'MANIFEST_MISMATCH', 'OBJECT_SCHEMA_MISMATCH', 'PARENT_ROW_CONFLICT', 'PARENT_TABLE_MISSING',
    'PROJECT_LINK_CONFLICT', 'PROJECT_LINK_EVENT_CONFLICT', 'PROJECT_LINK_SCHEMA_UNSUPPORTED', 'RELATION_CONFLICT',
    'RELATION_EVENT_CONFLICT', 'RELATION_SCHEMA_UNSUPPORTED', 'REVISION_EVENT_CONFLICT', 'REVISION_EVENT_UNSUPPORTED',
    'SEQUENCE_CONFLICT', 'SHARD_INTEGRITY_MISMATCH', 'SHARD_SIZE_MISMATCH', 'SNAPSHOT_REQUIRED',
    'SQLITE_INTEGRITY_CONFLICT', 'TABLE_COUNT_MISMATCH', 'TABLE_SCHEMA_MISMATCH', 'UNSUPPORTED_CELL_ENCODING',
    'UNSUPPORTED_ROWID', 'UNSUPPORTED_TABLE_DESCRIPTOR', 'WORK_FTS_MISMATCH', 'WORK_FTS_UNSUPPORTED',
    'WORK_META_CONFLICT', 'WORK_RECORD_DELETED', 'WORK_REVISION_CONFLICT', 'WORK_REVISION_GAP',
    'WORK_REVISION_MISMATCH', 'WORK_UNTRACKED_CHANGE', 'WORK_VERSION_CONFLICT',
})
_DIRECT_ERRORS = frozenset({
    'ACCESS_DENIED', 'ACTIVATION_NOT_CONFIRMED', 'ACTIVATION_UNCERTAIN', 'ACTIVATION_UNKNOWN',
    'ACTIVE_CHANGED', 'ACTIVE_DB_UNVERIFIED', 'ACTIVE_MISSING', 'AUTH_EXPIRED',
    'AUTH_FAILED', 'AUTH_REQUIRED', 'CATALOG_MISMATCH', 'COMMITTED_GENERATION_DAMAGED',
    'COMMITTED_GENERATION_IMMUTABLE', 'COMMIT_MISMATCH', 'COMMIT_MISSING', 'CONFLICT',
    'CONNECTION_UNAVAILABLE', 'CONTROL_LIMIT', 'DATA_REVISION_CONFLICT', 'FOREIGN_KEY_FAILED',
    'HISTORY_TABLES_MISSING', 'INTEGRITY_FAILED', 'INVALID_ACTIVE', 'INVALID_COMMIT',
    'INVALID_CONTROL_VALUE', 'INVALID_MANIFEST', 'INVALID_NUMBER', 'INVALID_OVERLAY',
    'INVALID_PAYLOAD', 'INVALID_REQUEST', 'INVALID_SOURCE', 'INVALID_TEMP_DIRECTORY',
    'INVALID_TEXT', 'INVALID_TIMEOUT', 'INVALID_WORKSPACE', 'LEGACY_PIN_MISMATCH',
    'LIMIT_EXCEEDED', 'LOCAL_TEMP_UNAVAILABLE', 'MANIFEST_MISMATCH', 'MANIFEST_MISSING',
    'NATIVE_PAYLOAD_MISSING', 'PARENT_CATALOG_UNSUPPORTED', 'PARENT_CHANGED', 'PARENT_MANIFEST_CHANGED',
    'PARENT_MANIFEST_MISSING', 'PARENT_PAYLOAD_CONFLICT', 'PARENT_SHARD_MISSING', 'PATH_INVALID',
    'PAYLOAD_MISMATCH', 'PAYLOAD_NOT_DECLARED', 'PLAN_INVALID', 'PLAN_REQUIRED',
    'PROTECTED_TEMP_REQUIRED', 'PUBLICATION_FAILED', 'PUBLISHED_LOCAL_CHANGED', 'PUBLISHER_CLOSED',
    'PUBLISH_FAILED', 'RATE_LIMITED', 'READER_SIZE_UNSUPPORTED', 'READER_UNSUPPORTED',
    'READ_FAILED', 'REMOTE_HASH_MISMATCH', 'REMOTE_INCOMPLETE', 'REMOTE_PAYLOAD_MISMATCH',
    'REMOTE_SIZE_MISMATCH', 'REQUEST_FAILED', 'REQUEST_INVALID', 'RESPONSE_INVALID',
    'SCHEMA_MISMATCH', 'SINK_FAILED', 'SNAPSHOT_CHANGED', 'SNAPSHOT_CLEANUP_FAILED',
    'SNAPSHOT_CLOSED', 'SNAPSHOT_FAILED', 'SNAPSHOT_SIDECAR_UNSUPPORTED', 'SNAPSHOT_TIMEOUT',
    'SNAPSHOT_UNAVAILABLE', 'SOURCE_CHANGED', 'SOURCE_MISSING', 'SOURCE_UNAVAILABLE',
    'STAGING_INCOMPLETE', 'START_FAILED', 'STATUS_UNAVAILABLE', 'VERSION_MISMATCH',
    'WORKSPACE_FORMAT_MISMATCH', 'WORKSPACE_PATH_CONFLICT', 'WORK_BODY_HASH_MISMATCH', 'WRITE_UNCERTAIN',
})
# Native resolver reads ACTIVE, MANIFEST and COMMIT controls, then ACTIVE
# again. Each step may fail with a fixed transport code. Keep this bounded.
_FETCH_STEPS = frozenset({'ACTIVE', 'MANIFEST', 'COMMIT', 'ACTIVE_RECHECK'})
_FETCH_CAUSES = frozenset({
    'REQUEST_FAILED', 'RESPONSE_INVALID', 'AUTH_REQUIRED', 'ACCESS_DENIED',
    'CONFLICT', 'RATE_LIMITED', 'PATH_INVALID', 'LIMIT_EXCEEDED',
    'REMOTE_HASH_MISMATCH', 'REMOTE_SIZE_MISMATCH', 'SINK_FAILED',
    'READ_FAILED', 'DNS_FAILED', 'TLS_FAILED', 'READ_TIMEOUT',
    'NETWORK_FAILED',
})
_ERRORS = (_DIRECT_ERRORS
           | frozenset('PARENT_' + code for code in _PARENT_COMPARISON_CODES)
           | frozenset('PARENT_WORKSPACE_' + code for code in _PARENT_COMPARISON_CODES)
           | frozenset(stage + '_' + cause for stage in _FETCH_STEPS for cause in _FETCH_CAUSES))
_UNCERTAIN = frozenset({'ACTIVATION_UNKNOWN', 'ACTIVATION_NOT_CONFIRMED', 'ACTIVE_DB_UNVERIFIED',
    'PUBLISHED_LOCAL_CHANGED', 'PUBLICATION_FAILED', 'WRITE_UNCERTAIN',
    'STAGING_INCOMPLETE', 'REMOTE_INCOMPLETE', 'REMOTE_HASH_MISMATCH'})


class WorkspacePublisher:
    """One publish attempt at a time; ambiguous activation stays latched.

    The caller must already have confirmed the intended App Folder and runtime
    isolation. Python process integrity and the local runtime are assumed.
    """
    def __init__(self, runtime, app_key, configured_redirect, registered_redirect):
        if not isinstance(app_key, str) or not _APP_KEY_RE.fullmatch(app_key):
            raise ValueError('INVALID_CONFIGURATION')
        if not callable(getattr(runtime, 'publication_status', None)) or not callable(getattr(runtime, 'publish', None)):
            raise ValueError('INVALID_CONFIGURATION')
        validate_redirect(configured_redirect, registered_redirect)
        self.__runtime = runtime
        self.__app_key = app_key
        self.__configured_redirect = configured_redirect
        self.__registered_redirect = registered_redirect
        self.__lock = threading.RLock()
        self.__running = False
        self.__publishing = False
        self.__publication_done = threading.Event()
        self.__publication_done.set()
        self.__closed = False
        self.__unknown = False
        self.__active_scope = None
        self.__previous_success = None
        self.__error = None

    def __repr__(self):
        return '<WorkspacePublisher redacted>'

    def owns(self, runtime):
        """Bind the server route to the exact runtime used by this controller."""
        return runtime is self.__runtime

    def write_if_idle(self, callback):
        """Serialize a local mutation with the start of a publish attempt.

        The callback is server-owned, not supplied by the HTTP client. Holding
        the same lock through it prevents OAuth from beginning between an idle
        check and a local write. Publication itself holds the runtime lock.
        """
        if not callable(callback):
            raise ValueError('INVALID_REQUEST')
        with self.__lock:
            if self.__running or self.__closed:
                return {'contract_version': 'UTT-WORKSPACE-WRITE/0.1', 'status': 'ERROR',
                        'error': {'code': 'PUBLISH_IN_PROGRESS' if self.__running else 'CONNECTION_UNAVAILABLE'}}
            return callback()

    @staticmethod
    def __scope(value):
        if (type(value) is not dict or value.get('contract_version') != 'UTT-WORKSPACE-STATUS/0.1'
                or value.get('status') != 'OK'):
            raise ValueError('INVALID_RUNTIME_STATUS')
        dataset, snapshot = value.get('dataset_id'), value.get('snapshot_id')
        if (type(dataset) is not str or not dataset or len(dataset) > 1024
                or type(snapshot) is not str or not snapshot or len(snapshot) > 1024):
            raise ValueError('INVALID_RUNTIME_STATUS')
        return dataset, snapshot

    @staticmethod
    def __response(status, scope, code=None):
        out = {'contract_version': _STATE, 'status': status,
               'dataset_id': scope[0], 'snapshot_id': scope[1]}
        if code is not None:
            out['error'] = {'code': code if type(code) is str and code in _ERRORS else 'PUBLISH_FAILED'}
        return out

    def status(self):
        """Return only fixed status and local snapshot scope; never wait for OAuth."""
        with self.__lock:
            if self.__running:
                return self.__response('RUNNING', self.__active_scope)
            current = None
            try:
                current = self.__runtime.publication_status()
                scope = self.__scope(current)
            except Exception:
                # A known active scope, if any, is safe to return without
                # including exception messages or local paths.
                scope = (None, None)
                refresh = type(current) is dict and current.get('local_status') == 'REFRESH_REQUIRED'
                return self.__response('UNKNOWN' if self.__unknown or refresh else 'ERROR', scope, 'STATUS_UNAVAILABLE')
            if self.__unknown:
                return self.__response('UNKNOWN', scope, 'ACTIVATION_UNCERTAIN')
            if self.__closed:
                return self.__response('ERROR', scope, 'PUBLISHER_CLOSED')
            if self.__error is not None:
                return self.__response('ERROR', scope, self.__error)
            remote = current.get('dropbox_status')
            if current.get('local_status') == 'REFRESH_REQUIRED':
                return self.__response('UNKNOWN', scope, 'STATUS_UNAVAILABLE')
            if self.__previous_success is not None:
                if scope != self.__previous_success or remote == 'UNPUBLISHED':
                    return self.__response('UNPUBLISHED', scope)
                return self.__response('PUBLISHED' if remote == 'PUBLISHED' else 'UNKNOWN', scope)
            if remote == 'PUBLISHED':
                return self.__response('PUBLISHED', scope)
            if remote == 'UNPUBLISHED':
                return self.__response('UNPUBLISHED', scope)
            return self.__response('READY', scope)

    def start(self, dataset_id, snapshot_id):
        """Start one explicit attempt only for the currently observed snapshot."""
        with self.__lock:
            if self.__running:
                return self.__response('RUNNING', self.__active_scope)
            try:
                current = self.__runtime.publication_status()
                scope = self.__scope(current)
            except Exception:
                return self.__response('ERROR', (None, None), 'STATUS_UNAVAILABLE')
            if self.__unknown:
                return self.__response('UNKNOWN', scope, 'ACTIVATION_UNCERTAIN')
            if self.__closed:
                return self.__response('ERROR', scope, 'PUBLISHER_CLOSED')
            if current.get('status') != 'OK' or current.get('local_status') != 'LOCAL_SAVED':
                return self.__response('ERROR', scope, 'STATUS_UNAVAILABLE')
            if type(dataset_id) is not str or type(snapshot_id) is not str or (dataset_id, snapshot_id) != scope:
                return self.__response('ERROR', scope, 'VERSION_MISMATCH')
            self.__running = True
            self.__active_scope = scope
            self.__error = None
            try:
                threading.Thread(target=self.__run, args=(scope,), daemon=True,
                                 name='utt-publisher').start()
            except Exception:
                self.__running = False
                self.__error = 'START_FAILED'
                return self.__response('ERROR', scope, self.__error)
            return self.__response('RUNNING', scope)

    def __run(self, scope):
        session = None
        publish_started = False
        result = None
        app_key = None
        try:
            with self.__lock:
                if self.__closed:
                    return
                # An in-flight OAuth call retains this local key until it
                # returns; close() clears the controller's stored copy.
                app_key = self.__app_key
            session = run_publisher_flow(app_key, self.__configured_redirect,
                                         self.__registered_redirect)
            with self.__lock:
                if self.__closed:
                    self.__error = 'PUBLISHER_CLOSED'
                    return
                transport = PublisherTransport(session, confirmed_app_key=app_key,
                                               app_folder_confirmation='USER_CONFIRMED')
                self.__publishing = True
                self.__publication_done.clear()
                publish_started = True
            result = self.__runtime.publish(transport, session, dataset_id=scope[0], snapshot_id=scope[1])
            with self.__lock:
                if (type(result) is dict and result.get('contract_version') == 'UTT-WORKSPACE-PUBLICATION/0.1'
                        and result.get('status') == 'OK'
                        and result.get('code') in ('PUBLISHED', 'ALREADY_ACTIVE')
                        and result.get('dropbox_status') == 'PUBLISHED'
                        and (result.get('dataset_id'), result.get('snapshot_id')) == scope):
                    self.__previous_success = scope
                    self.__error = None
                elif (type(result) is dict and result.get('contract_version') == 'UTT-WORKSPACE-PUBLICATION/0.1'
                      and result.get('status') == 'ERROR'
                      and type(result.get('error')) is dict
                      and result['error'].get('code') not in _UNCERTAIN):
                    error = result.get('error')
                    self.__error = error.get('code') if type(error) is dict else 'PUBLISH_FAILED'
                else:
                    self.__unknown = True
        except Exception:
            with self.__lock:
                if publish_started:
                    self.__unknown = True
                else:
                    self.__error = 'AUTH_FAILED'
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            with self.__lock:
                self.__running = False
                self.__publishing = False
                self.__publication_done.set()
            app_key = None

    def close(self):
        """Clear the key and prevent pending OAuth from starting a publish.

        A publication already admitted cannot safely be interrupted: wait for
        it to finish rather than report closure while it may still write. An
        OAuth call already in progress may retain a local key until it returns.
        """
        with self.__lock:
            self.__closed = True
            self.__app_key = ''
            publishing = self.__publishing
        if publishing:
            self.__publication_done.wait()
