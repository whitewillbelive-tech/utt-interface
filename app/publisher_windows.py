"""Explicit Windows publisher. Secrets and paths remain in the local process.

Actual network use starts only when the user runs this program and chooses
START. The Dropbox publisher grant is separate from the read-only grant.
"""
from __future__ import annotations

import getpass
import hmac
import sys
import warnings

from app.dropbox_auth import OAuthError, _APP_KEY_RE
from app.dropbox_windows import validate_redirect
from app.publisher_auth import run_publisher_flow
from app.publisher_transport import PublisherTransport, PublisherTransportError
from app.publisher_workspace import WorkspaceError, snapshot_workspace
from app.publisher_pipeline import PublicationError, publish_snapshot


def _secret(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main():
    if sys.platform != 'win32':
        print('Publisher is available on Windows only.')
        return 2
    session = None
    try:
        print('Confirm the existing UnifiedThinkTank app is an App Folder app.')
        print('Only run in a protected user environment; stop all workspace editors first.')
        print('App Folder and process isolation are user-confirmed, not proven by this program.')
        if input('Type CONFIRM after checking: ') != 'CONFIRM':
            print('Cancelled.')
            return 2
        configured = input('Local redirect URI: ')
        registered = input('Exact URI already registered in the app: ')
        validate_redirect(configured, registered)
        app_key = _secret('Existing app key (hidden): ')
        confirmed_key = _secret('Repeat the confirmed app key (hidden): ')
        if (not _APP_KEY_RE.fullmatch(app_key) or not _APP_KEY_RE.fullmatch(confirmed_key)
                or not hmac.compare_digest(app_key, confirmed_key)):
            print('Publication stopped: APP_KEY_MISMATCH')
            return 2
        source = _secret('Local workspace SQLite path (hidden): ')
        directory = _secret('Protected local temporary directory (hidden): ')
        if not source or not directory:
            print('Publication stopped: INVALID_LOCAL_INPUT')
            return 2
        print('The publisher will stage a new generation and may switch the active reference.')
        print('The old fixed-generation A reader cannot open the new active generation.')
        print('Use the verified workspace reader supplied with this code; it is read-only.')
        if input('Type START to authorize and publish: ') != 'START':
            print('Cancelled.')
            return 2
        with snapshot_workspace(source, directory=directory) as snapshot:
            session = run_publisher_flow(app_key, configured, registered)
            transport = PublisherTransport(session, confirmed_app_key=confirmed_key,
                                           app_folder_confirmation='USER_CONFIRMED')
            result = publish_snapshot(snapshot, transport, session)
            print('Publication status:', result.code)
            return 0 if result.code in ('PUBLISHED', 'ALREADY_ACTIVE') else 2
    except KeyboardInterrupt:
        print('\nPublication interrupted; follow the documented recovery procedure.')
        return 2
    except getpass.GetPassWarning:
        print('Publication stopped: SECURE_INPUT_UNAVAILABLE')
        return 2
    except (OAuthError, PublisherTransportError, WorkspaceError, PublicationError) as exc:
        print('Publication stopped:', exc.code)
        return 2
    except Exception:
        # Do not disclose paths, tokens, IDs, hashes, remote errors or stack traces.
        print('Publication stopped: OPERATION_FAILED')
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == '__main__':
    raise SystemExit(main())
