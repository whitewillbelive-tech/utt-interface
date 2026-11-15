import test from 'node:test';
import assert from 'node:assert/strict';
import {ReadClient} from '../../app/web/client.mjs';

const scope={dataset_id:'fictional-data',snapshot_id:'fictional-snapshot',record_id:'fictional-record'};
const response=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>value});
const blank=()=>Object.fromEntries(['projects','topics','sources','decisions','revisions','events','notes','tags','proposals','merged_children'].map(k=>[k,[]]));
const detail=()=>({contract_version:'UTT-WORKSPACE-DETAIL/0.1',status:'OK',...scope,
 record:{record_id:scope.record_id,version:2,archived_at:'fictional-time',merged_into:null},...blank()});

test('workspace capabilities are explicit and absent endpoint is not fabricated',async()=>{
 const absent=new ReadClient(async()=>response({},404));assert.equal(await absent.workspaceFeatures(),null);
 const f={contract_version:'UTT-WORKSPACE-FEATURES/0.1',read_detail:true,writes_supported:false};
 const client=new ReadClient(async()=>response(f));assert.deepEqual(await client.workspaceFeatures(),f);
 await assert.rejects(new ReadClient(async()=>response({...f,extra:'x'})).workspaceFeatures(),e=>e.code==='INVALID_RESPONSE');
});

test('workspace detail is bound to dataset, snapshot and record; original rows are preserved',async()=>{
 const v=detail();v.sources=[{record_id:scope.record_id,source_id:null,locator:''},{record_id:scope.record_id,source_id:'two',locator:'p2'}];
 let sent;
 const client=new ReadClient(async(path,options)=>{assert.equal(path,'/api/workspace/read');sent=JSON.parse(options.body);return response(v);});
 assert.deepEqual(await client.workspaceDetail(scope),v);
 assert.deepEqual(sent,{contract_version:'UTT-WORKSPACE-READ/0.1',operation:'detail',...scope});
 for(const key of Object.keys(scope)){
  const bad=detail();bad[key]='fictional-other';
  await assert.rejects(new ReadClient(async()=>response(bad)).workspaceDetail(scope),e=>e.code==='RESPONSE_SCOPE_MISMATCH');
 }
 const bad=detail();bad.record.record_id='fictional-other';
 await assert.rejects(new ReadClient(async()=>response(bad)).workspaceDetail(scope),e=>e.code==='INVALID_RESPONSE');
});

test('lifecycle requires same snapshot and exactly the requested records without duplicates',async()=>{
 const identity={dataset_id:scope.dataset_id,snapshot_id:scope.snapshot_id,record_ids:['one','two']};
 const value={contract_version:'UTT-WORKSPACE-LIFECYCLE/0.1',snapshot:{dataset_id:scope.dataset_id,snapshot_id:scope.snapshot_id},records:[
  {record_id:'two',archived_at:'fictional-time',merged_into:null},{record_id:'one',archived_at:null,merged_into:'two'}]};
 let sent;const client=new ReadClient(async(path,opts)=>{assert.equal(path,'/api/workspace/lifecycle');sent=JSON.parse(opts.body);return response(value);});
 const mapped=await client.workspaceLifecycle(identity);assert.equal(mapped.get('one').merged_into,'two');assert.equal(mapped.get('two').archived_at,'fictional-time');
 assert.deepEqual(sent,{contract_version:'UTT-WORKSPACE-LIFECYCLE/0.1',...identity});
 for(const mutation of [v=>v.snapshot.snapshot_id='other',v=>v.records[0].record_id='other',v=>v.records[0].archived_at=undefined,v=>v.records[0].record_id='one']){
  const v=structuredClone(value);mutation(v);
  await assert.rejects(new ReadClient(async()=>response(v)).workspaceLifecycle(identity));
 }
});

test('workspace project catalog and members are bound to exact snapshot and identifier',async()=>{
 const identity={dataset_id:scope.dataset_id,snapshot_id:scope.snapshot_id};
 const catalog={contract_version:'UTT-WORKSPACE-DETAIL/0.1',status:'OK',operation:'projects',kind:'projects',...identity,items:[{project_id:'p',name:'가상 프로젝트'}],next_offset:null};
 let sent;const client=new ReadClient(async(path,opts)=>{assert.equal(path,'/api/workspace/catalog');sent=JSON.parse(opts.body);return response(catalog);});
 assert.deepEqual(await client.workspaceCatalog(identity,'projects'),catalog);
 assert.deepEqual(sent,{contract_version:'UTT-WORKSPACE-READ/0.1',operation:'projects',...identity,limit:50,offset:0});
 const members={contract_version:'UTT-WORKSPACE-DETAIL/0.1',status:'OK',operation:'project_members',kind:'project',...identity,items:[{link:{project_id:'p',record_id:'r',basis:'CANDIDATE'},record:{record_id:'r',title:'가상 자료',version:1,archived_at:null,merged_into:null}}],next_offset:null};
 const membersClient=new ReadClient(async()=>response(members));assert.deepEqual(await membersClient.workspaceCatalog(identity,'project_members',{identifier:'p'}),members);
 const wrong=structuredClone(members);wrong.items[0].link.project_id='other';
 await assert.rejects(new ReadClient(async()=>response(wrong)).workspaceCatalog(identity,'project_members',{identifier:'p'}),e=>e.code==='RESPONSE_SCOPE_MISMATCH');
 const stale=structuredClone(catalog);stale.snapshot_id='other';
 await assert.rejects(new ReadClient(async()=>response(stale)).workspaceCatalog(identity,'projects'),e=>e.code==='RESPONSE_SCOPE_MISMATCH');
});
