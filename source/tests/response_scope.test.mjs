import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {ReadClient,LatestRequest} from '../../app/web/client.mjs';
const examples=JSON.parse(await readFile(new URL('../../fixtures/synthetic/contract_examples.json',import.meta.url)));
const clone=value=>structuredClone(value);
const scope=examples.detail.snapshot;
function clientFor(envelope){const client=new ReadClient(async()=>({ok:true,json:async()=>clone(envelope)}));client.dataset=clone(scope);return client;}
const rejected=(promise,code)=>assert.rejects(promise,e=>e.code===code);

test('different dataset with same snapshot is refused against the sent request',async()=>{
 const d=clone(examples.detail);d.snapshot.dataset_id='synthetic-other';
 await rejected(clientFor(d).read('detail',{record_id:d.result.record.record_id}),'RESPONSE_SCOPE_MISMATCH');
});
test('same dataset with different snapshot is refused',async()=>{
 const d=clone(examples.detail);d.snapshot.snapshot_id='synthetic-other-version';
 await rejected(clientFor(d).read('detail',{record_id:d.result.record.record_id}),'VERSION_MISMATCH');
});
test('explicit request scope takes precedence over mutable client selection',async()=>{
 const d=clone(examples.detail),client=clientFor(d);client.dataset={...scope,dataset_id:'other',snapshot_id:'other'};
 assert.equal((await client.read('detail',{dataset_id:scope.dataset_id,snapshot_id:scope.snapshot_id,record_id:d.result.record.record_id})).status,'OK');
});
test('null initial capabilities scope still checks envelope and embedded dataset',async()=>{
 const d=clone(examples.capabilities),client=clientFor(d);client.dataset=null;
 assert.equal((await client.read('capabilities')).status,'OK');
 d.result.dataset.dataset_id='other';const wrongDataset=clientFor(d);wrongDataset.dataset=null;
 await rejected(wrongDataset.read('capabilities'),'RESPONSE_SCOPE_MISMATCH');
 const bad=clone(examples.capabilities);bad.result.dataset.snapshot_id='other';
 const wrongVersion=clientFor(bad);wrongVersion.dataset=null;
 await rejected(wrongVersion.read('capabilities'),'VERSION_MISMATCH');
});
test('initial dataset-bound capabilities is not a blanket scope exemption',async()=>{
 const d=clone(examples.capabilities),client=clientFor(d);client.dataset=null;
 await rejected(client.read('capabilities',{dataset_id:'other'}),'RESPONSE_SCOPE_MISMATCH');
});
test('all operation collections reject mixed internal dataset scopes',async()=>{
 const cases=[['search_page_1',d=>d.result.records[0].dataset_id='other'],
 ['detail',d=>d.result.record.dataset_id='other'],['detail',d=>d.result.sources[0].dataset_id='other'],
 ['detail',d=>d.result.record.source_refs[0].dataset_id='other'],['detail',d=>d.result.related_projects[0].dataset_id='other'],
 ['projects',d=>d.result.projects[0].dataset_id='other'],['history',d=>d.result.events[0].dataset_id='other'],
 ['history',d=>d.result.related_records[0].dataset_id='other']];
 for(const [name,mutate] of cases){const d=clone(examples[name]);mutate(d);
  await rejected(clientFor(d).read(d.operation,{record_id:examples.detail.result.record.record_id}),'RESPONSE_SCOPE_MISMATCH');}
 const d=clone(examples.projects);d.result.components=[{component_id:'synthetic-component',dataset_id:'other',name:'가상 구성요소',parent_component_id:null,relationship_status:'OBSERVED'}];
 await rejected(clientFor(d).read('projects'),'RESPONSE_SCOPE_MISMATCH');
});
test('relations respect local anchors without inventing a dataset_id field',async()=>{
 const d=clone(examples.detail),link=d.result.record.project_relations[0];
 link.to_dataset_id='synthetic-external';
 assert.equal((await clientFor(d).read('detail',{record_id:d.result.record.record_id})).status,'OK');
 link.from_dataset_id='other';await rejected(clientFor(d).read('detail',{record_id:d.result.record.record_id}),'RESPONSE_SCOPE_MISMATCH');
 const mixed=clone(examples.projects);mixed.result.relations=[{...link}];
 await rejected(clientFor(mixed).read('projects'),'RESPONSE_SCOPE_MISMATCH');
});
test('detail id mismatch and malformed envelopes have distinct local errors',async()=>{
 await rejected(clientFor(examples.detail).read('detail',{record_id:'synthetic-different'}),'RESPONSE_SCOPE_MISMATCH');
 for(const patch of [{status:'maybe'},{result:null},{snapshot:null},{error:{}},{operation:'search'}])
  await rejected(clientFor({...examples.detail,...patch}).read('detail',{record_id:examples.detail.result.record.record_id}),'INVALID_RESPONSE');
 const d=clone(examples.connection_failure);d.result={};
 await rejected(clientFor(d).read('search'),'INVALID_RESPONSE');
});
test('valid old-dataset reply is validated against captured request then discarded by generation',async()=>{
 let resolve;const gate=new LatestRequest(),old=gate.begin();
 const client=new ReadClient(async()=>({ok:true,json:()=>new Promise(r=>resolve=r)}));client.dataset=clone(scope);
 const pending=client.read('detail',{record_id:examples.detail.result.record.record_id},old.signal);
 await Promise.resolve();
 client.dataset={...scope,dataset_id:'synthetic-next',snapshot_id:'next'};const latest=gate.begin();
 resolve(clone(examples.detail));const response=await pending;
 assert.equal(response.snapshot.dataset_id,scope.dataset_id);assert.equal(old.current(),false);assert.equal(latest.current(),true);
});
test('a late reply matching the new selection is rejected if the old request asked for another dataset',async()=>{
 let resolve;const client=new ReadClient(async()=>({ok:true,json:()=>new Promise(r=>resolve=r)}));client.dataset=clone(scope);
 const pending=client.read('detail',{record_id:examples.detail.result.record.record_id});await Promise.resolve();
 const d=clone(examples.detail);d.snapshot.dataset_id='synthetic-next';client.dataset={...scope,dataset_id:'synthetic-next'};
 resolve(d);await rejected(pending,'RESPONSE_SCOPE_MISMATCH');
});
