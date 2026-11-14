"""Metadata-only synthetic explorer tests; no real Dropbox or local data."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import socket
import unittest
from unittest.mock import patch

from app import baseline_explorer as explorer


def approved_fixture():
    fixture = explorer.SyntheticFolderFixture()
    fixture.user_approved = True  # Explicit fictional test consent only.
    return fixture


class SyntheticBaselineTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.fixture = approved_fixture()

    def check_unknown(self, result):
        self.assertEqual(result.canonical, 'UNKNOWN')
        self.assertEqual(result.snapshot, 'UNKNOWN')
        self.assertEqual(result.source, 'SYNTHETIC')
        return result

    def test_unapproved_default_stops_before_observations(self):
        fixture = explorer.SyntheticFolderFixture()
        result = self.check_unknown(explorer.inspect_synthetic(fixture))
        self.assertEqual(result.status, 'SCOPE_NOT_AUTHORIZED')
        self.assertEqual(fixture.calls, [])

    def test_complete_fictional_chain_is_only_a_synthetic_match(self):
        result = self.check_unknown(explorer.inspect_synthetic(self.fixture))
        self.assertEqual(result.status, 'SYNTHETIC_CHAIN_MATCH')

    def test_live_entry_fails_before_reading_inputs_or_network(self):
        class Poison:
            def __getattribute__(self, name):
                raise AssertionError('live gate touched an input')
            def __repr__(self):
                raise AssertionError('live gate displayed an input')
        result = explorer.inspect_live(Poison())
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.canonical, 'UNKNOWN')

    def test_missing_reference_is_distinct_from_multiple_candidates(self):
        self.fixture.root_entries = [self.fixture.root_entries[1]]
        missing = self.check_unknown(explorer.inspect_synthetic(self.fixture))
        self.assertEqual(missing.status, 'ACTIVE_REFERENCE_MISSING')
        fixture = approved_fixture()
        fixture.root_entries.append({'kind': 'reference', 'id': 'fictional-other-reference'})
        ambiguous = self.check_unknown(explorer.inspect_synthetic(fixture))
        self.assertEqual(ambiguous.status, 'CANDIDATES_AMBIGUOUS')
        self.assertEqual(fixture.calls, ['root'])

    def test_duplicate_entries_are_not_resolved_by_selecting_one(self):
        self.fixture.root_entries.append(copy.deepcopy(self.fixture.root_entries[0]))
        result = self.check_unknown(explorer.inspect_synthetic(self.fixture))
        self.assertEqual(result.status, 'DUPLICATE_ENTRY')
        fixture = approved_fixture()
        fixture.generation_entries.append(copy.deepcopy(fixture.generation_entries[1]))
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status, 'DUPLICATE_ENTRY')

    def test_multiple_commit_or_manifest_candidates_never_auto_select(self):
        for index in (0, 1):
            with self.subTest(index=index):
                fixture = approved_fixture()
                extra = copy.deepcopy(fixture.generation_entries[index])
                extra['id'] = 'fictional-other-node'
                fixture.generation_entries.append(extra)
                self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status,
                                 'CANDIDATES_AMBIGUOUS')

    def test_multiple_generations_are_scoped_by_the_reference(self):
        self.fixture.root_entries.append({'kind': 'generation', 'id': 'fictional-inactive-generation'})
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(self.fixture)).status,
                         'SYNTHETIC_CHAIN_MATCH')

    def test_unrelated_fictional_root_entry_is_ignored_but_counts_toward_budget(self):
        self.fixture.root_entries.append({'kind': 'other', 'id': 'fictional-unrelated-entry'})
        result = self.check_unknown(explorer.inspect_synthetic(self.fixture, max_entries=5))
        self.assertEqual(result.status, 'SYNTHETIC_CHAIN_MATCH')
        fixture = approved_fixture()
        fixture.root_entries.append({'kind': 'other', 'id': 'fictional-unrelated-entry'})
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture, max_entries=4)).status,
                         'BUDGET_EXCEEDED')
        fixture = approved_fixture()
        fixture.root_entries.append({'kind': 'other', 'id': fixture.root_entries[0]['id']})
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status,
                         'DUPLICATE_ENTRY')

    def test_cross_kind_global_id_collision_is_rejected(self):
        reused = self.fixture.root_entries[1]['id']
        self.fixture.generation_entries[0]['id'] = reused
        self.fixture.generation_entries[1]['commit'] = reused
        for reference in self.fixture.references:
            reference['commit'] = reused
        self.fixture.approval['commit'] = reused
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(self.fixture)).status,
                         'DUPLICATE_ENTRY')

    def test_incomplete_root_and_generation_cannot_establish_absence_or_success(self):
        for field in ('root_complete', 'generation_complete'):
            with self.subTest(field=field):
                fixture = approved_fixture()
                setattr(fixture, field, False)
                if field == 'root_complete':
                    fixture.root_entries = []
                self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status, 'INCOMPLETE_SCOPE')

    def test_reference_change_or_replacement_is_never_a_match(self):
        for key in ('id', 'revision', 'generation', 'commit', 'manifest'):
            with self.subTest(key=key):
                fixture = approved_fixture()
                fixture.references[1][key] = 'fictional-changed-value'
                result = self.check_unknown(explorer.inspect_synthetic(fixture))
                self.assertEqual(result.status, 'REFERENCE_CHANGED')
                self.assertEqual(fixture.calls, ['root', 'ref_first', 'generation', 'ref_last'])

    def test_first_reference_must_point_to_observed_generation_and_reference(self):
        for field in ('id', 'generation'):
            with self.subTest(field=field):
                fixture = approved_fixture()
                fixture.references[0][field] = 'fictional-nonexistent'
                self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status,
                                 'REFERENCE_MISMATCH')

    def test_manifest_commit_and_generation_links_are_individually_checked(self):
        changes = [(0, 'generation'), (0, 'manifest'), (0, 'anchor'),
                   (1, 'generation'), (1, 'commit'), (1, 'anchor')]
        for index, key in changes:
            with self.subTest(node=index, key=key):
                fixture = approved_fixture()
                fixture.generation_entries[index][key] = 'fictional-wrong-value'
                self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status,
                                 'MANIFEST_MISMATCH')

    def test_approval_must_exist_and_match_each_link_and_anchor(self):
        self.fixture.approval = None
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(self.fixture)).status, 'APPROVAL_UNKNOWN')
        for field in ('generation', 'commit', 'manifest', 'anchor'):
            with self.subTest(field=field):
                fixture = approved_fixture()
                fixture.approval[field] = 'fictional-other-value'
                self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status,
                                 'APPROVAL_MISMATCH')
                self.assertNotIn('ref_last', fixture.calls)

    def test_each_phase_distinguishes_denial_expiry_and_failure(self):
        for operation in ('root', 'ref_first', 'generation', 'ref_last'):
            for status in ('ACCESS_DENIED', 'AUTH_EXPIRED', 'REQUEST_FAILED'):
                with self.subTest(operation=operation, status=status):
                    fixture = approved_fixture()
                    fixture.failures[operation] = status
                    self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status, status)
                    self.assertEqual(fixture.calls[-1], operation)
        self.fixture.auth_state = 'EXPIRED'
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(self.fixture)).status, 'AUTH_EXPIRED')
        self.assertEqual(self.fixture.calls, [])

    def test_scope_and_explicit_approval_required_before_any_observation(self):
        for field, value, status in [('user_approved', False, 'SCOPE_NOT_AUTHORIZED'),
                                     ('user_approved', 1, 'SCOPE_NOT_AUTHORIZED'),
                                     ('app_identity', 'fictional-other-app', 'WRONG_SCOPE'),
                                     ('folder_scope', 'fictional-full-account', 'WRONG_SCOPE')]:
            with self.subTest(field=field):
                fixture = approved_fixture()
                setattr(fixture, field, value)
                self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture)).status, status)
                self.assertEqual(fixture.calls, [])

    def test_revocation_or_expiry_during_last_observation_cannot_match(self):
        original_check = explorer._check_dict
        for field, value, status in [('user_approved', False, 'SCOPE_NOT_AUTHORIZED'),
                                     ('folder_scope', 'fictional-full-account', 'WRONG_SCOPE'),
                                     ('auth_state', 'EXPIRED', 'AUTH_EXPIRED')]:
            with self.subTest(field=field):
                fixture = approved_fixture()
                def revoke(value_to_check, keys):
                    if value_to_check is fixture.references[1]:
                        setattr(fixture, field, value)
                    return original_check(value_to_check, keys)
                with patch.object(explorer, '_check_dict', side_effect=revoke):
                    result = self.check_unknown(explorer.inspect_synthetic(fixture))
                self.assertEqual(result.status, status)

    def test_request_and_total_entry_budgets_stop_without_extra_reads(self):
        for request_limit in (1, 2, 3):
            with self.subTest(request_limit=request_limit):
                fixture = approved_fixture()
                result = self.check_unknown(explorer.inspect_synthetic(fixture, max_requests=request_limit))
                self.assertEqual(result.status, 'BUDGET_EXCEEDED')
                self.assertLessEqual(len(fixture.calls), request_limit)
        fixture = approved_fixture()
        result = self.check_unknown(explorer.inspect_synthetic(fixture, max_entries=3))
        self.assertEqual(result.status, 'BUDGET_EXCEEDED')
        self.assertNotIn('ref_last', fixture.calls)
        self.assertEqual(self.check_unknown(explorer.inspect_synthetic(
            approved_fixture(), max_entries=4)).status, 'SYNTHETIC_CHAIN_MATCH')

    def test_out_of_range_or_boolean_limits_rejected_before_any_read(self):
        for kwargs in ({'max_requests': 0}, {'max_requests': 5}, {'max_requests': True},
                       {'max_entries': 0}, {'max_entries': 33}, {'max_entries': True}):
            with self.subTest(kwargs=kwargs):
                fixture = approved_fixture()
                self.assertEqual(self.check_unknown(explorer.inspect_synthetic(fixture, **kwargs)).status,
                                 'INVALID_LIMIT')
                self.assertEqual(fixture.calls, [])

    def test_invalid_graph_does_not_leak_or_raise(self):
        self.fixture.root_entries[0]['id'] = '/fictional-secret-path'
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = self.check_unknown(explorer.inspect_synthetic(self.fixture))
        self.assertEqual(result.status, 'INVALID_FIXTURE')
        self.assertNotIn('/fictional-secret-path', str(result) + stdout.getvalue() + stderr.getvalue())

    def test_untrusted_error_and_auth_values_never_leak(self):
        secret = 'fictional-token-never-display'
        for field in ('failure', 'auth'):
            with self.subTest(field=field):
                fixture = approved_fixture()
                if field == 'failure':
                    fixture.failures['root'] = secret
                else:
                    fixture.auth_state = secret
                with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                    result = self.check_unknown(explorer.inspect_synthetic(fixture))
                self.assertIn(result.status, ('REQUEST_FAILED', 'AUTH_REQUIRED'))
                self.assertNotIn(secret, repr(result) + stdout.getvalue() + stderr.getvalue())

    def test_exploration_does_not_use_file_or_network_access(self):
        with patch('builtins.open', side_effect=AssertionError('filesystem forbidden')):
            result = self.check_unknown(explorer.inspect_synthetic(self.fixture))
        self.assertEqual(result.status, 'SYNTHETIC_CHAIN_MATCH')

    def test_graph_and_approval_are_not_modified(self):
        before = copy.deepcopy((self.fixture.root_entries, self.fixture.generation_entries,
                                self.fixture.references, self.fixture.approval))
        self.check_unknown(explorer.inspect_synthetic(self.fixture))
        self.assertEqual(before, (self.fixture.root_entries, self.fixture.generation_entries,
                                  self.fixture.references, self.fixture.approval))

    def test_results_never_print_identifiers_or_credentials(self):
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            result = explorer.inspect_synthetic(self.fixture)
        text = captured.getvalue() + repr(result) + str(result)
        for entry in self.fixture.root_entries + self.fixture.generation_entries:
            for key in ('id', 'path', 'name', 'revision', 'hash', 'token'):
                if isinstance(entry.get(key), str):
                    self.assertNotIn(entry[key], text)
        for reference in self.fixture.references:
            for value in reference.values():
                if isinstance(value, str) and len(value) > 6:
                    self.assertNotIn(value, text)


if __name__ == '__main__':
    unittest.main()
