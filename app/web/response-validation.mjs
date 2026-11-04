// Client-side envelope/scope checks for the existing read draft, not a full
// JSON Schema implementation. No fields are added to wire messages.
const OPS=['capabilities','projects','search','detail','related_history'];
const ERROR_CODES=['ITEM_NOT_FOUND','SOURCE_MISSING','CONNECTION_UNAVAILABLE','VERSION_MISMATCH','UNSUPPORTED_CONTRACT','INVALID_REQUEST','INTERNAL_ERROR'];
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const text=value=>typeof value==='string'&&value.length>0;
const exact=(value,keys)=>object(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));
function fail(code,message){throw Object.assign(Error(message),{code});}
function requireShape(condition){if(!condition)fail('INVALID_RESPONSE','읽기 응답 형식이 계약과 일치하지 않습니다.');}
function descriptor(value){
  requireShape(exact(value,['dataset_id','dataset_kind','snapshot_id','source_version','legacy_contracts']));
  requireShape(text(value.dataset_id)&&text(value.dataset_kind)&&text(value.snapshot_id)&&
    (value.source_version===null||text(value.source_version))&&Array.isArray(value.legacy_contracts)&&value.legacy_contracts.every(text));
}
function sameScope(value,scope){
  requireShape(object(value)&&text(value.dataset_id));
  if(value.dataset_id!==scope.dataset_id)fail('RESPONSE_SCOPE_MISMATCH','요청과 다른 데이터셋의 응답을 거부했습니다.');
}
function pin(descriptorValue,request){
  descriptor(descriptorValue);
  if(request.dataset_id!==null&&descriptorValue.dataset_id!==request.dataset_id)
    fail('RESPONSE_SCOPE_MISMATCH','요청과 다른 데이터셋의 응답을 거부했습니다.');
  if(request.snapshot_id!==null&&descriptorValue.snapshot_id!==request.snapshot_id)
    fail('VERSION_MISMATCH','요청과 다른 기준 버전의 응답을 거부했습니다.');
}
function array(value,visit){requireShape(Array.isArray(value));value.forEach(visit);}
function page(value){
  requireShape(exact(value,['page_size','next_cursor','sort'])&&Number.isInteger(value.page_size)&&value.page_size>=1&&value.page_size<=100&&
    (value.next_cursor===null||text(value.next_cursor))&&Array.isArray(value.sort)&&value.sort.length>=2&&value.sort.every(text));
}
// The draft explicitly represents both endpoints. Cross-dataset relations are
// allowed; do not invent a relation.dataset_id or require both endpoints local.
function relation(value,scope,anchor=null){
  requireShape(object(value)&&text(value.from_dataset_id)&&text(value.to_dataset_id)&&text(value.from_id)&&text(value.to_id));
  const from=value.from_dataset_id===scope.dataset_id, to=value.to_dataset_id===scope.dataset_id;
  if(!(anchor ? (from&&value.from_id===anchor)||(to&&value.to_id===anchor) : from||to))
    fail('RESPONSE_SCOPE_MISMATCH','자료와 관계의 데이터셋 범위가 일치하지 않습니다.');
}
function source(value,scope){sameScope(value,scope);}
function record(value,scope){
  sameScope(value,scope);requireShape(text(value.record_id));
  array(value.source_refs,item=>source(item,scope));
  array(value.project_relations,item=>relation(item,scope,value.record_id));
}
export function validateResponse(envelope,request,version){
  requireShape(exact(envelope,['contract_version','operation','status','snapshot','result','error']));
  requireShape(envelope.contract_version===version&&envelope.operation===request.operation&&OPS.includes(envelope.operation));
  requireShape(envelope.status==='OK'||envelope.status==='ERROR');
  // Nullable initial scope belongs to capabilities only. All other operations
  // must remain pinned, even when no client dataset happens to be selected now.
  if(envelope.snapshot!==null)pin(envelope.snapshot,request);
  if(envelope.status==='ERROR'){
    requireShape(envelope.result===null&&exact(envelope.error,['code','message','retryable'])&&
      ERROR_CODES.includes(envelope.error.code)&&text(envelope.error.message)&&typeof envelope.error.retryable==='boolean');
    return;
  }
  requireShape(request.operation==='capabilities'||(text(request.dataset_id)&&text(request.snapshot_id)));
  requireShape(envelope.snapshot!==null&&envelope.error===null&&object(envelope.result));
  const scope=envelope.snapshot,r=envelope.result;
  const project=item=>sameScope(item,scope);
  const link=item=>relation(item,scope);
  switch(request.operation){
    case 'capabilities':
      requireShape(exact(r,['dataset','operations','sort_order','max_page_size','writes_supported']));
      pin(r.dataset,scope);
      requireShape(Array.isArray(r.operations)&&r.operations.every(text)&&Array.isArray(r.sort_order)&&r.sort_order.length>=2&&r.sort_order.every(text)&&
        Number.isInteger(r.max_page_size)&&r.max_page_size>=1&&r.max_page_size<=100&&r.writes_supported===false);
      break;
    case 'projects':
      requireShape(exact(r,['projects','components','relations','page']));
      array(r.projects,project);array(r.components,project);array(r.relations,link);page(r.page);break;
    case 'search':
      requireShape(exact(r,['records','page']));array(r.records,item=>record(item,scope));page(r.page);break;
    case 'detail':
      requireShape(exact(r,['record','related_projects','sources']));record(r.record,scope);
      if(r.record.record_id!==request.record_id)fail('RESPONSE_SCOPE_MISMATCH','요청과 다른 자료의 상세 응답을 거부했습니다.');
      array(r.related_projects,project);array(r.sources,item=>source(item,scope));break;
    case 'related_history':
      requireShape(exact(r,['related_records','relations','events','history_status','page']));
      array(r.related_records,item=>record(item,scope));array(r.relations,link);array(r.events,item=>sameScope(item,scope));
      requireShape(['AVAILABLE','MISSING','UNKNOWN','INACCESSIBLE'].includes(r.history_status));page(r.page);break;
  }
}

