import test from 'node:test';
import assert from 'node:assert/strict';
import {ReadClient} from '../../app/web/client.mjs';

test('HTTP permission and authentication errors stay distinct from connection loss',async()=>{
 for(const [status,code] of [[401,'AUTHENTICATION_REQUIRED'],[403,'ACCESS_DENIED'],[503,'CONNECTION_UNAVAILABLE'],[404,'HTTP_ERROR']]){
  const client=new ReadClient(async()=>({ok:false,status,json:()=>{throw Error('must not read an unvalidated error body');}}));
  await assert.rejects(client.read('capabilities'),e=>e.code===code&&e.httpStatus===status);
  await assert.rejects(client.readMode(),e=>e.code===code);
 }
});
test('network exceptions are sanitized and abort stays an abort',async()=>{
 const client=new ReadClient(async()=>{throw TypeError('fictional-internal-endpoint');});
 await assert.rejects(client.read('capabilities'),e=>e.code==='CONNECTION_UNAVAILABLE'&&!e.message.includes('fictional-internal'));
 const aborted=new DOMException('cancelled','AbortError');
 const cancelled=new ReadClient(async()=>{throw aborted;});
 await assert.rejects(cancelled.read('capabilities'),e=>e===aborted);
 const interruptedBody=new ReadClient(async()=>({ok:true,json:async()=>{throw aborted;}}));
 await assert.rejects(interruptedBody.read('capabilities'),e=>e===aborted);
});
test('mode cannot silently default to synthetic or continue after malformed response',async()=>{
 for(const info of [null,{}, {mode:'unknown'}]){
  const client=new ReadClient(async()=>({ok:true,json:async()=>info}));
  await assert.rejects(client.readMode(),e=>e.code==='INVALID_RESPONSE');
 }
 for(const mode of ['synthetic','readonly']){
  const client=new ReadClient(async()=>({ok:true,json:async()=>({mode})}));
  assert.equal((await client.readMode()).mode,mode);
 }
});
