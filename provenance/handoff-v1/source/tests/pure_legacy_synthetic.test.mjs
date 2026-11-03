// Entirely fictional records; no input file, database, network, or cache.
import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) Object.defineProperty(globalThis, "crypto", {value:webcrypto});
import {ReadModel,buildSearchIndex} from '../safe_modules/readmodel/readmodel.mjs';
import {blocks} from '../safe_modules/readmodel/readmodel-parser.mjs';
import {canonicalView} from '../safe_modules/readmodel/canonical-view.mjs';
function record(id,title,text,projects,state='CONFIRMED') {
 return {record_id:id,title,document_role:'knowledge',original_ids:[id],group_links:[],body_blocks:[{block_id:id+'-block',text,searchable:true,locator:null}],project_links:projects.map(project_id=>({project_id,confirmation:'confirmed_in_stored_data',block_ids:null})),classification:{original_type:'CONCEPT',original_domain:'SYNTHETIC'},stored_state:{status:state},parse_status:'parsed',keywords:[],source_refs:[],review_issues:[]};
}
const rows=[record('fiction-r-a','가상 북쪽','은빛 구름', ['fiction-p-a','fiction-p-b']),record('fiction-r-b','가상 남쪽','은빛 기차',['fiction-p-b']),record('fiction-r-c','가상 동쪽','노란 산',[],'PENDING')];
const dataset={dataset_id:'fiction-dataset',dataset_kind:'SYNTHETIC'};
const model=new ReadModel(dataset,rows,{searchIndex:buildSearchIndex(rows)});
test('search and project filter share a result but exclude unrelated records',()=>{
 let r=model.search('은빛',{project:'fiction-p-a'});assert.equal(r.total,1);assert.equal(r.items[0].key,'fiction-r-a');
 assert.equal(model.search('은빛',{project:'fiction-p-b'}).total,2);
});
test('empty search result and paging are explicit',()=>{
 assert.equal(model.search('초록 고래').total,0);
 const a=model.search('은빛',{offset:0,limit:1});const b=model.search('은빛',{offset:1,limit:1});assert.equal(a.total,2);assert.equal(b.total,2);assert.notEqual(a.items[0].key,b.items[0].key);
});
test('unassigned pending record remains visible without project filter',()=>{
 let r=model.search('',{pending:true});assert.deepEqual(r.items.map(x=>x.key),['fiction-r-c']);
});
test('decoded text UTF-16 offsets follow emoji boundaries',async()=>{
 const raw='별🌙과 구름\n';const b=await blocks(raw);assert.equal(b[0].locator.offset_unit,'UTF-16');assert.equal(b[0].locator.start,0);assert.equal(b[0].locator.end,raw.length);assert.equal(raw.slice(b[0].locator.start,b[0].locator.end),raw);
});
test('canonical consumer rejects unsupported input',()=>assert.throws(()=>canonicalView({contract:'fiction-unsupported'}),/CANONICAL_CONSUMER_INVALID/));
