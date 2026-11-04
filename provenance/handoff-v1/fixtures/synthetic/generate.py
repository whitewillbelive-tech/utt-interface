#!/usr/bin/env python3
"""Build synthetic test data locally. Never reads user paths or input files."""
import argparse
import hashlib
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

def u16len(value):
    return len(value.encode('utf-16-le')) // 2

def create():
    text = '가상의 자료 🧭 동쪽 지점은 시험을 위해 만든 표식입니다. '
    text += ('여기는 실제 대화나 문서를 사용하지 않고 작성한 긴 한국어 문장입니다. ' * 100)
    target = '🧭 동쪽'
    cp_start = text.index(target)
    start = u16len(text[:cp_start])
    end = start + u16len(target)
    sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return {
        'fixture_marker': 'SYNTHETIC_ONLY',
        'snapshot': 'synthetic-snapshot-001',
        'projects': [
            {'id': 'synthetic-project-cloud', 'name': '구름 정원'},
            {'id': 'synthetic-project-clock', 'name': '모래 시계'},
        ],
        'sources': [
            {'id': 'synthetic-source-one', 'version_sha256': sha, 'text': text},
            {'id': 'synthetic-source-missing', 'missing': True, 'text': None},
        ],
        'records': [
            {'id':'synthetic-record-001','title':'가상 관측','source_id':'synthetic-source-one','review_state':'unreviewed','proposal_state':'none','canon_state':'not_approved','observed_state':'observed'},
            {'id':'synthetic-record-002','title':'가상 교차 관계','source_id':'synthetic-source-one','review_state':'held','proposal_state':'draft','canon_state':'not_approved','observed_state':'observed'},
            {'id':'synthetic-record-003','title':'가상 미분류','source_id':None,'review_state':'unreviewed','proposal_state':'none','canon_state':'unknown','observed_state':'observed'},
            {'id':'synthetic-record-004','title':'가상 출처 누락','source_id':'synthetic-source-missing','review_state':'evidence_requested','proposal_state':'none','canon_state':'not_approved','observed_state':'observed'},
            {'id':'synthetic-record-005','title':'가상 충돌 보류','source_id':'synthetic-source-one','review_state':'held','proposal_state':'conflict','canon_state':'not_approved','observed_state':'conflicting'},
            {'id':'synthetic-record-006','title':'가상 이력','source_id':'synthetic-source-one','review_state':'cancelled','proposal_state':'none','canon_state':'not_approved','observed_state':'observed'},
            {'id':'synthetic-record-007','title':'가상 빈 설명','source_id':'synthetic-source-one','review_state':'unreviewed','proposal_state':'none','canon_state':'unknown','observed_state':'observed','summary':''},
        ],
        'project_links': [
            {'record_id':'synthetic-record-001','project_id':'synthetic-project-cloud','status':'confirmed'},
            {'record_id':'synthetic-record-002','project_id':'synthetic-project-cloud','status':'observed'},
            {'record_id':'synthetic-record-002','project_id':'synthetic-project-clock','status':'unconfirmed'},
            {'record_id':'synthetic-record-004','project_id':'synthetic-project-clock','status':'observed'},
            {'record_id':'synthetic-record-005','project_id':'synthetic-project-cloud','status':'conflict'},
            {'record_id':'synthetic-record-006','project_id':'synthetic-project-clock','status':'observed'},
            {'record_id':'synthetic-record-007','project_id':'synthetic-project-clock','status':'observed'},
        ],
        'history': [
            {'id':'synthetic-event-001','record_id':'synthetic-record-006','type':'hold','supersedes':None},
            {'id':'synthetic-event-002','record_id':'synthetic-record-006','type':'cancel','supersedes':'synthetic-event-001'},
        ],
        'location': {'source_id':'synthetic-source-one','unit':'utf16_code_units','start':start,'end':end,'boundary':'half_open','text':target,'source_sha256':sha},
        'scenarios': [
            {'id':'empty_search','kind':'success','expected_total':0},
            {'id':'source_missing','kind':'error','expected_code':'SOURCE_MISSING'},
            {'id':'connection_failure','kind':'error','expected_code':'CONNECTION_UNAVAILABLE'},
            {'id':'unsupported_contract','kind':'error','expected_code':'UNSUPPORTED_CONTRACT'},
            {'id':'snapshot_changed','kind':'error','expected_code':'VERSION_MISMATCH'},
        ],
    }

