"""Native control and payload checks over wholly fictional in-memory data."""
import hashlib
import json
import socket
import unittest
from unittest.mock import patch

from app.publisher_reader import (ACTIVE, ReaderError, assert_legacy_pins, assert_native_payloads,
                                  assert_parent_preserved, parse_manifest,
                                  resolve_active, verify_payload)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(data):
    return (json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def candidate(payloads, *, extension=None):
    files = [{'path': path, 'bytes': len(raw), 'sha256': digest(raw)}
             for path, raw in payloads.items()]
    manifest = {'format': 'utt-baseline/1', 'baseline_id': 'fictional-baseline', 'files': files}
    if extension is not None:
        manifest['utt_workspace'] = extension
    manifest_raw = encode(manifest)
    generation = digest(manifest_raw)
    commit_raw = encode({'format': 'utt-baseline/1', 'baseline_id': 'fictional-baseline',
                         'manifest_sha256': generation, 'state': 'complete'})
    root = '/snapshots/baselines/' + generation + '/'
    active = {'format': 'utt-active-baseline/1', 'generation_id': generation,
              'manifest_sha256': generation, 'baseline_id': 'fictional-baseline',
              'root': root, 'commit_sha256': digest(commit_raw),
              'files': len(files) + 2,
              'bytes': sum(len(x) for x in payloads.values()) + len(manifest_raw) + len(commit_raw)}
    active_raw = encode(active)
    blobs = {ACTIVE: (active_raw, 'fictional-rev'),
             root + 'system/manifest.json': (manifest_raw, 'fictional-manifest-rev'),
             root + 'system/COMMIT.json': (commit_raw, 'fictional-commit-rev')}
    return active, manifest, blobs


class PublisherReaderTests(unittest.TestCase):
    def setUp(self):
        socket_guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        socket_guard.start()
        self.addCleanup(socket_guard.stop)
        self.payloads = {'system/baseline.json': b'fictional-baseline',
                         'sources/asset.txt': b'fictional-source'}
        self.active, self.manifest, self.blobs = candidate(self.payloads)

    def resolve(self, blobs=None):
        mapping = self.blobs if blobs is None else blobs
        return resolve_active(lambda path: mapping.get(path))

    def assert_code(self, code, call):
        with self.assertRaises(ReaderError) as error:
            call()
        self.assertEqual(error.exception.code, code)
        self.assertNotIn('fictional-', str(error.exception))

    def test_control_fetch_diagnostics_are_stage_scoped_and_redacted(self):
        from app.publisher_transport import PublisherTransportError
        root = self.active['root']
        for failing_path, expected in (
            (ACTIVE, 'ACTIVE_REQUEST_FAILED'),
            (root + 'system/manifest.json', 'MANIFEST_NETWORK_FAILED'),
            (root + 'system/COMMIT.json', 'COMMIT_RESPONSE_INVALID'),
        ):
            with self.subTest(code=expected):
                seen = []
                def fetch(path):
                    seen.append(path)
                    if path == failing_path:
                        code = expected.split('_', 1)[1]
                        raise PublisherTransportError(code)
                    return self.blobs.get(path)
                self.assert_code(expected, lambda: resolve_active(fetch))
                self.assertIn(failing_path, seen)

    def test_active_recheck_failure_has_distinct_fixed_code(self):
        calls = 0
        def fetch(path):
            nonlocal calls
            if path == ACTIVE:
                calls += 1
                if calls == 2:
                    raise OSError('fictional-secret')
            return self.blobs.get(path)
        self.assert_code('ACTIVE_RECHECK_READ_FAILED', lambda: resolve_active(fetch))

    def test_resolves_exact_controls_and_rereads_revision(self):
        calls = []
        def fetch(path):
            calls.append(path)
            return self.blobs.get(path)
        resolved = resolve_active(fetch)
        self.assertEqual(calls, [ACTIVE, self.active['root'] + 'system/manifest.json',
                                 self.active['root'] + 'system/COMMIT.json', ACTIVE])
        self.assertEqual(resolved.manifest.sha256, self.active['generation_id'])
        self.assertEqual(resolved.active_revision, 'fictional-rev')
        self.assertIsNone(resolved.manifest.workspace)
        self.assertNotIn('fictional-', repr(resolved))
        self.assertNotIn('fictional-', repr(resolved.manifest))

    def test_missing_or_changed_active_bytes_or_revision_fail(self):
        self.assert_code('ACTIVE_MISSING', lambda: resolve_active(lambda _path: None))
        for change in ('bytes', 'revision'):
            with self.subTest(change=change):
                count = 0
                def fetch(path):
                    nonlocal count
                    value = self.blobs.get(path)
                    if path == ACTIVE:
                        count += 1
                        if count == 2:
                            return ((value[0] + b' ') if change == 'bytes' else value[0],
                                    'fictional-other' if change == 'revision' else value[1])
                    return value
                self.assert_code('ACTIVE_CHANGED', lambda: resolve_active(fetch))

    def test_active_shape_and_root_are_strict_before_fetch(self):
        for changes in ({'root': '/fictional-other/'}, {'files': True}, {'bytes': -1},
                        {'generation_id': 'A' * 64}, {'format': 'other'}, {'extra': 1}):
            with self.subTest(changes=changes):
                value = {**self.active, **changes}
                calls = []
                def fetch(path):
                    calls.append(path)
                    return (encode(value), 'fictional-rev')
                self.assert_code('INVALID_ACTIVE', lambda: resolve_active(fetch))
                self.assertEqual(calls, [ACTIVE])
        self.assert_code('INVALID_ACTIVE', lambda: resolve_active(
            lambda _: (b'{"format":"x","format":"y"}', 'fictional-rev')))

    def test_exact_raw_manifest_and_commit_bytes_are_pinned(self):
        for leaf, expected in (('system/manifest.json', 'MANIFEST_MISMATCH'),
                               ('system/COMMIT.json', 'COMMIT_MISMATCH')):
            with self.subTest(leaf=leaf):
                changed = dict(self.blobs)
                path = self.active['root'] + leaf
                data, rev = changed[path]
                changed[path] = (data + b' ', rev)
                self.assert_code(expected, lambda: self.resolve(changed))

    def test_catalog_count_and_bytes_mismatch_are_rejected(self):
        for field in ('files', 'bytes'):
            with self.subTest(field=field):
                changed = dict(self.blobs)
                active = dict(self.active)
                active[field] += 1
                changed[ACTIVE] = (encode(active), 'fictional-rev')
                self.assert_code('CATALOG_MISMATCH', lambda: self.resolve(changed))

    def test_manifest_paths_duplicates_and_metadata_validation(self):
        variants = []
        for path in ('../x', '/outside', 'a//b', 'system/COMMIT.json', 'Scrivener/file', '한글'):
            variants.append({'path': path, 'bytes': 0, 'sha256': '0' * 64})
        variants.extend([{'path': 'x', 'bytes': True, 'sha256': '0' * 64},
                         {'path': 'x', 'bytes': 0, 'sha256': 'G' * 64}])
        for item in variants:
            with self.subTest(item=item):
                self.assert_code('INVALID_MANIFEST', lambda: parse_manifest(encode({
                    'format': 'utt-baseline/1', 'baseline_id': 'fictional', 'files': [item]})))
        duplicate = {'format': 'utt-baseline/1', 'baseline_id': 'fictional', 'files': [
            {'path': 'data/A', 'bytes': 0, 'sha256': '0' * 64},
            {'path': 'data/a', 'bytes': 0, 'sha256': '0' * 64}]}
        self.assert_code('INVALID_MANIFEST', lambda: parse_manifest(encode(duplicate)))

    def test_payload_stream_is_bound_and_checks_all_bytes(self):
        resolved = self.resolve()
        calls = []
        def fetch(path):
            calls.append(path)
            return (b'fictional-', b'source')
        verify_payload(resolved, 'sources/asset.txt', fetch)
        self.assertEqual(calls, [resolved.root + 'sources/asset.txt'])
        self.assert_code('PAYLOAD_NOT_DECLARED', lambda: verify_payload(resolved, 'unlisted', fetch))
        self.assert_code('PAYLOAD_MISMATCH', lambda: verify_payload(resolved, 'sources/asset.txt', lambda _: [b'bad']))
        self.assert_code('INVALID_PAYLOAD', lambda: verify_payload(resolved, 'sources/asset.txt', lambda _: [b'']))
        self.assert_code('READ_FAILED', lambda: verify_payload(resolved, 'sources/asset.txt',
                         lambda _: (_ for _ in ()).throw(RuntimeError('fictional-secret'))))

    def test_parent_entries_must_be_retained_exactly(self):
        parent = self.resolve().manifest
        _, candidate_manifest, _ = candidate({**self.payloads, 'workspace/fictional.sqlite': b'workspace'})
        added = assert_parent_preserved(parent, parse_manifest(encode(candidate_manifest)))
        self.assertEqual(len(added), 1)
        _, changed, _ = candidate({**self.payloads, 'sources/asset.txt': b'changed'})
        self.assert_code('PARENT_PAYLOAD_CONFLICT', lambda: assert_parent_preserved(parent, parse_manifest(encode(changed))))
        _, missing, _ = candidate({'system/baseline.json': b'fictional-baseline'})
        self.assert_code('PARENT_PAYLOAD_CONFLICT', lambda: assert_parent_preserved(parent, parse_manifest(encode(missing))))

    def test_native_preflight_requires_all_known_catalogs(self):
        self.assert_code('NATIVE_PAYLOAD_MISSING', lambda: assert_native_payloads(self.resolve().manifest))
        full = {path: b'fictional' for path in ('system/baseline.json', 'system/schema.json',
                'snapshots/tables.json', 'snapshots/objects.json', 'sources/index.json')}
        _, manifest, _ = candidate(full)
        assert_native_payloads(parse_manifest(encode(manifest)))
        for path in tuple(full):
            with self.subTest(path=path):
                _, missing, _ = candidate({key: value for key, value in full.items() if key != path})
                self.assert_code('NATIVE_PAYLOAD_MISSING', lambda: assert_native_payloads(parse_manifest(encode(missing))))

    def test_workspace_extension_binds_path_hash_and_manifest_entry(self):
        raw = b'fictional-workspace'
        path = 'workspace/snapshot-' + digest(raw) + '.sqlite'
        extension = {'format': 'UTT-WORKSPACE-OVERLAY/0.1', 'path': path, 'sha256': digest(raw)}
        _, manifest, blobs = candidate({**self.payloads, path: raw}, extension=extension)
        self.assertEqual(self.resolve(blobs).manifest.workspace.path, path)
        for modified in ({**extension, 'sha256': '0' * 64}, {**extension, 'path': 'workspace/wrong.sqlite'},
                         {**extension, 'extra': True}):
            with self.subTest(modified=modified):
                manifest['utt_workspace'] = modified
                self.assert_code('INVALID_OVERLAY', lambda: parse_manifest(encode(manifest)))
        _, missing, _ = candidate(self.payloads, extension=extension)
        self.assert_code('INVALID_OVERLAY', lambda: parse_manifest(encode(missing)))

    def test_legacy_pin_match_is_only_a_narrow_check(self):
        resolved = self.resolve()
        assert_legacy_pins(resolved, manifest_sha256=resolved.manifest.sha256, root=resolved.root,
                           file_count=resolved.file_count, total_bytes=resolved.total_bytes)
        self.assert_code('LEGACY_PIN_MISMATCH', lambda: assert_legacy_pins(
            resolved, manifest_sha256='0' * 64, root=resolved.root,
            file_count=resolved.file_count, total_bytes=resolved.total_bytes))


if __name__ == '__main__':
    unittest.main()
