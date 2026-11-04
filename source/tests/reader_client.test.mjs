import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {ReadClient,LatestRequest,sourceRange,makeRequest} from '../../app/web/client.mjs';
const examples=JSON.parse(await readFile(new URL('../../fixtures/synthetic/contract_examples.json',import.meta.url)));
const record=examples.detail.result.record, source=examples.detail.result.sources[0];
test('UTF-16 emoji range requires matching body and fragment hashes',async()=>{
 const range=await sourceRange(record.raw_text,source,record.raw_text_sha256,webcrypto);
 assert.equal(range.ok,true);assert.equal(range.fragment,'🧭 동쪽');
 assert.equal((await sourceRange(record.raw_text+'x',source,record.raw_text_sha256,webcrypto)).ok,false);
 assert.equal((await sourceRange(record.raw_text,{...source,location:{...source.location,fragment_sha256:'0'.repeat(64)}},record.raw_text_sha256,webcrypto)).ok,false);
});
test('source absence, old version, unsupported units and split surrogate never highlight',async()=>{
 const bad=[{...source,availability:'MISSING'}, {...source,version:null},
 {...source,location:{...source.location,unit:'byte'}},
 {...source,location:{...source.location,start:source.location.start+1}},
 {...source,location:{...source.location,end:record.raw_text.length+1}},
 {...source,location:{...source.location,target_sha256:'0'.repeat(64)}}];
 for(const s of bad)assert.equal((await sourceRange(record.raw_text,s,record.raw_text_sha256,webcrypto)).ok,false);
});
test('empty available body is distinct from missing',async()=>{
 const hash=Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256',new Uint8Array())),x=>x.toString(16).padStart(2,'0')).join('');
 const s={...source,content_sha256:hash,location:{...source.location,start:0,end:0,target_sha256:hash,fragment_sha256:hash}};
 assert.equal((await sourceRange('',s,hash,webcrypto)).ok,true);
 assert.equal((await sourceRange(null,s,hash,webcrypto)).ok,false);
});
test('request generation blocks stale responses even if fetch ignores abort',()=>{
 const gate=new LatestRequest(),first=gate.begin(),second=gate.begin();assert.equal(first.current(),false);assert.equal(first.signal.aborted,true);assert.equal(second.current(),true);gate.cancel();assert.equal(second.current(),false);
});
test('client propagates errors and refuses different snapshots',async()=>{
 let envelope=examples.connection_failure;
 const client=new ReadClient(async()=>({ok:true,json:async()=>envelope}));
 await assert.rejects(client.read('search'),e=>e.code==='CONNECTION_UNAVAILABLE');
 envelope=examples.detail;client.dataset={...envelope.snapshot,snapshot_id:'another'};
 await assert.rejects(client.read('detail'),e=>e.code==='VERSION_MISMATCH');
 const q=makeRequest('search');assert.equal(q.query,null);assert.equal(q.cursor,null);
});
