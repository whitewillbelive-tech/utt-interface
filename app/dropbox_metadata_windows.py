"""Explicit Windows App Folder metadata check; no file content access.

The existing application's identity and App Folder type are confirmed locally
by the user. Dropbox does not provide independent proof of that app type here.
No filenames, paths, account IDs, token values or remote bodies are printed.
"""
from __future__ import annotations

import getpass
import hmac
import sys
import warnings

from app.dropbox_auth import OAuthError, _APP_KEY_RE
from app.dropbox_metadata import _valid_target, inspect_live_metadata
from app.dropbox_windows import run_flow, validate_redirect


def _secret(prompt: str) -> str:
    """Never fall back to echoing a key or sensitive canonical path."""
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main() -> int:
    if sys.platform != 'win32':
        print('This command is available on Windows only.')
        return 2
    session = None
    try:
        print('Confirm locally in the existing Dropbox Developer Console:')
        print('UnifiedThinkTank is the intended app and its access type is App Folder.')
        print('This is USER_CONFIRMED, not independent Dropbox verification.')
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
            print('Metadata check did not complete: APP_KEY_MISMATCH')
            return 2
        print('Use one already confirmed canonical Dropbox path_lower within this App Folder.')
        print('This program does not list folders or discover the target.')
        target = _secret('Canonical path_lower (hidden): ')
        if not _valid_target(target):
            print('Metadata check did not complete: INVALID_REQUEST')
            return 2
        if input('Type START to authorize and request metadata once: ') != 'START':
            print('Cancelled.')
            return 2
        session = run_flow(app_key, configured, registered)
        result = inspect_live_metadata(session, confirmed_app_key=confirmed_key,
                                       app_folder_confirmation='USER_CONFIRMED', target=target)
        # Result is a fixed status only: no target, path, name, ID or response.
        print('Metadata status:', result.status)
        print('App Folder:', result.source)
        print('File integrity:', result.integrity, '| Snapshot:', result.snapshot)
        return 0 if result.status == 'FOUND_UNVERIFIED' else 2
    except KeyboardInterrupt:
        print('\nCancelled.')
        return 2
    except getpass.GetPassWarning:
        print('Metadata check did not complete: SECURE_INPUT_UNAVAILABLE')
        return 2
    except OAuthError as exc:
        # OAuthError is fixed-code only; never display remote exception text.
        print('Metadata check did not complete:', exc.code)
        return 2
    except Exception:
        print('Metadata check did not complete: REQUEST_FAILED')
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == '__main__':
    raise SystemExit(main())
