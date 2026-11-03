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
