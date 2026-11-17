"""Run the local workspace UI with explicit local input and user-started publish.

Paths, app key, OAuth tokens and records remain on the user's Windows machine.
The app requests no Dropbox grant until the user starts publication in the UI.
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import sys
import warnings

from app.dropbox_auth import _APP_KEY_RE
from app.dropbox_windows import validate_redirect
from app.server import make_server
from app.workspace_write import OriginalWorkspaceEventCodec
from app.workspace_runtime import LocalWorkspaceRuntime
from app.workspace_publisher import WorkspacePublisher


def _secret(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Open the local UTT workspace.')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        print('Workspace is available on Windows only.')
        return 2
    if not 0 <= args.port <= 65535:
        print('Workspace did not start: INVALID_PORT')
        return 2
    runtime = publisher = server = None
    try:
        print('Use a protected user-only runtime and stop other workspace editors.')
        source = _secret('Local workspace SQLite path (hidden): ')
        directory = _secret('Protected local temporary directory (hidden): ')
        configured = input('Local redirect URI: ')
        registered = input('Exact URI already registered in the existing App Folder app: ')
        validate_redirect(configured, registered)
        app_key = _secret('Existing app key (hidden): ')
        repeated_key = _secret('Repeat app key (hidden): ')
        if (not source or not directory or not _APP_KEY_RE.fullmatch(app_key)
                or not _APP_KEY_RE.fullmatch(repeated_key) or not hmac.compare_digest(app_key, repeated_key)):
            print('Workspace did not start: INVALID_LOCAL_INPUT')
            return 2
        runtime = LocalWorkspaceRuntime(source, directory, codec=OriginalWorkspaceEventCodec())
        publisher = WorkspacePublisher(runtime, app_key, configured, registered)
        app_key = repeated_key = ''
        server = make_server(args.port, mode='readonly', provider=runtime, publisher=publisher)
        print(f'Workspace ready at http://127.0.0.1:{server.server_port}/', flush=True)
        print('Edits are local until you select Dropbox publication in the workspace.', flush=True)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print('\nWorkspace stopped.')
        return 0
    except getpass.GetPassWarning:
        print('Workspace did not start: SECURE_INPUT_UNAVAILABLE')
        return 2
    except Exception:
        # Exceptions may contain user paths, IDs, bodies or credentials.
        print('Workspace did not start: LOCAL_WORKSPACE_UNAVAILABLE')
        return 2
    finally:
        if server is not None:
            server.server_close()
        if publisher is not None:
            publisher.close()
        if runtime is not None:
            runtime.close()


if __name__ == '__main__':
    raise SystemExit(main())
