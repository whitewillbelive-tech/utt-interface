"""Invented native-control inputs for the publisher candidate builder."""
import copy
import hashlib
import json
import unittest

from app.publisher_candidate import CandidateError, _canonical, build_candidate
from app.publisher_reader import resolve_active, assert_parent_preserved, ReaderError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def parent_fixture(*, extra_entries=()):
    files = [
        {'path': 'system/baseline.json', 'bytes': 5, 'sha256': sha(b'first'), 'opaque': 'fictional-entry-extra'},
        {'path': 'sources/fictional-source.bin', 'bytes': 6, 'sha256': sha(b'second')},
        *copy.deepcopy(list(extra_entries)),
    ]
    manifest = {'format': 'utt-baseline/1', 'baseline_id': 'fictional-baseline',
                'unknown_extension': {'z': 2, 'a': ['fictional-marker']}, 'files': files}
    manifest_raw = wire(manifest)
    generation = sha(manifest_raw)
    root = '/snapshots/baselines/' + generation + '/'
    commit_raw = wire({'format': 'utt-baseline/1', 'baseline_id': 'fictional-baseline',
                       'manifest_sha256': generation, 'state': 'complete'})
    active_raw = wire({'format': 'utt-active-baseline/1', 'generation_id': generation,
                       'manifest_sha256': generation, 'baseline_id': 'fictional-baseline',
                       'root': root, 'commit_sha256': sha(commit_raw), 'files': len(files) + 2,
                       'bytes': sum(row['bytes'] for row in files) + len(manifest_raw) + len(commit_raw)})
    objects = {'/system/active-baseline.json': active_raw,
               root + 'system/manifest.json': manifest_raw,
               root + 'system/COMMIT.json': commit_raw}
    parent = resolve_active(lambda path: (objects[path], 'fictional-rev'))
    return parent, manifest_raw, manifest


class PublisherCandidateTests(unittest.TestCase):
    def setUp(self):
        self.parent, self.raw, self.value = parent_fixture()
        self.digest = sha(b'fictional-workspace-bytes')

    def build(self, **kwargs):
        return build_candidate(self.parent, self.raw, workspace_size=23,
                               workspace_sha256=kwargs.get('workspace_sha256', self.digest))

    def test_preserves_parent_payload_extra_fields_and_binds_workspace(self):
        candidate = self.build()
        manifest = json.loads(candidate.manifest_bytes)
        self.assertEqual(manifest['unknown_extension'], self.value['unknown_extension'])
        self.assertEqual(manifest['files'][:len(self.value['files'])], self.value['files'])
        self.assertEqual(manifest['utt_workspace']['format'], 'UTT-WORKSPACE-OVERLAY/0.1')
        self.assertEqual(manifest['utt_workspace']['sha256'], self.digest)
        self.assertEqual(manifest['utt_workspace']['path'], candidate.workspace_path)
        self.assertEqual(candidate.generation_id, sha(candidate.manifest_bytes))
        root = '/snapshots/baselines/' + candidate.generation_id + '/'
        objects = {'/system/active-baseline.json': candidate.active_bytes,
                   root + 'system/manifest.json': candidate.manifest_bytes,
                   root + 'system/COMMIT.json': candidate.commit_bytes}
        resolved = resolve_active(lambda path: (objects[path], 'fictional-rev'))
        additions = assert_parent_preserved(self.parent.manifest, resolved.manifest)
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].path, candidate.workspace_path)

    def test_exact_parent_bytes_are_bound_even_if_json_semantics_match(self):
        different = json.dumps(self.value, ensure_ascii=False, indent=2).encode()
        with self.assertRaises(CandidateError) as exc:
            build_candidate(self.parent, different, workspace_size=23, workspace_sha256=self.digest)
        self.assertEqual(exc.exception.code, 'PARENT_MANIFEST_CHANGED')

    def test_existing_identical_workspace_is_not_duplicated(self):
        path = 'workspace/snapshot-' + self.digest + '.sqlite'
        parent, raw, original = parent_fixture(extra_entries=[{'path': path, 'bytes': 23, 'sha256': self.digest}])
        candidate = build_candidate(parent, raw, workspace_size=23, workspace_sha256=self.digest)
        manifest = json.loads(candidate.manifest_bytes)
        self.assertEqual(len(manifest['files']), len(original['files']))
        self.assertEqual(sum(row['path'] == path for row in manifest['files']), 1)

    def test_conflicting_workspace_paths_or_sizes_cannot_be_reused(self):
        path = 'workspace/snapshot-' + self.digest + '.sqlite'
        for original in (path, path.upper()):
            parent, raw, _ = parent_fixture(extra_entries=[{'path': original, 'bytes': 24, 'sha256': self.digest}])
            with self.subTest(case='original' if original == path else 'case-folded'):
                with self.assertRaises(CandidateError) as exc:
                    build_candidate(parent, raw, workspace_size=23, workspace_sha256=self.digest)
                self.assertEqual(exc.exception.code, 'WORKSPACE_PATH_CONFLICT')

    def test_untrusted_extra_overlay_is_replaced_without_removing_historical_payload(self):
        old_hash = sha(b'old-fictional-workspace')
        old_path = 'workspace/snapshot-' + old_hash + '.sqlite'
        parent, raw, _ = parent_fixture(extra_entries=[{'path': old_path, 'bytes': 17, 'sha256': old_hash}])
        candidate = build_candidate(parent, raw, workspace_size=23, workspace_sha256=self.digest)
        manifest = json.loads(candidate.manifest_bytes)
        self.assertIn(old_path, [entry['path'] for entry in manifest['files']])
        self.assertEqual(manifest['utt_workspace']['path'], candidate.workspace_path)

    def test_canonical_js_utf16_key_order_safe_integers_and_escaping(self):
        value = {'\ue000': 1, '\U00010000': 2, 'a': ['line\n"\\', None, True, -(2**53 - 1)]}
        self.assertEqual(_canonical(value), '{"a":["line\\n\\\"\\\\",null,true,-9007199254740991],"𐀀":2,"":1}')
        for value in (2**53, -(2**53), 1.5, {'x': '\ud800'}):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(CandidateError):
                    _canonical(value)

    def test_invalid_workspace_specs_fail_closed(self):
        for size, digest in [(0, self.digest), (-1, self.digest), (True, self.digest),
                             (2**53, self.digest), (1, 'x' * 64), (1, 'F' * 64)]:
            with self.subTest(size=size):
                with self.assertRaises(CandidateError):
                    build_candidate(self.parent, self.raw, workspace_size=size, workspace_sha256=digest)

    def test_no_parent_or_digest_values_are_in_repr_or_errors(self):
        candidate = self.build()
        self.assertNotIn(self.digest, repr(candidate))
        self.assertNotIn('fictional-baseline', repr(candidate))
        with self.assertRaises(CandidateError) as exc:
            build_candidate(self.parent, b'fictional-invalid-secret', workspace_size=23, workspace_sha256=self.digest)
        self.assertNotIn('fictional-invalid-secret', str(exc.exception))


if __name__ == '__main__':
    unittest.main()
