import {validateResponse} from './response-validation.mjs';
export const VERSION = 'UTT-ACCESS-READ/0.1.0';
export function makeRequest(operation, values={}) {
  return {contract_version:VERSION,operation,snapshot_id:null,dataset_id:null,project_id:null,
    record_id:null,query:null,document_role:null,review_status:null,page_size:3,cursor:null,...values};
}
export class ReadClient {
  constructor(fetcher=globalThis.fetch.bind(globalThis)) { this.fetcher=fetcher; this.dataset=null; this.scenario='normal'; this.sort='id_asc'; }
  async read(operation, values={}, signal) {
    const request=Object.freeze(makeRequest(operation,{dataset_id:this.dataset?.dataset_id??null,snapshot_id:this.dataset?.snapshot_id??null,...values}));
    const response=await this.fetcher('/api/read',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({request,sort:this.sort,scenario:this.scenario}),signal});
    if(!response.ok) throw Object.assign(Error('읽기 서버에 연결할 수 없습니다.'),{code:'CONNECTION_UNAVAILABLE'});
    let envelope;
    try{envelope=await response.json();}catch{throw Object.assign(Error('읽기 응답이 올바른 JSON이 아닙니다.'),{code:'INVALID_RESPONSE'});}
    validateResponse(envelope,request,VERSION);
    if(envelope.status!=='OK') throw Object.assign(Error(envelope.error?.message??'읽기 실패'),{code:envelope.error?.code});
    return envelope;
  }
}
// Abort is an optimization; generation guards also reject already-resolved stale work.
export class LatestRequest {
  constructor(){this.generation=0;this.controller=null;}
  begin(){this.controller?.abort();this.controller=new AbortController();const generation=++this.generation;
    return {signal:this.controller.signal,current:()=>generation===this.generation};}
  cancel(){this.controller?.abort();++this.generation;}
}
export async function sourceRange(text, source, bodyHash, cryptoApi=globalThis.crypto) {
  const fail=reason=>({ok:false,reason});
  const location=source.location;
  if(typeof text!=='string'||source.availability!=='AVAILABLE'||!location) return fail('출처 또는 본문이 없어 좌표를 적용하지 않습니다.');
  if(location.target!=='SYNTHETIC_SOURCE'||location.unit!=='UTF-16'||location.boundary!=='half_open') return fail('이 화면이 지원하지 않는 대상·좌표 단위입니다.');
  if(!source.version || !bodyHash || !location.target_sha256 || !source.content_sha256 || !location.fragment_sha256)
    return fail('버전·해시 근거가 부족합니다.');
  const hash=async value=>Array.from(new Uint8Array(await cryptoApi.subtle.digest('SHA-256',new TextEncoder().encode(value))),x=>x.toString(16).padStart(2,'0')).join('');
  const actual=await hash(text);
  if(actual!==bodyHash || actual!==location.target_sha256 || actual!==source.content_sha256) return fail('본문 버전 또는 해시 불일치: 좌표를 적용하지 않습니다.');
  const {start,end}=location;
  const splitSurrogate=i=>i>0&&i<text.length&&/[\uD800-\uDBFF]/.test(text[i-1])&&/[\uDC00-\uDFFF]/.test(text[i]);
  if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||end<start||end>text.length||splitSurrogate(start)||splitSurrogate(end)) return fail('UTF-16 범위 또는 이모지 경계가 잘못됐습니다.');
  if(await hash(text.slice(start,end))!==location.fragment_sha256) return fail('출처 조각 해시 불일치입니다.');
  return {ok:true,start,end,fragment:text.slice(start,end)};
}