// UTT-SOURCE-READ is independent of the record-body read contract. In
// particular, metadata file hashes/versions do not identify the displayed text.
const SOURCE_VERSION='UTT-SOURCE-READ/0.1.0';
const SOURCE_IDENTITY=['dataset_id','snapshot_id','record_id','source_id','source_version'];
const hashValue=value=>typeof value==='string'&&/^[0-9a-f]{64}$/.test(value);
const nullableText=value=>value===null||text(value);
function sourceShape(value){
 requireShape(exact(value,['source_id','dataset_id','availability','version','content_sha256','location','verification']));
 requireShape(text(value.source_id)&&text(value.dataset_id)&&['AVAILABLE','MISSING','UNKNOWN','INACCESSIBLE'].includes(value.availability)&&
   nullableText(value.version)&&(value.content_sha256===null||hashValue(value.content_sha256))&&['VERIFIED','UNVERIFIED','UNKNOWN'].includes(value.verification));
 if(value.location!==null){
  const l=value.location;
  requireShape(exact(l,['target','unit','start','end','boundary','target_sha256','fragment_sha256','verification']));
  requireShape(text(l.target)&&['unicode_code_point','UTF-16','byte'].includes(l.unit)&&Number.isSafeInteger(l.start)&&l.start>=0&&
    Number.isSafeInteger(l.end)&&l.end>=0&&l.boundary==='half_open'&&(l.target_sha256===null||hashValue(l.target_sha256))&&
    (l.fragment_sha256===null||hashValue(l.fragment_sha256))&&['VERIFIED','UNVERIFIED','UNKNOWN'].includes(l.verification));
 }
}
async function textHash(value,cryptoApi){
 // TextEncoder replaces lone surrogates. Reject them instead of verifying a
 // different serialization from the exact Unicode content provided.
 for(let i=0;i<value.length;i++){
  const code=value.charCodeAt(i);
  if(code>=0xD800&&code<=0xDBFF){
   const next=value.charCodeAt(++i);requireShape(next>=0xDC00&&next<=0xDFFF);
  }else requireShape(!(code>=0xDC00&&code<=0xDFFF));
 }
 requireShape(Boolean(cryptoApi?.subtle));
 return Array.from(new Uint8Array(await cryptoApi.subtle.digest('SHA-256',new TextEncoder().encode(value))),x=>x.toString(16).padStart(2,'0')).join('');
}
async function sourceLocator(source,content,cryptoApi){
 const state=(status,reason)=>({status,reason,highlight:null});
 if(content.availability!=='AVAILABLE')return state('NOT_AVAILABLE','CONTENT_UNAVAILABLE');
 const l=source.location;
 if(l===null)return state('UNVERIFIED','LOCATION_NOT_PROVIDED');
 const targets={SOURCE_BODY:'SOURCE_BODY',RECORD_BODY:'RECORD_BODY',SYNTHETIC_SOURCE:'SOURCE_BODY'};
 if(!Object.hasOwn(targets,l.target)||targets[l.target]!==content.target_kind)return state('NOT_SUPPORTED','TARGET_NOT_SUPPORTED');
 if(l.unit!=='UTF-16')return state('NOT_SUPPORTED','UNIT_NOT_SUPPORTED');
 if(content.locator_target_version===null)return state('UNVERIFIED','TARGET_VERSION_UNCONFIRMED');
 if(content.locator_target_version!==content.target_version)return state('MISMATCH','TARGET_VERSION_MISMATCH');
 if(l.target_sha256===null||l.fragment_sha256===null)return state('UNVERIFIED','HASH_NOT_PROVIDED');
 if(l.target_sha256!==content.text_sha256)return state('MISMATCH','TARGET_HASH_MISMATCH');
 const split=i=>i>0&&i<content.text.length&&/[\uD800-\uDBFF]/.test(content.text[i-1])&&/[\uDC00-\uDFFF]/.test(content.text[i]);
 if(l.start>l.end||l.end>content.text.length||split(l.start)||split(l.end))return state('MISMATCH','INVALID_RANGE');
 if(await textHash(content.text.slice(l.start,l.end),cryptoApi)!==l.fragment_sha256)return state('MISMATCH','FRAGMENT_HASH_MISMATCH');
 return {status:'VERIFIED',reason:'HASH_AND_RANGE_MATCH',highlight:{start:l.start,end:l.end}};
}
export async function validateSourceResponse(envelope,request,cryptoApi=globalThis.crypto){
 requireShape(exact(request,['contract_version',...SOURCE_IDENTITY])&&request.contract_version===SOURCE_VERSION&&SOURCE_IDENTITY.every(k=>text(request[k])));
 requireShape(exact(envelope,['contract_version','status','result','error'])&&envelope.contract_version===SOURCE_VERSION&&['OK','ERROR'].includes(envelope.status));
 if(envelope.status==='ERROR'){
  requireShape(envelope.result===null&&exact(envelope.error,['code','message','retryable'])&&
   [...ERROR_CODES,'NOT_SUPPORTED'].includes(envelope.error.code)&&text(envelope.error.message)&&typeof envelope.error.retryable==='boolean');
  return null;
 }
 const r=envelope.result;
 requireShape(envelope.error===null&&exact(r,[...SOURCE_IDENTITY,'source','content','locator_verification']));
 for(const key of SOURCE_IDENTITY){
  requireShape(text(r[key]));
  if(r[key]!==request[key])fail(['snapshot_id','source_version'].includes(key)?'VERSION_MISMATCH':'RESPONSE_SCOPE_MISMATCH','출처 응답의 요청 범위가 일치하지 않습니다.');
 }
 sourceShape(r.source);
 if(r.source.source_id!==request.source_id||r.source.dataset_id!==request.dataset_id)fail('RESPONSE_SCOPE_MISMATCH','출처 메타데이터의 범위가 일치하지 않습니다.');
 if(r.source.version!==request.source_version)fail('VERSION_MISMATCH','출처 메타데이터 버전이 일치하지 않습니다.');
 const c=r.content;
 requireShape(exact(c,['availability','target_kind','target_id','target_version','text','text_sha256','locator_target_version']));
 requireShape(['AVAILABLE','MISSING','UNKNOWN','INACCESSIBLE','NOT_SUPPORTED'].includes(c.availability)&&
  [null,'SOURCE_BODY','RECORD_BODY'].includes(c.target_kind)&&nullableText(c.target_id)&&nullableText(c.target_version)&&nullableText(c.locator_target_version));
 if(c.target_kind===null)requireShape(c.target_id===null&&c.target_version===null);
 else if(c.target_id!==request[c.target_kind==='SOURCE_BODY'?'source_id':'record_id'])fail('RESPONSE_SCOPE_MISMATCH','출처 본문 대상이 요청과 일치하지 않습니다.');
 if(c.availability==='AVAILABLE'){
  requireShape(c.target_kind!==null&&text(c.target_version)&&typeof c.text==='string'&&hashValue(c.text_sha256));
  if(await textHash(c.text,cryptoApi)!==c.text_sha256)fail('INVALID_RESPONSE','출처 본문의 UTF-8 텍스트 해시가 일치하지 않습니다.');
 }else requireShape(c.text===null&&c.text_sha256===null);
 const verification=r.locator_verification;
 requireShape(exact(verification,['status','reason']));
 const checked=await sourceLocator(r.source,c,cryptoApi);
 if(verification.status!==checked.status||verification.reason!==checked.reason)fail('INVALID_RESPONSE','출처 위치 검증 주장과 본문 검증 결과가 일치하지 않습니다.');
 return checked.highlight;
}