def contract_examples(f):
    """Make examples for the draft; no connection or database is used."""
    ds = {'dataset_id':'synthetic-dataset-001', 'dataset_kind':'SYNTHETIC',
          'snapshot_id':f['snapshot'], 'source_version':'synthetic-v1', 'legacy_contracts':[]}
    state = {'observation':'OBSERVED','review':'UNREVIEWED','proposal':'NONE','adoption':'NOT_APPROVED'}
    projects = [{'project_id':p['id'],'dataset_id':ds['dataset_id'],'name':p['name'],
                 'aliases':[],'former_names':[],'classification_observed':None,
                 'membership_user_confirmed':None,'state':state.copy()} for p in f['projects']]
    links = [{'relation_id':'synthetic-relation-%03d' % (index+1),
              'from_id':a['record_id'], 'to_id':a['project_id'], 'relation_kind':'BELONGS_TO',
              'scope':'PROJECT','assertion_status':{'confirmed':'USER_CONFIRMED','observed':'OBSERVED',
              'unconfirmed':'UNRESOLVED','conflict':'UNRESOLVED'}[a['status']], 'evidence_ids':[],
              'from_dataset_id':ds['dataset_id'],'to_dataset_id':ds['dataset_id']} for index,a in enumerate(f['project_links'])]
    src = f['sources'][0]; loc = f['location']
    location = {'target':'SYNTHETIC_SOURCE', 'unit':'UTF-16','start':loc['start'],'end':loc['end'],
                'boundary':'half_open','target_sha256':loc['source_sha256'],
                'fragment_sha256':hashlib.sha256(loc['text'].encode('utf-8')).hexdigest(),'verification':'VERIFIED'}
    available = {'source_id':src['id'],'dataset_id':ds['dataset_id'],'availability':'AVAILABLE',
                 'version':'synthetic-source-v1','content_sha256':src['version_sha256'],
                 'location':location,'verification':'VERIFIED'}
    missing = {'source_id':'synthetic-source-missing','dataset_id':ds['dataset_id'],'availability':'MISSING',
               'version':None,'content_sha256':None,'location':None,'verification':'UNKNOWN'}
    records = []
    for r in f['records']:
        source_refs = [] if r['source_id'] is None else ([missing] if r['source_id']==missing['source_id'] else [available])
        s = {'observation':r['observed_state'].upper(),'review':r['review_state'].upper(),
             'proposal':r['proposal_state'].upper(),'adoption':r['canon_state'].upper()}
        content = src['text'] if r['id']=='synthetic-record-001' else None
        records.append({'record_id':r['id'],'dataset_id':ds['dataset_id'],'original_ids':[],
                        'title':r['title'],'document_role':'SYNTHETIC_NOTE','state':s,
                        'display_summary':r.get('summary'),
                        'project_relations':[x for x in links if x['from_id']==r['id']],
                        'project_relations_status':'AVAILABLE','source_refs':source_refs,
                        'source_status':'UNKNOWN' if r['source_id'] is None else ('MISSING' if r['source_id']==missing['source_id'] else 'AVAILABLE'),
                        'content_availability':'AVAILABLE' if content is not None else 'UNKNOWN',
                        'raw_text':content,'raw_text_sha256':src['version_sha256'] if content is not None else None})
    history = [{'event_id':e['id'],'dataset_id':ds['dataset_id'],'record_id':e['record_id'],
                'event_contract':'SYNTHETIC-DECISION/1','event_kind':e['type'].upper(),
                'decision':None,'supersedes':e['supersedes'],'provenance_diagnostic':None,
                'event_payload_preservation':'UNTOUCHED'} for e in f['history']]
    def page(next_cursor, mixed=False):
        order = ['item_kind:asc','dataset_id:asc','item_id:asc'] if mixed else ['dataset_id:asc','record_id:asc']
        return {'page_size':3,'next_cursor':next_cursor,'sort':order}
    def ok(operation,result):
        return {'contract_version':'UTT-ACCESS-READ/0.1.0','operation':operation,'status':'OK',
                'snapshot':ds,'result':result,'error':None}
    def err(code):
        return {'contract_version':'UTT-ACCESS-READ/0.1.0','operation':'search','status':'ERROR',
                'snapshot':None,'result':None,'error':{'code':code,'message':'합성 오류 예시',
                'retryable':code=='CONNECTION_UNAVAILABLE'}}
    request = {'contract_version':'UTT-ACCESS-READ/0.1.0','operation':'search',
               'snapshot_id':ds['snapshot_id'],'dataset_id':ds['dataset_id'],'project_id':None,
               'record_id':None,'query':'가상','document_role':None,'review_status':None,
               'page_size':3,'cursor':None}
    return {'request_search':request,
            'capabilities':ok('capabilities',{'dataset':ds,'operations':['capabilities','projects','search','detail','related_history'],
                           'sort_order':['dataset_id:asc','record_id:asc'],'max_page_size':100,'writes_supported':False}),
            'projects':ok('projects',{'projects':projects,'components':[],'relations':[], 'page':page(None,True)}),
            'search_page_1':ok('search',{'records':records[:3],'page':page('synthetic-cursor-1')}),
            'search_page_2':ok('search',{'records':records[3:6],'page':page('synthetic-cursor-2')}),
            'search_page_3':ok('search',{'records':records[6:],'page':page(None)}),
            'search_empty':ok('search',{'records':[],'page':page(None)}),
            'detail':ok('detail',{'record':records[0],'related_projects':projects[:1],'sources':[available]}),
            'history':ok('related_history',{'related_records':[records[5]],'relations':[],
                         'events':history,'page':page(None,True),'history_status':'AVAILABLE'}),
            'source_missing':err('SOURCE_MISSING'), 'connection_failure':err('CONNECTION_UNAVAILABLE'),
            'unsupported_contract':err('UNSUPPORTED_CONTRACT'), 'snapshot_changed':err('VERSION_MISMATCH')}

def main():
    parser = argparse.ArgumentParser(description='Create wholly synthetic test fixtures')
    parser.add_argument('--output-dir', help='new output location; fails if fixture files already exist')
    args = parser.parse_args()
    outdir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix='utt-synthetic-'))
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / 'fixture.json'
    example_path = outdir / 'contract_examples.json'
    if path.exists() or path.is_symlink() or example_path.exists() or example_path.is_symlink():
        raise SystemExit('Refusing to overwrite an existing fixture file')
    fixture = create()
    with path.open('x',encoding='utf-8') as file:
        file.write(json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    with example_path.open('x',encoding='utf-8') as file:
        file.write(json.dumps(contract_examples(fixture),ensure_ascii=False,indent=2,sort_keys=True)+'\n')
    print('Synthetic fixtures created in:', outdir)

if __name__ == '__main__':
    main()
