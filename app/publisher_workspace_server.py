"""Windows-only reader for a verified published workspace snapshot.

The operator must confirm the existing App Folder and start authentication.
Only read scopes are requested. No source paths, credentials, record contents,
or remote metadata are printed; the snapshot remains in memory. Workspace
links and history have a separate read contract; original source-body access
and editing remain unsupported.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import sys
import warnings

from app.dropbox_auth import OAuthError, _APP_KEY_RE
from app.dropbox_windows import run_flow, validate_redirect
from app.publisher_read_transport import PublisherReadTransport, PublisherReadTransportError, MAX_WORKSPACE
from app.publisher_reader import ACTIVE, ReaderError, resolve_active
from app.publisher_workspace_reader import WorkspaceSnapshotProvider, WorkspaceSnapshotError
from app.provider import ReadOnlyAdapter
from app.server import make_server


def load_verified_workspace(transport):
    """Capture a declared snapshot and recheck the observed active reference.

    This establishes identity of the bytes read, not future immutability or
    absence of an unobserved active-reference ABA change.
    """
    resolved = resolve_active(transport.control)
    entry = resolved.manifest.workspace
    if entry is None:
        raise WorkspaceSnapshotError('UNSUPPORTED_CONTRACT')
    if not 100 <= entry.size <= MAX_WORKSPACE:
        raise WorkspaceSnapshotError('UNSUPPORTED_CONTRACT')
    try:
        reply = transport.workspace(resolved.root + entry.path, expected_size=entry.size)
    except (PublisherReadTransportError, OAuthError):
        raise WorkspaceSnapshotError('CONNECTION_UNAVAILABLE') from None
    if (type(reply) is not tuple or len(reply) != 2 or type(reply[0]) is not bytes
            or len(reply[0]) != entry.size or hashlib.sha256(reply[0]).hexdigest() != entry.sha256):
        raise WorkspaceSnapshotError('VERSION_MISMATCH')
    try:
        observed = transport.control(ACTIVE)
    except (PublisherReadTransportError, OAuthError):
        raise WorkspaceSnapshotError('CONNECTION_UNAVAILABLE') from None
    if observed != (resolved.active_bytes, resolved.active_revision):
        raise WorkspaceSnapshotError('VERSION_MISMATCH')
    # The hosting UI reads lifecycle status alongside each list and detail.
    return WorkspaceSnapshotProvider(reply[0], lifecycle_enabled=True)


def _secret(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Open a verified workspace snapshot in read-only mode.')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        print('This command is available on Windows only.')
        return 2
    if not 0 <= args.port <= 65535:
        print('Reader did not start: INVALID_PORT')
        return 2
    session = provider = server = None
    try:
        print('Confirm the existing UnifiedThinkTank App Folder in the Dropbox Developer Console.')
        print('App Folder type is user-confirmed, not independently verified by Dropbox.')
        if input('Type CONFIRM after checking: ') != 'CONFIRM':
            print('Cancelled.')
            return 2
        configured = input('Local redirect URI: ')
        registered = input('Exact URI already registered in the app: ')
        validate_redirect(configured, registered)
        app_key = _secret('Existing app key (hidden): ')
        confirmed = _secret('Repeat the confirmed app key (hidden): ')
        if (not _APP_KEY_RE.fullmatch(app_key) or not _APP_KEY_RE.fullmatch(confirmed)
                or not hmac.compare_digest(app_key, confirmed)):
            print('Reader did not start: APP_KEY_MISMATCH')
            return 2
        if input('Type START to authenticate and open read-only workspace: ') != 'START':
            print('Cancelled.')
            return 2
        session = run_flow(app_key, configured, registered)
        transport = PublisherReadTransport(session, confirmed_app_key=confirmed,
                                           app_folder_confirmation='USER_CONFIRMED')
        provider = load_verified_workspace(transport)
        # The server uses only the captured in-memory snapshot, not the grant.
        session.close()
        session = None
        server = make_server(args.port, mode='readonly', provider=ReadOnlyAdapter(provider.read))
        print(f'Workspace reader is ready at http://127.0.0.1:{server.server_port}/', flush=True)
        print('Read-only snapshot; original source bodies and editing are not connected.', flush=True)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print('\nStopped.')
        return 0
    except getpass.GetPassWarning:
        print('Reader did not start: SECURE_INPUT_UNAVAILABLE')
        return 2
    except (OAuthError, PublisherReadTransportError, ReaderError, WorkspaceSnapshotError) as exc:
        print('Reader did not start:', exc.code)
        return 2
    except Exception:
        print('Reader did not start: CONNECTION_UNAVAILABLE')
        return 2
    finally:
        if session is not None:
            session.close()
        if server is not None:
            server.server_close()
        if provider is not None:
            provider.close()


if __name__ == '__main__':
    raise SystemExit(main())
