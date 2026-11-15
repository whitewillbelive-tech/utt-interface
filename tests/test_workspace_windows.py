"""Synthetic Windows startup: no actual filesystem, browser or Dropbox."""
import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

from app import workspace_windows as win


class WorkspaceWindowsTests(unittest.TestCase):
    def test_starts_without_authentication_and_closes_local_services(self):
        runtime, publisher, server = MagicMock(), MagicMock(), MagicMock()
        server.server_port = 8765
        output = io.StringIO()
        with patch.object(win.sys, 'platform', 'win32'), \
             patch.object(win, '_secret', side_effect=['private-db', 'private-dir', 'appkey', 'appkey']), \
             patch('builtins.input', side_effect=['http://127.0.0.1:9999/cb'] * 2), \
             patch.object(win, 'validate_redirect'), \
             patch.object(win, 'OriginalWorkspaceEventCodec'), \
             patch.object(win, 'LocalWorkspaceRuntime', return_value=runtime), \
             patch.object(win, 'WorkspacePublisher', return_value=publisher), \
             patch.object(win, 'make_server', return_value=server), \
             contextlib.redirect_stdout(output):
            self.assertEqual(win.main([]), 0)
        server.serve_forever.assert_called_once()
        publisher.close.assert_called_once()
        runtime.close.assert_called_once()
        self.assertNotIn('private-db', output.getvalue())
        self.assertNotIn('appkey', output.getvalue())

    def test_key_mismatch_prevents_runtime_and_publisher(self):
        output = io.StringIO()
        with patch.object(win.sys, 'platform', 'win32'), \
             patch.object(win, '_secret', side_effect=['private-db', 'private-dir', 'appkey', 'different']), \
             patch('builtins.input', side_effect=['http://127.0.0.1:9999/cb'] * 2), \
             patch.object(win, 'validate_redirect'), \
             patch.object(win, 'LocalWorkspaceRuntime') as runtime, \
             patch.object(win, 'WorkspacePublisher') as publisher, \
             contextlib.redirect_stdout(output):
            self.assertEqual(win.main([]), 2)
        runtime.assert_not_called()
        publisher.assert_not_called()
        self.assertNotIn('private-db', output.getvalue())
        self.assertNotIn('different', output.getvalue())


if __name__ == '__main__':
    unittest.main()
