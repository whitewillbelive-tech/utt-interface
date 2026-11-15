import {validateResponse,validateSourceResponse} from './response-validation.mjs';
export const VERSION = 'UTT-ACCESS-READ/0.1.0';
export function makeRequest(operation, values={}) {
  return {contract_version:VERSION,operation,snapshot_id:null,dataset_id:null,project_id:null,
    record_id:null,query:null,document_role:null,review_status:null,page_size:3,cursor:null,...values};
}
export class ReadClient {
  constructor(fetcher=globalThis.fetch.bind(globalThis)) { this.fetcher=fetcher; this.dataset=null; this.scenario='normal'; this.sort='id_asc'; }
  async fetchResponse(path, options, acceptedStatuses=[]) {
    let response;
    try { response=await this.fetcher(path,options); }
    catch(error) {
      if(error.name==='AbortError')throw error;
      throw Object.assign(Error('읽기 서버에 연결할 수 없습니다.'),{code:'CONNECTION_UNAVAILABLE'});
    }
    if(!response.ok&&!acceptedStatuses.includes(response.status)) {
      // HTTP status is transport evidence, not a new read-contract error enum.
      const code=response.status===401?'AUTHENTICATION_REQUIRED':response.status===403?'ACCESS_DENIED':
        [502,503,504].includes(response.status)?'CONNECTION_UNAVAILABLE':'HTTP_ERROR';
      throw Object.assign(Error('읽기 요청을 처리하지 못했습니다.'),{code,httpStatus:response.status});
    }
    return response;
  }
  async readMode(signal) {
    const response=await this.fetchResponse('/api/mode',{signal});
    let info;
    try { info=await response.json(); }
    catch(error) {
      if(error.name==='AbortError')throw error;
      throw Object.assign(Error('실행 모드 응답을 읽을 수 없습니다.'),{code:'INVALID_RESPONSE'});
    }
    if(!info||!['synthetic','readonly'].includes(info.mode))
      throw Object.assign(Error('확인되지 않은 실행 모드입니다.'),{code:'INVALID_RESPONSE'});
    return info;
  }
  async readFeatures(signal) {
    try {
      const response=await this.fetchResponse('/api/features',{signal});
      const value=await response.json();
      const fields=['query','project','document_role','review_status','tag','source'];
      const states=['SUPPORTED','CONDITIONAL','UNSUPPORTED','UNKNOWN'];
      if(!value||typeof value!=='object'||Object.keys(value).length!==2||value.contract_version!=='UTT-READ-FEATURES/0.1.0'||
        !value.filters||typeof value.filters!=='object'||Array.isArray(value.filters)||Object.keys(value.filters).length!==fields.length||
        !fields.every(key=>Object.hasOwn(value.filters,key)&&states.includes(value.filters[key])))return null;
      return value;
    }catch(error){if(error.name==='AbortError')throw error;return null;}
  }
  async workspaceFeatures(signal) {
    let response;
    try { response=await this.fetcher('/api/workspace/features',{signal}); }
    catch(error) {
      if(error.name==='AbortError')throw error;
      throw Object.assign(Error('작업 DB 기능 확인에 실패했습니다.'),{code:'CONNECTION_UNAVAILABLE'});
    }
    if(response.status===404||response.status===405)return null;
    if(!response.ok)throw Object.assign(Error('작업 DB 기능 확인에 실패했습니다.'),{code:'CONNECTION_UNAVAILABLE'});
    let value;
    try { value=await response.json(); }
    catch(error) {if(error.name==='AbortError')throw error;throw Object.assign(Error('잘못된 작업 DB 기능 응답'),{code:'INVALID_RESPONSE'});}
    if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==3||
       value.contract_version!=='UTT-WORKSPACE-FEATURES/0.1'||typeof value.read_detail!=='boolean'||
       typeof value.writes_supported!=='boolean')throw Object.assign(Error('잘못된 작업 DB 기능 응답'),{code:'INVALID_RESPONSE'});
    return value;
  }
  async workspaceDetail(identity,signal) {
    const fields=['dataset_id','snapshot_id','record_id'];
    if(!identity||!fields.every(key=>typeof identity[key]==='string'&&identity[key].length>0))
      throw Object.assign(Error('작업 DB 자료 범위가 필요합니다.'),{code:'INVALID_REQUEST'});
    const request=Object.freeze({contract_version:'UTT-WORKSPACE-READ/0.1',operation:'detail',
      ...Object.fromEntries(fields.map(key=>[key,identity[key]]))});
    const response=await this.fetchResponse('/api/workspace/read',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(request),signal},[400]);
    let result;
    try { result=await response.json(); }
    catch(error) {if(error.name==='AbortError')throw error;throw Object.assign(Error('잘못된 작업 DB 응답'),{code:'INVALID_RESPONSE'});}
    if(!result||typeof result!=='object'||Array.isArray(result)||result.contract_version!=='UTT-WORKSPACE-DETAIL/0.1')
      throw Object.assign(Error('잘못된 작업 DB 상세 응답'),{code:'INVALID_RESPONSE'});
    if(result.status==='ERROR')throw Object.assign(Error('작업 DB 상세를 읽지 못했습니다.'),{code:'CONNECTION_UNAVAILABLE'});
    if(!fields.every(key=>result[key]===identity[key]))
      throw Object.assign(Error('작업 DB 범위 불일치'),{code:'RESPONSE_SCOPE_MISMATCH'});
    const lists=['projects','topics','sources','decisions','revisions','events','notes','tags','proposals','merged_children'];
    if(!response.ok||result.status!=='OK'||!result.record||typeof result.record!=='object'||Array.isArray(result.record)||
       result.record.record_id!==identity.record_id||!Number.isSafeInteger(result.record.version)||result.record.version<1||
       ![null,'string'].includes(result.record.archived_at===null?null:typeof result.record.archived_at)||
       ![null,'string'].includes(result.record.merged_into===null?null:typeof result.record.merged_into)||
       !lists.every(key=>Array.isArray(result[key])&&result[key].every(row=>row&&typeof row==='object'&&!Array.isArray(row))))
      throw Object.assign(Error('잘못된 작업 DB 상세 응답'),{code:'INVALID_RESPONSE'});
    if(!['projects','topics'].every(key=>result[key].every(row=>row.link?.record_id===identity.record_id))||
       !['sources','decisions','revisions','events','notes','proposals'].every(key=>result[key].every(row=>row.record_id===identity.record_id))||
       !result.merged_children.every(row=>row.merged_into===identity.record_id))
      throw Object.assign(Error('작업 DB 자료 범위 불일치'),{code:'RESPONSE_SCOPE_MISMATCH'});
    return result;
  }
  async workspaceLifecycle(identity,signal) {
    const ids=identity?.record_ids;
    if(typeof identity?.dataset_id!=='string'||!identity.dataset_id||typeof identity?.snapshot_id!=='string'||!identity.snapshot_id||
       !Array.isArray(ids)||ids.length>100||!ids.every(id=>typeof id==='string'&&id.length>0)||new Set(ids).size!==ids.length)
      throw Object.assign(Error('잘못된 작업 DB 상태 요청'),{code:'INVALID_REQUEST'});
    const request={contract_version:'UTT-WORKSPACE-LIFECYCLE/0.1',dataset_id:identity.dataset_id,snapshot_id:identity.snapshot_id,record_ids:ids};
    const response=await this.fetchResponse('/api/workspace/lifecycle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request),signal},[400]);
    let result;
    try { result=await response.json(); }
    catch(error) {if(error.name==='AbortError')throw error;throw Object.assign(Error('잘못된 상태 응답'),{code:'INVALID_RESPONSE'});}
    if(!response.ok||!result||result.contract_version!==request.contract_version||
       result.snapshot?.dataset_id!==identity.dataset_id||result.snapshot?.snapshot_id!==identity.snapshot_id||
       !Array.isArray(result.records)||result.records.length!==ids.length)
      throw Object.assign(Error('작업 DB 상태 범위 불일치'),{code:'RESPONSE_SCOPE_MISMATCH'});
    const expected=new Set(ids),mapped=new Map();
    for(const row of result.records){
      if(!row||typeof row!=='object'||Array.isArray(row)||!expected.has(row.record_id)||mapped.has(row.record_id)||
         ![null,'string'].includes(row.archived_at===null?null:typeof row.archived_at)||
         ![null,'string'].includes(row.merged_into===null?null:typeof row.merged_into))
        throw Object.assign(Error('잘못된 작업 DB 상태 응답'),{code:'INVALID_RESPONSE'});
      mapped.set(row.record_id,row);
    }
    return mapped;
  }
  async workspaceCatalog(scope,operation,{identifier=null,limit=50,offset=0}={},signal) {
    const members=operation==='project_members'||operation==='topic_members';
    if(!['projects','topics','project_members','topic_members'].includes(operation)||
       typeof scope?.dataset_id!=='string'||!scope.dataset_id||typeof scope?.snapshot_id!=='string'||!scope.snapshot_id||
       !Number.isSafeInteger(limit)||limit<1||limit>1000||!Number.isSafeInteger(offset)||offset<0||offset>2147483647||
       (members&&(typeof identifier!=='string'||!identifier)))
      throw Object.assign(Error('잘못된 작업 DB 목록 요청'),{code:'INVALID_REQUEST'});
    const request={contract_version:'UTT-WORKSPACE-READ/0.1',operation,dataset_id:scope.dataset_id,snapshot_id:scope.snapshot_id,limit,offset};
    if(members)request.identifier=identifier;
    const response=await this.fetchResponse('/api/workspace/catalog',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request),signal},[400]);
    let result;
    try{result=await response.json();}
    catch(error){if(error.name==='AbortError')throw error;throw Object.assign(Error('잘못된 목록 응답'),{code:'INVALID_RESPONSE'});}
    if(!response.ok||!result||result.contract_version!=='UTT-WORKSPACE-DETAIL/0.1'||result.status!=='OK'||
       result.operation!==operation||result.kind!==({projects:'projects',topics:'topics',project_members:'project',topic_members:'topic'})[operation]||result.dataset_id!==scope.dataset_id||result.snapshot_id!==scope.snapshot_id||
       !Array.isArray(result.items)||result.items.length>limit||
       !(result.next_offset===null||(Number.isSafeInteger(result.next_offset)&&result.next_offset===offset+limit&&result.items.length===limit)))
      throw Object.assign(Error('작업 DB 목록 범위 불일치'),{code:'RESPONSE_SCOPE_MISMATCH'});
    const idKey=operation==='projects'||operation==='project_members'?'project_id':'topic_id';
    for(const item of result.items){
     if(!item||typeof item!=='object'||Array.isArray(item))throw Object.assign(Error('잘못된 목록 항목'),{code:'INVALID_RESPONSE'});
     if(members){
      if(!item.link||item.link[idKey]!==identifier||!item.record||item.record.record_id!==item.link.record_id||
         typeof item.record.record_id!=='string'||!item.record.record_id||typeof item.record.title!=='string'||
         ![null,'string'].includes(item.record.archived_at===null?null:typeof item.record.archived_at)||
         ![null,'string'].includes(item.record.merged_into===null?null:typeof item.record.merged_into))
        throw Object.assign(Error('목록 구성 범위 불일치'),{code:'RESPONSE_SCOPE_MISMATCH'});
     }else if(typeof item[idKey]!=='string'||!item[idKey]||typeof item.name!=='string')
       throw Object.assign(Error('잘못된 목록 항목'),{code:'INVALID_RESPONSE'});
    }
    return result;
  }
  async read(operation, values={}, signal) {
    const request=Object.freeze(makeRequest(operation,{dataset_id:this.dataset?.dataset_id??null,snapshot_id:this.dataset?.snapshot_id??null,...values}));
    const response=await this.fetchResponse('/api/read',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({request,sort:this.sort,scenario:this.scenario}),signal});
    let envelope;
    try{envelope=await response.json();}
    catch(error){
      if(error.name==='AbortError')throw error;
      throw Object.assign(Error('읽기 응답이 올바른 JSON이 아닙니다.'),{code:'INVALID_RESPONSE'});
    }
    validateResponse(envelope,request,VERSION);
    if(envelope.status!=='OK') throw Object.assign(Error(envelope.error?.message??'읽기 실패'),{code:envelope.error?.code});
    return envelope;
  }
  async readSource(identity,signal) {
    const fields=['dataset_id','snapshot_id','record_id','source_id','source_version'];
    if(!identity||!fields.every(key=>typeof identity[key]==='string'&&identity[key].length>0))
      throw Object.assign(Error('확인된 출처 식별자와 버전이 필요합니다.'),{code:'INVALID_REQUEST'});
    const request=Object.freeze(Object.fromEntries([['contract_version','UTT-SOURCE-READ/0.1.0'],...fields.map(key=>[key,identity[key]])]));
    const transport=await this.fetchResponse('/api/source',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request),signal},[400]);
    let response;
    try{response=await transport.json();}
    catch(error){
      if(error.name==='AbortError')throw error;
      throw Object.assign(Error('출처 읽기 응답이 올바른 JSON이 아닙니다.'),{code:'INVALID_RESPONSE'});
    }
    const highlight=await validateSourceResponse(response,request);
    if(!transport.ok&&response.status!=='ERROR')throw Object.assign(Error('잘못된 출처 오류 응답입니다.'),{code:'INVALID_RESPONSE'});
    if(response.status==='ERROR')throw Object.assign(Error('출처 읽기 요청을 완료하지 못했습니다.'),{code:response.error.code});
    // Client-only view state; the wire envelope is kept unchanged.
    return {response,highlight};
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
