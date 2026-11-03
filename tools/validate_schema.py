"""Independent full Draft 2020-12 syntax/response validation on synthetic data."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jsonschema import Draft202012Validator
from app.provider import SyntheticProvider, request

root=Path(__file__).resolve().parents[1]
schema=json.loads((root/'contracts/utt-read-api.schema.json').read_text())
Draft202012Validator.check_schema(schema)
validators={k:Draft202012Validator({'$schema':schema['$schema'],'$defs':schema['$defs'],'$ref':'#/$defs/'+k}) for k in ['request','response']}
count=0
examples=json.loads((root/'fixtures/synthetic/contract_examples.json').read_text())
for name,value in examples.items():
    validators['request' if name.startswith('request_') else 'response'].validate(value);count+=1
p=SyntheticProvider(2)
try:
    scope=dict(dataset_id=p.dataset['dataset_id'],snapshot_id=p.dataset['snapshot_id'])
    for op in ['capabilities','projects','search','detail','related_history']:
        q=request(op,**scope,record_id='synthetic-record-006-copy-00001',page_size=1)
        while True:
            validators['request'].validate(q);count+=1
            d=p.read(q);validators['response'].validate(d);count+=1
            assert d['status']=='OK'
            page=d['result'].get('page')
            if not page or not page['next_cursor']:break
            q['cursor']=page['next_cursor']
    for q in [request('detail',**scope,record_id='absent'),request('search',**{**scope,'snapshot_id':'old'})]:
        d=p.read(q);assert d['status']=='ERROR';validators['response'].validate(d);count+=1
finally:p.close()
print(f'Draft 2020-12: schema valid; {count} synthetic request/response validations passed.')
