import {ReadClient,LatestRequest} from './client.mjs';
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,action,cls)=>{const n=el('button',text,cls);n.type='button';n.onclick=action;return n;};
const labels={UNREVIEWED:'미검토',HELD:'보류',EVIDENCE_REQUESTED:'근거 요청',CANCELLED:'취소',OBSERVED:'관측됨',CONFLICTING:'관측 충돌',NONE:'없음',DRAFT:'초안',CONFLICT:'충돌',NOT_APPROVED:'미승인',UNKNOWN:'미확인',AVAILABLE:'이용 가능',MISSING:'누락 확인',NOT_REQUESTED:'미요청',INACCESSIBLE:'접근 불가',USER_CONFIRMED:'사용자 확정',UNRESOLVED:'미해결'};
const label=v=>v==null?'미확인':labels[v]??v;
const client=new ReadClient(), listGate=new LatestRequest(), detailGate=new LatestRequest(), bootGate=new LatestRequest();
let page=0,cursors=[null],nextCursor=null,projects=[],mode=null,capabilities=null,features=null,providerSort='id_asc',listOriginId=null,listScrollY=0,currentDetailId=null,timer;
const NS='utt.synthetic.session.v1';
const sourceGates=new Set();
function cancelSources(){for(const gate of sourceGates)gate.cancel();sourceGates.clear();}
const supports=operation=>capabilities?.operations.includes(operation)===true;
const filterState=name=>features?.filters[name]??'UNKNOWN';
const filterSupported=name=>filterState(name)==='SUPPORTED';
const filterLabel=name=>({SUPPORTED:'지원',CONDITIONAL:name==='query'?'조건부 · 제공자 확인 필요':'조건부 · 현재 비활성',UNSUPPORTED:'미지원',UNKNOWN:'지원 미확인'})[filterState(name)];
const synthetic=()=>mode==='synthetic'&&client.dataset?.dataset_kind==='SYNTHETIC';
const pageSize=()=>Math.min(3,capabilities?.max_page_size??3);
function notify(text,kind=''){ $('notice').textContent=text;$('notice').className=kind; }
function errorText(error){
 const code=error.code??'CONNECTION_UNAVAILABLE';
 const messages={AUTHENTICATION_REQUIRED:'인증이 필요합니다. 승인된 실행 환경의 인증 상태를 확인하세요.',ACCESS_DENIED:'접근 권한이 거부되었습니다. 승인된 실행 환경의 접근 설정을 확인하세요.',
  CONNECTION_UNAVAILABLE:'읽기 공급자에 연결할 수 없습니다. 합성 자료로 대체하지 않습니다.',
  ITEM_NOT_FOUND:'이 기준 버전에서 요청한 자료를 찾을 수 없습니다.',SOURCE_MISSING:'요청한 출처의 누락이 확인됐습니다.',
  VERSION_MISMATCH:'오래된 버전입니다. 기준 다시 조회 후 첫 페이지부터 검색하세요.',
  RESPONSE_SCOPE_MISMATCH:'요청과 다른 데이터셋 또는 자료의 응답을 거부했습니다.',INVALID_RESPONSE:'응답 형식 또는 범위를 확인할 수 없습니다.',
  UNSUPPORTED_CONTRACT:'지원하지 않는 읽기 계약입니다.',INVALID_REQUEST:'읽기 요청 조건을 처리할 수 없습니다.',
  NOT_SUPPORTED:'이 출처 본문 읽기는 지원되지 않습니다.',INTERNAL_ERROR:'읽기 공급자가 요청을 완료하지 못했습니다.',HTTP_ERROR:'읽기 요청이 거절되었거나 처리되지 않았습니다.'};
 return `${code} · ${messages[code]??'읽기 요청을 완료하지 못했습니다.'}`;
}
function failure(error){notify(errorText(error),'error');}
function summaryStates(record){
 const box=el('dl',undefined,'states');
 for(const [key,name] of [['observation','관측 상태'],['review','검토'],['proposal','제안'],['adoption','정본 승인']])box.append(el('dt',name),el('dd',label(record.state[key])));
 return box;
}
function projectName(id){return projects.find(p=>p.project_id===id)?.name??id;}
function rememberProjects(items){
 for(const project of items){
  if(!projects.some(p=>p.project_id===project.project_id))projects.push(project);
  if(![...$('project').options].some(o=>o.value===project.project_id))$('project').append(new Option(project.name,project.project_id));
 }
 updateControls();
}
function endpoint(relation,anchor){
 const scope=client.dataset.dataset_id;
 if(relation.from_dataset_id===scope&&relation.from_id===anchor)return {id:relation.to_id,dataset:relation.to_dataset_id};
 if(relation.to_dataset_id===scope&&relation.to_id===anchor)return {id:relation.from_id,dataset:relation.from_dataset_id};
 return null;
}
function projectRelation(relation,anchor){
 const target=endpoint(relation,anchor);
 if(!target)return {text:'연결 대상 프로젝트 미확인'};
 if(target.dataset!==client.dataset.dataset_id)return {text:`다른 데이터셋 관계 · 현재 연결에서 이동 미지원 (${target.id})`};
 const known=projects.find(p=>p.project_id===target.id);
 return known?{id:known.project_id,text:known.name}:{text:`연결 대상 프로젝트 미확인 (${target.id})`};
}
function updateControls(){
 const canSearch=supports('search');
 for(const n of $('filters').elements)n.disabled=!canSearch;
 $('search').disabled=!canSearch||filterState('query')==='UNSUPPORTED';
 $('project').disabled=!canSearch||projects.length===0||!filterSupported('project');
 $('role').disabled=!canSearch||!filterSupported('document_role');
 $('review').disabled=!canSearch||!filterSupported('review_status');
 $('filter-support').textContent=`검색어: ${filterLabel('query')} · 프로젝트: ${filterLabel('project')} · 자료 유형: ${filterLabel('document_role')} · 검토 상태: ${filterLabel('review_status')}`;
 $('sort').disabled=!canSearch||!synthetic();
 for(const n of $('projects').querySelectorAll('button'))n.disabled=!canSearch||!filterSupported('project');
 for(const n of $('components').querySelectorAll('button'))n.disabled=!canSearch||!filterSupported('project');
}
function hideDetail(){cancelSources();detailGate.cancel();currentDetailId=null;$('detail').hidden=true;$('detail').replaceChildren();$('list-view').hidden=false;}
function invalidate(){clearTimeout(timer);listGate.cancel();hideDetail();nextCursor=null;$('results').replaceChildren();$('next').disabled=true;$('prev').disabled=true;}
async function bootstrap(){
 invalidate();const token=bootGate.begin();client.dataset=null;capabilities=null;features=null;mode=null;projects=[];client.scenario='normal';
 $('scenario').value='normal';$('synthetic-scenarios').hidden=true;$('dataset-kind').textContent='미연결';
 $('project').replaceChildren(new Option('전체 프로젝트 · 미분류 포함',''));
 $('projects').replaceChildren();$('components').replaceChildren();$('dataset').textContent='연결 확인 중…';
 $('mode').textContent='연결 확인 중 · 운영 승인 없음';$('connection-state').textContent='공급자: 확인 중 · 실제 정본: 미확인';
 updateControls();notify('읽기 공급자를 확인하는 중…');
 try{
  const info=await client.readMode(token.signal);if(!token.current())return;mode=info.mode;
  $('mode').textContent=mode==='synthetic'?'합성 개발 자료 · 운영 연결 없음':'읽기 어댑터 미연결 · 운영 연결 없음';
  const cap=await client.read('capabilities',{},token.signal);if(!token.current())return;
  if(mode==='synthetic'&&cap.snapshot.dataset_kind!=='SYNTHETIC')throw Object.assign(Error(),{code:'INVALID_RESPONSE'});
  client.dataset=cap.snapshot;capabilities=cap.result;
  const featureResponse=await client.readFeatures(token.signal);if(!token.current())return;
  features=featureResponse;
  if(filterState('query')==='UNSUPPORTED')$('search').value='';
  if(!filterSupported('project'))$('project').value='';
  $('mode').textContent=synthetic()?'합성 개발 자료 · 운영 연결 없음':'읽기 공급자 응답 확인 · 운영 승인 없음';
  $('dataset-kind').textContent=synthetic()?'SYNTHETIC ONLY':'읽기 전용';
  $('connection-state').textContent='공급자: 읽기 응답 확인 · 실제 정본: 미확인';
  $('synthetic-scenarios').hidden=!synthetic();
  $('dataset').textContent=`${cap.snapshot.dataset_id} / ${cap.snapshot.dataset_kind} / 기준 ${cap.snapshot.snapshot_id}`;
  const savedRole=filterSupported('document_role')?$('role').value:'';
  $('role').replaceChildren(new Option('모든 유형',''));
  if(synthetic())$('role').append(new Option('합성 노트','SYNTHETIC_NOTE'));
  if(savedRole){if(![...$('role').options].some(o=>o.value===savedRole))$('role').append(new Option(savedRole,savedRole));$('role').value=savedRole;}
  if(!filterSupported('document_role'))$('role').value='';
  if(!filterSupported('review_status'))$('review').value='';
  if(!synthetic()){
   providerSort=cap.result.sort_order.includes('record_id:desc')?'id_desc':'id_asc';
   $('sort').value=providerSort;
  }
  let navigationFailed=false;
  if(supports('projects')){
   try{
    let cursor=null;const components=[],relations=[];
    do{
     const d=await client.read('projects',{cursor,page_size:capabilities.max_page_size},token.signal);if(!token.current())return;
     projects.push(...d.result.projects);components.push(...d.result.components);relations.push(...d.result.relations);cursor=d.result.page.next_cursor;
    }while(cursor);
    $('projects').append(button('전체 자료 · 미분류 포함',()=>selectProject(''),'project-link'));
    for(const p of projects){
     $('project').append(new Option(p.name,p.project_id));
     const b=button(p.name,()=>selectProject(p.project_id),'project-link');
     b.append(el('small',`관측 분류: ${label(p.classification_observed)} / 사용자 확정 소속: ${label(p.membership_user_confirmed)}`));$('projects').append(b);
    }
    for(const c of components){
     const links=relations.filter(r=>endpoint(r,c.component_id)!==null);
     if(!links.length)$('components').append(el('p',`${c.name} · 연결 대상 프로젝트 미확인`));
     for(const link of links){
      const target=projectRelation(link,c.component_id);
      if(target.id){const b=button(c.name,()=>selectProject(target.id),'project-link');b.append(el('small',`${label(c.relationship_status)} · ${target.text}`));$('components').append(b);}
      else $('components').append(el('p',`${c.name} · ${target.text}`,'muted'));
     }
    }
   }catch(error){
    if(!token.current()||error.name==='AbortError')return;
    projects=[];navigationFailed=true;
    $('projects').replaceChildren(el('p',`프로젝트 조회 실패: ${errorText(error)}`,'error'));
   }
  }else{
   $('projects').append(el('p','공급자가 프로젝트 탐색을 지원하지 않습니다.'));
   $('components').append(el('p','구성요소 탐색 미지원'));
  }
  updateControls();
  if(supports('search'))await search(true);
  else notify('공급자가 검색을 지원하지 않습니다. 지원 범위를 확인하세요.','warning');
  if(token.current()&&navigationFailed&&$('notice').className!=='error')notify('자료 검색과 별도로 프로젝트 탐색을 완료하지 못했습니다. 기준 다시 조회로 재시도하세요.','warning');
 }catch(error){if(token.current()&&error.name!=='AbortError'){$('connection-state').textContent='공급자: 읽기 연결 미확인 · 실제 정본: 미확인';failure(error);}}
}
function selectProject(id){
 if(!filterSupported('project')){notify(`프로젝트 필터: ${filterLabel('project')} · 검색 조건을 바꾸지 않았습니다.`,'warning');return;}
 if(id!==''&&!projects.some(p=>p.project_id===id)){notify('연결 대상 프로젝트 미확인 · 검색 조건을 바꾸지 않았습니다.','warning');return;}
 $('project').value=id;invalidate();search(true);
}
async function search(reset=false){
 if(!client.dataset){notify('공급자 미연결: 기준 다시 조회를 눌러 주세요.','error');return;}
 if(!supports('search')){notify('공급자가 검색을 지원하지 않습니다.','warning');return;}
 hideDetail();if(reset){page=0;cursors=[null];}const token=listGate.begin();
 $('results').replaceChildren();$('results').setAttribute('aria-busy','true');$('count').textContent='';notify('자료를 불러오는 중…');$('next').disabled=true;$('prev').disabled=true;
 if(!synthetic())$('sort').value=providerSort;
 client.sort=$('sort').value;
 try{
  const d=await client.read('search',{query:filterState('query')==='UNSUPPORTED'?null:$('search').value,project_id:filterSupported('project')?$('project').value||null:null,document_role:filterSupported('document_role')?$('role').value||null:null,review_status:filterSupported('review_status')?$('review').value||null:null,cursor:cursors[page],page_size:pageSize()},token.signal);
  if(!token.current())return;
  nextCursor=d.result.page.next_cursor;$('page-number').textContent=`${page+1} 페이지`;$('count').textContent=`이 페이지 ${d.result.records.length}건 · 한 번에 ${d.result.page.page_size}건`;
  for(const r of d.result.records){
   if(![...$('role').options].some(o=>o.value===r.document_role))$('role').append(new Option(r.document_role,r.document_role));
   const card=el('article',undefined,'record');const b=button(r.title,()=>{listOriginId=r.record_id;listScrollY=window.scrollY;showDetail(r.record_id);},'record-title');b.dataset.recordId=r.record_id;b.disabled=!supports('detail');
   card.append(el('span',r.document_role,'eyebrow'),b,el('p',r.record_id,'muted'),summaryStates(r));
   if(!supports('detail'))card.append(el('p','공급자가 상세 조회를 지원하지 않습니다.','muted'));
   const links=el('p',r.project_relations.length?'프로젝트: ':r.project_relations_status==='AVAILABLE'?'미분류 자료 · 이 기준에서 기록된 소속 없음':`프로젝트 관계: ${label(r.project_relations_status)}`);
   r.project_relations.forEach((l,i)=>{if(i)links.append(document.createTextNode(' / '));links.append(document.createTextNode(`${projectRelation(l,r.record_id).text} (${label(l.assertion_status)})`));});
   card.append(links,el('p',`출처: ${label(r.source_status)} · 본문: 상세에서 요청`,'muted'));$('results').append(card);
  }
  if(!d.result.records.length)$('results').append(el('p','조건에 맞는 기록이 없습니다. 연결 오류와는 다릅니다.','empty'));
  $('prev').disabled=page===0;$('next').disabled=!nextCursor;
  const queryNotice=$('search').value&&!filterSupported('query')?` · 검색어 ${filterLabel('query')} (제공자 응답 기준)`:'';
  notify(`${synthetic()?'합성 자료':'읽기 자료'} 조회 완료 · 실제 정본 상태는 미확인${queryNotice}`);
 }catch(error){if(token.current()&&error.name!=='AbortError')failure(error);}
 finally{if(token.current())$('results').setAttribute('aria-busy','false');}
}
function back(){
 hideDetail();notify('검색 조건과 페이지를 유지했습니다.');
 const match=[...document.querySelectorAll('[data-record-id]')].find(n=>n.dataset.recordId===listOriginId);
 match?.focus({preventScroll:true});window.scrollTo({top:listScrollY});
}
async function showDetail(id){
 if(!supports('detail')){notify('공급자가 상세 조회를 지원하지 않습니다.','warning');return;}
 cancelSources();currentDetailId=id;const token=detailGate.begin();listGate.cancel();$('list-view').hidden=true;$('results').setAttribute('aria-busy','false');
 const box=$('detail');box.hidden=false;box.replaceChildren(button('← 검색 결과로',back),el('p','상세 자료를 불러오는 중…'));box.focus();notify('상세 조회 중…');
 try{
  const d=await client.read('detail',{record_id:id,page_size:pageSize()},token.signal);if(!token.current()||currentDetailId!==id)return;const r=d.result.record;
  // Detail can supply scoped project metadata even without a project catalogue.
  rememberProjects(d.result.related_projects);
  box.replaceChildren(button('← 검색 결과로',back),el('p',r.record_id,'eyebrow'),el('h2',r.title),summaryStates(r));
  box.append(el('h3','프로젝트와 분류'));
  if(!r.project_relations.length)box.append(el('p',r.project_relations_status==='AVAILABLE'?`미분류 · 관계 상태: ${label(r.project_relations_status)}`:`프로젝트 관계: ${label(r.project_relations_status)}`));
  for(const l of r.project_relations){
   const target=projectRelation(l,r.record_id);
   if(target.id&&supports('search')&&filterSupported('project'))box.append(button(`${target.text} · ${label(l.assertion_status)}`,()=>selectProject(target.id),'text-link'));
   else box.append(el('p',`${target.text} · ${label(l.assertion_status)}${target.id?` · 프로젝트 필터 ${filterLabel('project')}`:''}`,'muted'));
  }
  for(const project of d.result.related_projects){
   if(r.project_relations.some(l=>endpoint(l,r.record_id)?.dataset===client.dataset.dataset_id&&endpoint(l,r.record_id)?.id===project.project_id))continue;
   if(supports('search')&&filterSupported('project'))box.append(button(`상세 응답 관련 프로젝트: ${project.name}`,()=>selectProject(project.project_id),'text-link'));
   else box.append(el('p',`상세 응답 관련 프로젝트: ${project.name} · 프로젝트 필터 ${filterLabel('project')}`));
  }
  box.append(el('p','관측 관계·사용자 확정 소속·제안·정본 승인은 독립 상태입니다.'));
  box.append(el('h3','보존 본문'),el('p',`본문 상태: ${label(r.content_availability)}`,'muted'));
  const body=el('pre',r.raw_text===null?`원문 ${label(r.content_availability)} · 요약으로 대체하지 않습니다.`:r.raw_text===''?'빈 원문 (이용 가능)':r.raw_text,'body-text');body.tabIndex=0;box.append(body);
  box.append(el('h3','출처와 원문 위치'),el('p',`출처 상태: ${label(r.source_status)}`));
  if(!d.result.sources.length)box.append(el('p','이 조회 범위의 출처 항목 없음. 전체 출처의 부재를 뜻하지 않습니다.'));
  for(const source of d.result.sources)box.append(sourceCard(source,r,d.snapshot,token));
  const history=el('section'),historyPages=el('div'),historyControls=el('div');
  history.append(el('h3',synthetic()?'기존 합성 이력과 관련 자료':'이력과 관련 자료'),historyPages,historyControls);box.append(history);
  const completeNotice=()=>notify(`${synthetic()?'합성 상세':'상세'} 조회 완료 · 실제 정본 상태는 미확인`);
  async function loadHistory(cursor=null){
   const returnFocus=historyControls.contains(document.activeElement);
   historyControls.replaceChildren(el('p','이력을 불러오는 중…'));
   try{
    const h=await client.read('related_history',{record_id:id,cursor,page_size:pageSize()},token.signal);if(!token.current())return false;
    const part=el('div');part.append(el('p',`이력 상태: ${label(h.result.history_status)}`));
    for(const event of h.result.events)part.append(el('p',`${event.event_id} · ${label(event.event_kind)} · ${event.event_contract} · 정정 대상 ${event.supersedes??'없음'} · 원형 ${event.event_payload_preservation}`));
    for(const peer of h.result.related_records){
     if(supports('detail'))part.append(button(`관련 자료: ${peer.title}`,()=>showDetail(peer.record_id),'text-link'));
     else part.append(el('p',`관련 자료: ${peer.title} · 상세 조회 미지원`));
    }
    for(const relation of h.result.relations)part.append(el('p',`${relation.relation_kind} · ${relation.from_id} → ${relation.to_id} · ${label(relation.assertion_status)}`));
    const empty=!h.result.events.length&&!h.result.related_records.length&&!h.result.relations.length;
    if(empty)part.append(el('p',h.result.history_status==='AVAILABLE'?'이 기준·조회 범위에 기록된 사건 없음.':`이력 ${label(h.result.history_status)} · 기록 없음으로 판정하지 않습니다.`));
    historyPages.append(part);historyControls.replaceChildren();
    if(h.result.page.next_cursor)historyControls.append(button('다음 이력 페이지',()=>loadHistory(h.result.page.next_cursor)));
    if(returnFocus){part.tabIndex=-1;part.focus();}
    completeNotice();return true;
   }catch(error){
    if(token.current()&&error.name!=='AbortError'){
     const retry=button('이력 다시 시도',()=>loadHistory(cursor));
     historyControls.replaceChildren(el('p',`이력 조회 실패: ${errorText(error)}`,'error'),retry);
     if(returnFocus)retry.focus();
     failure(error);
    }
    return false;
   }
  }
  let historyOk=true;
  if(supports('related_history'))historyOk=await loadHistory();
  else historyPages.append(el('p','공급자가 이력 조회를 지원하지 않습니다.'));
  if(!token.current())return;
  if(synthetic())box.append(simulationUI(id));
  box.append(el('p','정본 승인·실제 저장·동기화는 명령 계약 미정으로 지원하지 않습니다.','warning'));
  if(historyOk)completeNotice();
 }catch(error){if(token.current()&&error.name!=='AbortError'){box.replaceChildren(button('← 검색 결과로',back),el('p','상세를 읽지 못했습니다.'));failure(error);}}
}
// Each card owns its request generation; the detail generation also guards
// responses arriving after record navigation, filter changes or list return.
function sourceCard(source,record,snapshot,detailToken){
 const section=el('section',undefined,'source');
 section.dataset.sourceId=source.source_id;
 section.append(el('h4',source.source_id),el('p','상세에서 받은 출처 메타데이터 확인'),
  el('p',`메타데이터 상태: ${label(source.availability)} · 출처 버전: ${source.version??'미확인'}`),
  el('p',`원본 파일 바이트 SHA-256: ${source.content_sha256??'미확인'}`,'muted'));
 if(source.location)section.append(el('p',`메타데이터 위치: ${source.location.target} · ${source.location.unit} · [${source.location.start}, ${source.location.end})`));
 const status=el('p',source.version?'출처 본문 미요청':'출처 버전 미확인 · 메타데이터만 표시합니다.','source-status');
 status.setAttribute('role','status');status.setAttribute('aria-live','polite');
 const resultBox=el('div',undefined,'source-result'),gate=new LatestRequest();sourceGates.add(gate);
 const identity=Object.freeze({dataset_id:snapshot.dataset_id,snapshot_id:snapshot.snapshot_id,record_id:record.record_id,source_id:source.source_id,source_version:source.version});
 const load=button('출처 본문 조회',async()=>{
  const token=gate.begin();resultBox.replaceChildren();resultBox.setAttribute('aria-busy','true');status.className='source-status';status.textContent='출처 본문을 조회·검증하는 중…';
  try{
   const {response,highlight}=await client.readSource(identity,token.signal);
   if(!token.current()||!detailToken.current()||currentDetailId!==record.record_id)return;
   const data=response.result,content=data.content,verification=data.locator_verification;
   const states={AVAILABLE:'이용 가능',MISSING:'누락 확인',UNKNOWN:'미확인',INACCESSIBLE:'접근 불가',NOT_SUPPORTED:'미지원'};
   status.textContent=`출처 조회 상태: ${states[content.availability]}`;
   resultBox.append(el('p',`응답 출처 메타데이터 버전: ${data.source.version}`),
    el('p',`응답 원본 파일 바이트 SHA-256: ${data.source.content_sha256??'미확인'}`,'muted'),
    el('p',`본문 대상: ${content.target_kind??'미확인'}${content.target_kind==='RECORD_BODY'?' · 기록 본문 대상 (출처 본문과 구분)':content.target_kind==='SOURCE_BODY'?' · 출처 본문':''}`),
    el('p',`대상 ID: ${content.target_id??'미확인'} · 본문 대상 버전: ${content.target_version??'미확인'}`),
    el('p',`위치 대상 버전: ${content.locator_target_version??'미확인'}`),
    el('p',`UTF-8 텍스트 SHA-256: ${content.text_sha256??'미확인'}`,'muted'),
    el('p',`위치 검증: ${verification.status} · ${verification.reason}`,'source-verification'));
   if(data.source.location){
    const l=data.source.location;
    resultBox.append(el('p',`응답 위치: ${l.target} · ${l.unit} · [${l.start}, ${l.end}) · ${l.boundary}`));
   }
   if(content.availability!=='AVAILABLE'){
    resultBox.append(el('p',`본문 ${states[content.availability]} · 빈 본문이나 기록 본문으로 대체하지 않습니다.`));return;
   }
   if(content.text==='')resultBox.append(el('p','빈 본문 (이용 가능)'));
   const text=el('pre',content.text,'body-text source-body');text.tabIndex=0;
   if(highlight){
    const mark=el('mark',content.text.slice(highlight.start,highlight.end));
    text.replaceChildren(document.createTextNode(content.text.slice(0,highlight.start)),mark,document.createTextNode(content.text.slice(highlight.end)));
   }
   resultBox.append(text);
  }catch(error){
   if(token.current()&&detailToken.current()&&error.name!=='AbortError'){
    status.className='source-status error';status.textContent=errorText(error);
   }
  }finally{if(token.current()&&detailToken.current())resultBox.setAttribute('aria-busy','false');}
 });
 load.disabled=typeof source.version!=='string'||!source.version.length;
 section.append(load,status,resultBox);return section;
}
function simulationUI(id){
 const section=el('section',undefined,'simulation');section.append(el('h3','합성 검토 연습'),el('p','이 탭의 합성 세션에만 기록됩니다. 위의 원형 상태·기존 이력·실제 원장은 변경하지 않습니다.'));
 const form=el('form'),select=el('select');select.id='simulation-action';select.append(new Option('보류 연습','HELD'),new Option('근거 요청 연습','EVIDENCE_REQUESTED'),new Option('제안 초안 연습','DRAFT'));
 const actionLabel=el('label','합성 동작');actionLabel.append(select);const note=el('textarea');note.id='simulation-note';note.required=true;note.maxLength=1000;
 const noteLabel=el('label','합성 사유 (실제 자료 입력 금지)');noteLabel.append(note);const save=el('button','합성 세션에 기록');save.type='submit';form.append(actionLabel,noteLabel,save);
 const status=el('p');status.setAttribute('role','status');const events=el('ol');
 function read(){const raw=JSON.parse(sessionStorage.getItem(NS)??'[]');if(!Array.isArray(raw))throw Error();return raw;}
 function render(){events.replaceChildren();for(const event of read().filter(e=>e.record_id===id&&e.dataset_id===client.dataset.dataset_id&&e.snapshot_id===client.dataset.snapshot_id))events.append(el('li',`${event.id} · ${label(event.action)} · ${event.note} · 합성 전용`));}
 form.onsubmit=e=>{e.preventDefault();if(!note.value.trim())return;try{const all=read();if(all.length>=1000)throw Error();all.push({id:`synthetic-session-${all.length+1}`,record_id:id,action:select.value,note:note.value.trim(),dataset_id:client.dataset.dataset_id,snapshot_id:client.dataset.snapshot_id});sessionStorage.setItem(NS,JSON.stringify(all));render();status.textContent='합성 세션에만 기록됨 · 실제 승인·동기화 아님';note.value='';}catch{status.textContent='합성 세션 저장 실패 · 기록되지 않았거나 저장 상태를 확인할 수 없습니다.';}};
 section.append(form,status,events,button('이 탭의 합성 연습 기록 지우기',()=>{try{sessionStorage.removeItem(NS);render();status.textContent='합성 연습 기록만 지웠습니다.';}catch{status.textContent='세션 저장소 접근 실패';}}));
 try{render();}catch{status.textContent='합성 세션 저장소를 사용할 수 없습니다.';}return section;
}
$('filters').onsubmit=e=>{e.preventDefault();invalidate();search(true);};
$('filters').onreset=()=>{invalidate();setTimeout(()=>search(true),0);};
$('search').oninput=()=>{invalidate();notify('검색 조건 변경 중…');timer=setTimeout(()=>search(true),180);};
for(const id of ['project','role','review','sort'])$(id).onchange=()=>{invalidate();search(true);};
$('next').onclick=()=>{if(nextCursor){cursors[++page]=nextCursor;search();}};
$('prev').onclick=()=>{if(page>0){--page;search();}};
$('reconnect').onclick=()=>bootstrap();
$('scenario').onchange=()=>{client.scenario=$('scenario').value;invalidate();search(true);};
bootstrap();
