"""Semantics tests for a synthetic example; not a test of production integration."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'fixtures' / 'synthetic'
fixture = json.loads((HERE / 'fixture.json').read_text(encoding='utf-8'))
spec = importlib.util.spec_from_file_location('synthetic_generator', HERE / 'generate.py')
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

class SyntheticTests(unittest.TestCase):
    def test_generator_is_deterministic_and_matches_committed_fixture(self):
        self.assertEqual(gen.create(), fixture)
        self.assertEqual(fixture['fixture_marker'], 'SYNTHETIC_ONLY')

    def test_generator_refuses_to_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as dirname:
            args=[sys.executable,str(HERE/'generate.py'),'--output-dir',dirname]
            first=subprocess.run(args,capture_output=True,text=True,check=False)
            self.assertEqual(first.returncode,0,first.stderr)
            output=Path(dirname)/'fixture.json'
            before=output.read_bytes()
            second=subprocess.run(args,capture_output=True,text=True,check=False)
            self.assertNotEqual(second.returncode,0)
            self.assertEqual(output.read_bytes(),before)

    def test_project_links_are_many_to_many_and_unclassified_is_distinct(self):
        links = fixture['project_links']
        records = {r['id'] for r in fixture['records']}
        projects = {p['id'] for p in fixture['projects']}
        self.assertTrue(all(x['record_id'] in records and x['project_id'] in projects for x in links))
        self.assertEqual({x['project_id'] for x in links if x['record_id']=='synthetic-record-002'},
                         {'synthetic-project-cloud','synthetic-project-clock'})
        self.assertFalse(any(x['record_id']=='synthetic-record-003' for x in links))
        self.assertEqual(next(x['status'] for x in links if x['record_id']=='synthetic-record-002' and x['project_id']=='synthetic-project-clock'),'unconfirmed')

    def test_source_missing_and_empty_value_are_not_collapsed(self):
        sources = {s['id']:s for s in fixture['sources']}
        records = {r['id']:r for r in fixture['records']}
        self.assertIsNone(records['synthetic-record-003']['source_id'])
        self.assertIsNone(sources[records['synthetic-record-004']['source_id']]['text'])
        self.assertTrue(sources[records['synthetic-record-004']['source_id']]['missing'])
        self.assertEqual(records['synthetic-record-007']['summary'], '')
        self.assertNotIn('summary', records['synthetic-record-003'])

    def test_code_unit_span_roundtrip_and_version_hash(self):
        span = fixture['location']
        source = next(s for s in fixture['sources'] if s['id']==span['source_id'])
        original = source['text']
        self.assertGreater(len(original),3000)
        self.assertEqual(span['unit'], 'utf16_code_units')
        self.assertEqual(span['boundary'], 'half_open')
        units = original.encode('utf-16-le')
        self.assertEqual(units[2*span['start']:2*span['end']].decode('utf-16-le'), '🧭 동쪽')
        self.assertNotEqual(span['end']-span['start'],len(span['text']))
        self.assertEqual(hashlib.sha256(original.encode('utf-8')).hexdigest(),span['source_sha256'])

    def test_cancel_and_conflict_do_not_imply_canon_approval(self):
        records = {r['id']:r for r in fixture['records']}
        conflict = records['synthetic-record-005']
        self.assertEqual((conflict['review_state'],conflict['proposal_state'],conflict['canon_state']),('held','conflict','not_approved'))
        events = fixture['history']
        self.assertEqual(events[1]['type'],'cancel')
        self.assertEqual(events[1]['supersedes'],events[0]['id'])
        self.assertNotEqual(records['synthetic-record-006']['canon_state'],'approved')

    def test_example_pagination_is_stable_and_complete(self):
        # Fixed expected order is independent of implementation; never an operational API.
        rows = sorted(fixture['records'],key=lambda r:r['id'])
        pages = [rows[i:i+3] for i in range(0,len(rows),3)]
        self.assertEqual([len(p) for p in pages],[3,3,1])
        ids = [r['id'] for p in pages for r in p]
        self.assertEqual(ids,['synthetic-record-%03d'%i for i in range(1,8)])
        self.assertEqual(len(ids),len(set(ids)))

    def test_contract_examples_cover_every_operation(self):
        examples = json.loads((HERE / 'contract_examples.json').read_text(encoding='utf-8'))
        self.assertEqual(examples, gen.contract_examples(fixture))
        ops = {x['operation'] for x in examples.values() if x.get('status')=='OK'}
        self.assertEqual(ops, {'capabilities','projects','search','detail','related_history'})
        page1 = examples['search_page_1']['result']
        page2 = examples['search_page_2']['result']
        page3 = examples['search_page_3']['result']
        self.assertEqual([len(x['records']) for x in [page1,page2,page3]], [3,3,1])
        self.assertIsNone(page3['page']['next_cursor'])
        self.assertEqual(examples['search_empty']['status'], 'OK')
        self.assertEqual(examples['search_empty']['result']['records'], [])
        for name, code in [('source_missing','SOURCE_MISSING'),('connection_failure','CONNECTION_UNAVAILABLE'),('unsupported_contract','UNSUPPORTED_CONTRACT'),('snapshot_changed','VERSION_MISMATCH')]:
            self.assertEqual(examples[name]['status'],'ERROR')
            self.assertEqual(examples[name]['error']['code'],code)
            self.assertIsNone(examples[name]['result'])

    def test_empty_search_differs_from_error_scenarios(self):
        cases = {x['id']:x for x in fixture['scenarios']}
        self.assertEqual(cases['empty_search'],{'id':'empty_search','kind':'success','expected_total':0})
        self.assertEqual(cases['source_missing']['expected_code'],'SOURCE_MISSING')
        self.assertEqual(cases['connection_failure']['expected_code'],'CONNECTION_UNAVAILABLE')
        self.assertEqual(cases['unsupported_contract']['expected_code'],'UNSUPPORTED_CONTRACT')
        self.assertEqual(cases['snapshot_changed']['expected_code'],'VERSION_MISMATCH')

if __name__ == '__main__':
    unittest.main()
