"""Contract, provider, paging, and read-only boundary checks using only fiction."""
import copy
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import unittest
from unittest.mock import patch
from pathlib import Path
from app.provider import SyntheticProvider, ReadOnlyAdapter, request
from test_synthetic_contract_schema import check, SCHEMA

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.p = SyntheticProvider()
        self.scope = dict(dataset_id=self.p.dataset['dataset_id'], snapshot_id=self.p.dataset['snapshot_id'])
    def tearDown(self): self.p.close()
    def read(self,op,sort='id_asc',**values):
        q=request(op,**self.scope,**values)
        check(SCHEMA['$defs']['request'],q)
        result=self.p.read(q,sort)
        check(SCHEMA['$defs']['response'],result)
        return result
    def collect(self,op,**values):
        cursor=None; pages=[]
        while True:
            d=self.read(op,cursor=cursor,**values);self.assertEqual(d['status'],'OK');pages.append(d['result'])
            cursor=d['result']['page']['next_cursor']
            if cursor is None:return pages
    def test_capabilities_and_adapter_synthetic_injection(self):
        d=self.read('capabilities');self.assertFalse(d['result']['writes_supported'])
        q=request('search',**self.scope)
        self.assertEqual(ReadOnlyAdapter(self.p.read).read(q),self.p.read(q))
        self.assertEqual(ReadOnlyAdapter().read(q)['error']['code'],'CONNECTION_UNAVAILABLE')
        def failing(*_):raise OSError('private value must not leak')
        result=ReadOnlyAdapter(failing).read(q)
        self.assertNotIn('private',json.dumps(result));self.assertEqual(result['status'],'ERROR')
    def test_search_pages_filters_sort_and_no_body(self):
        rows=[r for p in self.collect('search') for r in p['records']]
        self.assertEqual(len(rows),7);self.assertEqual(len({r['record_id'] for r in rows}),7)
        self.assertTrue(all(r['raw_text'] is None and r['content_availability']=='NOT_REQUESTED' for r in rows))
        self.assertEqual([r['record_id'] for r in self.read('search',sort='id_desc')['result']['records']],sorted([r['record_id'] for r in rows],reverse=True)[:3])
        d=self.read('search',query='🧭 동쪽');self.assertEqual(len(d['result']['records']),1)
        d=self.read('search',project_id='synthetic-project-cloud',review_status='HELD',document_role='SYNTHETIC_NOTE')
        self.assertEqual(len(d['result']['records']),2)
        self.assertEqual(self.read('search',query='없는-합성-문구')['result']['records'],[])
        self.assertEqual(self.read('search',document_role='OTHER')['result']['records'],[])
    def test_cursor_bound_to_every_condition_and_restart(self):
        q=request('search',**self.scope);q['cursor']=self.p.read(q)['result']['page']['next_cursor']
        changes=[{'query':'가상'},{'page_size':2},{'project_id':'synthetic-project-cloud'},
                 {'document_role':'other'},{'review_status':'HELD'},{'snapshot_id':'old'}, {'dataset_id':'other'}]
        for change in changes:
            with self.subTest(change=change):self.assertEqual(self.p.read({**q,**change})['status'],'ERROR')
        self.assertEqual(self.p.read(q,'id_desc')['error']['code'],'VERSION_MISMATCH')
        self.assertEqual(self.p.read({**q,'cursor':q['cursor']+'x'})['error']['code'],'VERSION_MISMATCH')
        other=SyntheticProvider()
        try:self.assertEqual(other.read(q)['error']['code'],'VERSION_MISMATCH')
        finally:other.close()
    def test_cursor_is_opaque_and_state_has_only_keyed_context_not_query_fingerprint(self):
        q=request('search',**self.scope,query='가상',page_size=2)
        token=self.p.read(q)['result']['page']['next_cursor']
        self.assertRegex(token,r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(len(base64.urlsafe_b64decode(token+'=')),32)
        old_public_digest=hashlib.sha256(json.dumps({**q,'cursor':None,'ui_sort':'id_asc'},sort_keys=True).encode()).digest()
        state=self.p._cursors[token]
        self.assertEqual(len(state),4)
        self.assertEqual(type(state[0]),int)
        self.assertEqual(type(state[1]),bytes)
        self.assertEqual(len(state[1]),32)
        self.assertNotEqual(state[1],old_public_digest)
        self.assertNotIn(old_public_digest,base64.urlsafe_b64decode(token+'='))
        self.assertNotIn(q['query'],repr(self.p._cursors))
        other=SyntheticProvider()
        try:self.assertNotEqual(self.p._fingerprint(q,'id_asc'),other._fingerprint(q,'id_asc'))
        finally:other.close()
    def test_cursor_replay_and_back_navigation_reuse_handles_without_consuming(self):
        q=request('search',**self.scope,page_size=2)
        first=self.p.read(q)
        second_q={**q,'cursor':first['result']['page']['next_cursor']}
        second=self.p.read(second_q)
        self.assertEqual(self.p.read(second_q),second)
        self.assertEqual(self.p.read(q),first)
        self.assertEqual(len(self.p._cursors),2)
        self.assertEqual(self.p.read({**second_q,'query':''})['error']['code'],'VERSION_MISMATCH')
        self.assertEqual(self.p.read({**second_q,'query':' '})['error']['code'],'VERSION_MISMATCH')
        self.assertEqual(self.p.read(second_q),second)
        self.assertEqual(len(self.p._cursors),2)
    def test_cursor_ttl_is_fixed_at_issue_and_exact_expiry_is_rejected(self):
        q=request('search',**self.scope,page_size=2)
        with patch('app.provider.time.monotonic',return_value=100.0):
            token=self.p.read(q)['result']['page']['next_cursor']
        original_state=self.p._cursors[token]
        self.assertEqual(original_state[3]-original_state[2],15*60)
        with patch('app.provider.time.monotonic',return_value=999.0):
            self.assertEqual(self.p.read(q)['result']['page']['next_cursor'],token)
            self.assertEqual(self.p.read({**q,'cursor':token})['status'],'OK')
        self.assertEqual(self.p._cursors[token],original_state)
        with patch('app.provider.time.monotonic',return_value=1000.0):
            expired=self.p.read({**q,'cursor':token})
        self.assertEqual(expired['error']['code'],'VERSION_MISMATCH')
        self.assertNotIn(token,self.p._cursors)
    def test_cursor_capacity_is_explicit_and_does_not_evict_valid_handles(self):
        q=request('search',**self.scope,page_size=3)
        with patch.object(SyntheticProvider,'_MAX_CURSOR_HANDLES',1), \
             patch('app.provider.time.monotonic',return_value=100.0):
            first=self.p.read(q)
            token=first['result']['page']['next_cursor']
            original=dict(self.p._cursors)
            blocked=self.p.read({**q,'cursor':token})
            self.assertEqual(blocked['error']['code'],'CONNECTION_UNAVAILABLE')
            self.assertIsNone(blocked['result'])
            self.assertEqual(self.p._cursors,original)
            self.assertEqual(self.p.read(q),first)
            self.assertEqual(len(self.p._cursors),1)
        with patch.object(SyntheticProvider,'_MAX_CURSOR_HANDLES',1), \
             patch('app.provider.time.monotonic',return_value=1000.0):
            replacement=self.p.read(q)['result']['page']['next_cursor']
            self.assertNotEqual(replacement,token)
            self.assertEqual(len(self.p._cursors),1)
            self.assertEqual(self.p.read({**q,'cursor':token})['error']['code'],'VERSION_MISMATCH')
    def test_concurrent_cursor_issuance_reuses_one_bounded_live_handle(self):
        q=request('search',**self.scope,page_size=2)
        with patch.object(SyntheticProvider,'_MAX_CURSOR_HANDLES',1):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda _:self.p.read(q),range(8)))
        self.assertTrue(all(result['status']=='OK' for result in results))
        self.assertEqual(len({result['result']['page']['next_cursor'] for result in results}),1)
        self.assertEqual(len(self.p._cursors),1)
    def test_projects_mixed_pages_and_relationship_keys(self):
        pages=self.collect('projects',page_size=1)
        self.assertEqual(len(pages),4)
        flat=[x for p in pages for key in ('components','projects','relations') for x in p[key]]
        self.assertEqual(flat[0]['component_id'],'synthetic-component-reading')
        self.assertEqual(flat[-1]['to_id'],'synthetic-project-cloud')
        self.assertTrue(all(p['page']['sort']==['item_kind:asc','dataset_id:asc','item_id:asc'] for p in pages))
        filtered=self.read('projects',project_id='synthetic-project-clock')['result']
        self.assertEqual(filtered['components'],[]);self.assertEqual(filtered['relations'],[])
        self.assertEqual(len(filtered['projects']),1)
    def test_detail_states_sources_and_immutable_history(self):
        r=self.read('detail',record_id='synthetic-record-001')['result']
        self.assertIn('🧭',r['record']['raw_text']);self.assertEqual(len(r['sources']),2)
        self.assertGreater(len(r['record']['raw_text']),4000)
        missing=self.read('detail',record_id='synthetic-record-004')['result']
        self.assertEqual(missing['record']['source_status'],'MISSING')
        unassigned=self.read('detail',record_id='synthetic-record-003')['result']['record']
        self.assertEqual(unassigned['project_relations'],[]);self.assertEqual(unassigned['state']['adoption'],'UNKNOWN')
        pages=self.collect('related_history',record_id='synthetic-record-006',page_size=1)
        self.assertEqual(len(pages),3)
        self.assertEqual(pages[1]['events'][0]['supersedes'],pages[0]['events'][0]['event_id'])
        self.assertIsNone(pages[2]['related_records'][0]['raw_text'])
        self.assertEqual(self.read('detail',record_id='absent')['error']['code'],'ITEM_NOT_FOUND')
    def test_invalid_requests_rejected(self):
        base=request('search',**self.scope)
        invalid=[None,{}, {**base,'extra':1},{**base,'page_size':True},{**base,'page_size':101},
                 {**base,'query':[]},{**base,'snapshot_id':None},{**base,'operation':'write'}]
        for q in invalid:
            with self.subTest(q=q):self.assertEqual(self.p.read(q)['status'],'ERROR')
        self.assertEqual(self.p.read({**base,'contract_version':'other'})['error']['code'],'UNSUPPORTED_CONTRACT')
    def test_large_generated_dataset_stays_paged(self):
        large=SyntheticProvider(1000)
        try:
            q=request('search',dataset_id=large.dataset['dataset_id'],snapshot_id=large.dataset['snapshot_id'],page_size=100)
            seen=set();sizes=[]
            while True:
                d=large.read(q);self.assertEqual(d['status'],'OK');check(SCHEMA['$defs']['response'],d)
                sizes.append(len(json.dumps(d,ensure_ascii=False).encode()))
                for r in d['result']['records']:
                    self.assertNotIn(r['record_id'],seen);seen.add(r['record_id']);self.assertIsNone(r['raw_text'])
                q['cursor']=d['result']['page']['next_cursor']
                if not q['cursor']:break
            self.assertEqual(len(seen),7000);self.assertEqual(len(sizes),70);self.assertLess(max(sizes),250000)
            detail=large.read(request('detail',dataset_id=large.dataset['dataset_id'],snapshot_id=large.dataset['snapshot_id'],record_id='synthetic-record-001-copy-00999'))
            self.assertEqual(detail['status'],'OK')
        finally:large.close()

if __name__=='__main__':unittest.main()
