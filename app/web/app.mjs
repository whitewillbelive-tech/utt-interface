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
function failure(error){notify(errorText(error),'error');renderHome();renderConversations();}
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
function hideDetail(){cancelSources();detailGate.cancel();currentDetailId=null;$('library-view').classList.remove('is-reading');$('detail').hidden=true;$('detail').replaceChildren();$('list-view').hidden=false;}
function invalidate(){++navigationEpoch;listNeedsReset=true;pageResultValid=false;renderHome();clearTimeout(timer);listGate.cancel();$('results').setAttribute('aria-busy','false');hideDetail();nextCursor=null;$('results').replaceChildren();$('next').disabled=true;$('prev').disabled=true;}
async function bootstrap(){
 invalidate();const token=bootGate.begin();client.dataset=null;capabilities=null;features=null;mode=null;projects=[];pageRecords=[];pageResultValid=false;visited=[];client.scenario='normal';renderHome();renderConversations();
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
     if(p.classification_observed)b.append(el('small',p.classification_observed));$('projects').append(b);
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
  updateControls();renderHome();renderConversations();
  if(supports('search'))await search(true);
  else notify('공급자가 검색을 지원하지 않습니다. 지원 범위를 확인하세요.','warning');
  if(token.current()&&navigationFailed&&$('notice').className!=='error')notify('자료 검색과 별도로 프로젝트 탐색을 완료하지 못했습니다. 기준 다시 조회로 재시도하세요.','warning');
 }catch(error){if(token.current()&&error.name!=='AbortError'){$('connection-state').textContent='공급자: 읽기 연결 미확인 · 실제 정본: 미확인';failure(error);}}
}
function selectProject(id){
 if(!filterSupported('project')){notify(`프로젝트 필터: ${filterLabel('project')} · 검색 조건을 바꾸지 않았습니다.`,'warning');return;}
 if(id!==''&&!projects.some(p=>p.project_id===id)){notify('연결 대상 프로젝트 미확인 · 검색 조건을 바꾸지 않았습니다.','warning');return;}
 setStudioView('library',{focus:false,refresh:false});$('project').value=id;invalidate();search(true);
}
async function search(reset=false){
 if(!client.dataset){notify('공급자 미연결: 기준 다시 조회를 눌러 주세요.','error');return;}
 if(!supports('search')){notify('공급자가 검색을 지원하지 않습니다.','warning');return;}
 hideDetail();if(reset||listNeedsReset){page=0;cursors=[null];}listNeedsReset=false;pageResultValid=false;const token=listGate.begin();
 $('results').replaceChildren();$('results').setAttribute('aria-busy','true');$('count').textContent='';notify('자료를 불러오는 중…');$('next').disabled=true;$('prev').disabled=true;
 if(!synthetic())$('sort').value=providerSort;
 client.sort=$('sort').value;
 try{
  const d=await client.read('search',{query:filterState('query')==='UNSUPPORTED'?null:$('search').value||null,project_id:filterSupported('project')?$('project').value||null:null,document_role:filterSupported('document_role')?$('role').value||null:null,review_status:filterSupported('review_status')?$('review').value||null:null,cursor:cursors[page],page_size:pageSize()},token.signal);
  if(!token.current())return false;
  pageRecords=d.result.records;pageResultValid=true;
  nextCursor=d.result.page.next_cursor;$('page-number').textContent=`${page+1} 페이지`;$('count').textContent=`이 페이지 ${d.result.records.length}건 · 한 번에 ${d.result.page.page_size}건`;
  for(const r of d.result.records){
   if(![...$('role').options].some(o=>o.value===r.document_role))$('role').append(new Option(roleName(r.document_role),r.document_role));
   const card=el('article',undefined,'record');const b=button(r.title,()=>{listOriginId=r.record_id;listScrollY=window.scrollY;showDetail(r.record_id);},'record-title');b.dataset.recordId=r.record_id;b.disabled=!supports('detail');
   const cardTop=el('div',undefined,'record-heading');cardTop.append(el('span',roleName(r.document_role),'eyebrow'),el('span',label(r.state.review),'review-label'));
   card.append(cardTop,b);
   if(r.display_summary!==null&&r.display_summary!=='')card.append(el('p',r.display_summary,'record-summary'));
   card.append(proofDetails('자료 정보와 상태',el('p',`자료 ID: ${r.record_id}`,'muted'),el('p',`원래 자료 유형: ${r.document_role}`,'muted'),summaryStates(r)));
   if(!supports('detail'))card.append(el('p','공급자가 상세 조회를 지원하지 않습니다.','muted'));
   const links=el('p',r.project_relations.length?'프로젝트: ':r.project_relations_status==='AVAILABLE'?'미분류 자료 · 이 기준에서 기록된 소속 없음':`프로젝트 관계: ${label(r.project_relations_status)}`);
   r.project_relations.forEach((l,i)=>{if(i)links.append(document.createTextNode(' / '));links.append(document.createTextNode(`${projectRelation(l,r.record_id).text} (${label(l.assertion_status)})`));});
   card.append(links,el('p',`출처: ${label(r.source_status)} · 상세에서 읽기`,'muted'));$('results').append(card);
  }
  if(!d.result.records.length)$('results').append(el('p','조건에 맞는 기록이 없습니다. 연결 오류와는 다릅니다.','empty'));
  $('prev').disabled=page===0;$('next').disabled=!nextCursor;
  const queryNotice=$('search').value&&!filterSupported('query')?` · 검색어 ${filterLabel('query')} (제공자 응답 기준)`:'';
  notify(`이 페이지에서 ${d.result.records.length}개의 ${synthetic()?'합성 자료':'자료'}를 살펴볼 수 있습니다.${queryNotice}`);renderHome();return true;
 }catch(error){if(token.current()&&error.name!=='AbortError'){pageResultValid=false;failure(error);}return false;}
 finally{if(token.current())$('results').setAttribute('aria-busy','false');}
}
function back(){
 hideDetail();notify('검색 조건과 페이지를 유지했습니다.');
 const match=[...$('results').querySelectorAll('.record-title[data-record-id]')].find(n=>n.dataset.recordId===listOriginId);
 match?.focus({preventScroll:true});window.scrollTo({top:listScrollY});
}
async function showDetail(id){
 if(!supports('detail')){notify('공급자가 상세 조회를 지원하지 않습니다.','warning');return;}
 cancelSources();currentDetailId=id;const token=detailGate.begin();listGate.cancel();$('library-view').classList.add('is-reading');$('list-view').hidden=true;$('results').setAttribute('aria-busy','false');
 const box=$('detail');box.hidden=false;box.replaceChildren(button('← 검색 결과로',back),el('p','자료를 펼치는 중…'));box.focus();notify('상세 조회 중…');
 try{
  const d=await client.read('detail',{record_id:id,page_size:pageSize()},token.signal);if(!token.current()||currentDetailId!==id)return;const r=d.result.record;
  rememberProjects(d.result.related_projects);trackReading(r,d.snapshot);
  const heading=el('header',undefined,'reading-header');const title=el('h2',r.title);title.tabIndex=-1;
  heading.append(el('p',`${$('project').value?projectName($('project').value):'전체 자료'} · ${page+1}페이지`,'reading-breadcrumb'),el('p',roleName(r.document_role),'eyebrow'),title);
  box.replaceChildren(button('← 검색 결과로',back),heading);
  const layout=el('div',undefined,'reading-layout'),main=el('article',undefined,'reading-main'),context=el('aside',undefined,'reading-context');
  layout.append(main,context);box.append(layout);
  if(r.display_summary!==null&&r.display_summary!==''){const lead=el('section',undefined,'reading-summary');lead.append(el('h3','내용의 실마리'),el('p',r.display_summary));main.append(lead);}
  main.append(el('h3','본문'),el('p',`본문 상태: ${label(r.content_availability)}`,'muted'));
  const body=el('pre',r.raw_text===null?`원문 ${label(r.content_availability)} · 요약으로 대체하지 않습니다.`:r.raw_text===''?'빈 원문 (이용 가능)':r.raw_text,'body-text');body.tabIndex=0;main.append(body);
  context.append(el('h3','지금 자료의 상태'),summaryStates(r),el('p','관측·검토·제안·정본 승인은 서로 다른 상태입니다.','muted'));
  context.append(el('h3','프로젝트와 맥락'));
  if(!r.project_relations.length)context.append(el('p',r.project_relations_status==='AVAILABLE'?`미분류 · 관계 상태: ${label(r.project_relations_status)}`:`프로젝트 관계: ${label(r.project_relations_status)}`));
  for(const l of r.project_relations){
   const target=projectRelation(l,r.record_id);
   if(target.id&&supports('search')&&filterSupported('project'))context.append(button(`${target.text} · ${label(l.assertion_status)}`,()=>selectProject(target.id),'text-link'));
   else context.append(el('p',`${target.text} · ${label(l.assertion_status)}${target.id?` · 프로젝트 필터 ${filterLabel('project')}`:''}`,'muted'));
  }
  for(const project of d.result.related_projects){
   if(r.project_relations.some(l=>endpoint(l,r.record_id)?.dataset===client.dataset.dataset_id&&endpoint(l,r.record_id)?.id===project.project_id))continue;
   if(supports('search')&&filterSupported('project'))context.append(button(`상세 응답 관련 프로젝트: ${project.name}`,()=>selectProject(project.project_id),'text-link'));
   else context.append(el('p',`상세 응답 관련 프로젝트: ${project.name} · 프로젝트 필터 ${filterLabel('project')}`));
  }
  context.append(proofDetails('자료 식별·연결 정보',el('p',`자료 ID: ${r.record_id}`),el('p',`데이터셋: ${d.snapshot.dataset_id}`),el('p',`기준 버전: ${d.snapshot.snapshot_id}`),el('p',`원래 자료 유형: ${r.document_role}`),el('p',`본문 SHA-256: ${r.raw_text_sha256??'미확인'}`)));
  const sources=el('section',undefined,'reading-sources');sources.append(el('h3','출처를 따라 읽기'),el('p',`출처 상태: ${label(r.source_status)}`));main.append(sources);
  if(!d.result.sources.length)sources.append(el('p','이 조회 범위의 출처 항목 없음. 전체 출처의 부재를 뜻하지 않습니다.'));
  d.result.sources.forEach((source,index)=>sources.append(sourceCard(source,r,d.snapshot,token,index+1)));
  const history=el('section'),historyPages=el('div'),historyControls=el('div');
  history.append(el('h3',synthetic()?'기존 합성 이력과 관련 자료':'이력과 관련 자료'),historyPages,historyControls);context.append(history);
  const completeNotice=()=>notify(`${synthetic()?'합성 자료':'자료'}를 읽고 있습니다.`);
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
  if(synthetic())main.append(proofDetails('합성 검토 연습 (이 탭에서만)',simulationUI(id)));
  context.append(el('p','정본 승인·실제 저장·동기화는 명령 계약 미정으로 지원하지 않습니다.','warning'));
  if(historyOk)completeNotice();
 }catch(error){if(token.current()&&error.name!=='AbortError'){box.replaceChildren(button('← 검색 결과로',back),el('p','상세를 읽지 못했습니다.'));failure(error);}}
}
// Each card owns its request generation; the detail generation also guards
// responses arriving after record navigation, filter changes or list return.
function sourceCard(source,record,snapshot,detailToken,number){
 const section=el('section',undefined,'source');
 section.dataset.sourceId=source.source_id;
 section.append(el('h4',`출처 ${number}`),el('p',`메타데이터 상태: ${label(source.availability)}${source.version?'':' · 출처 버전: 미확인'}`,'source-meta-status'));
 const originalProof=proofDetails('출처 메타데이터와 근거',el('p','상세에서 받은 출처 메타데이터 확인'),el('p',`출처 ID: ${source.source_id}`),
  el('p',`데이터셋: ${source.dataset_id}`),el('p',`출처 버전: ${source.version??'미확인'}`),el('p',`원본 파일 바이트 SHA-256: ${source.content_sha256??'미확인'}`,'muted'));
 if(source.location)originalProof.append(el('p',`메타데이터 위치: ${source.location.target} · ${source.location.unit} · [${source.location.start}, ${source.location.end})`));
 section.append(originalProof);
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
   const responseProof=proofDetails('본문 버전·해시·위치 근거',el('p',`응답 출처 메타데이터 버전: ${data.source.version}`),
    el('p',`응답 원본 파일 바이트 SHA-256: ${data.source.content_sha256??'미확인'}`,'muted'),
    el('p',`본문 대상: ${content.target_kind??'미확인'}${content.target_kind==='RECORD_BODY'?' · 기록 본문 대상 (출처 본문과 구분)':content.target_kind==='SOURCE_BODY'?' · 출처 본문':''}`),
    el('p',`대상 ID: ${content.target_id??'미확인'} · 본문 대상 버전: ${content.target_version??'미확인'}`),
    el('p',`위치 대상 버전: ${content.locator_target_version??'미확인'}`),
    el('p',`UTF-8 텍스트 SHA-256: ${content.text_sha256??'미확인'}`,'muted'),
    el('p',`위치 검증: ${verification.status} · ${verification.reason}`,'source-verification'));
   if(data.source.location){
    const l=data.source.location;
    responseProof.append(el('p',`응답 위치: ${l.target} · ${l.unit} · [${l.start}, ${l.end}) · ${l.boundary}`));
   }
   const verificationLabels={VERIFIED:'본문과 위치가 일치합니다.',UNVERIFIED:'위치를 아직 확인하지 못했습니다.',NOT_SUPPORTED:'이 위치 방식은 지원되지 않습니다.',MISMATCH:'본문과 위치 근거가 일치하지 않습니다.',NOT_AVAILABLE:'본문이 없어 위치를 확인하지 못했습니다.'};
   resultBox.append(el('p',content.target_kind==='SOURCE_BODY'?'출처 본문':content.target_kind==='RECORD_BODY'?'기록 본문 대상 (출처 본문과 구분)':'본문 대상 미확인','source-target'),el('p',verificationLabels[verification.status],'location-status'),responseProof);
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
// Studio navigation and conversation models are UI-only. No new API, data
// classification, chronological order or persisted progress is implied.
const roleNames={SYNTHETIC_NOTE:'합성 노트',RESEARCH:'연구 자료',RESEARCH_NOTE:'연구 노트',CREATIVE_NOTE:'창작 노트',CHARACTER:'인물',PERSON:'인물 자료',EVENT:'사건',WORLD_BUILDING:'세계관',MANUSCRIPT:'원고',IDEA:'아이디어',DISCUSSION:'논의 기록',CONVERSATION:'대화 기록'};
const roleName=value=>roleNames[value]??(value?`자료 · ${value}`:'자료');
let studioView='studio',navigationEpoch=0,pageRecords=[],pageResultValid=false,listNeedsReset=true,visited=[],selectedDemo=0;
function proofDetails(title,...content){const d=el('details',undefined,'proof-details');d.append(el('summary',title),...content);return d;}
function setStudioView(view,{focus=true,historyMode='push',refresh=true}={}){
 if(!['studio','library','conversations'].includes(view))view='studio';
 ++navigationEpoch;
 if(view!=='library'){clearTimeout(timer);listGate.cancel();$('results').setAttribute('aria-busy','false');hideDetail();}else if(focus)hideDetail();
 studioView=view;
 for(const [name,id] of [['studio','studio-home'],['library','library-view'],['conversations','conversations-view']])$(id).hidden=view!==name;
 for(const [name,id] of [['studio','nav-home'],['library','nav-library'],['conversations','nav-conversations']]){
  if(view===name)$(id).setAttribute('aria-current','page');else $(id).removeAttribute('aria-current');
 }
 if(historyMode!=='none'&&location.hash!==`#${view}`){
  history[historyMode==='replace'?'replaceState':'pushState'](null,'',`#${view}`);
 }
 renderHome();renderConversations();
 if(focus)$(view==='studio'?'home-heading':view==='library'?'workspace-title':'conversations-heading').focus();
 if(refresh&&view==='library'&&!pageResultValid&&client.dataset&&supports('search'))search();
}
function captureListContext(){return {page,cursors:[...cursors],originId:listOriginId,scrollY:listScrollY,values:Object.fromEntries(['search','project','role','review','sort'].map(id=>[id,$(id).value]))};}
function trackReading(record,snapshot){
 const entry={id:record.record_id,title:record.title,role:record.document_role,dataset:snapshot.dataset_id,snapshot:snapshot.snapshot_id,context:captureListContext()};
 visited=[entry,...visited.filter(item=>item.id!==entry.id||item.dataset!==entry.dataset||item.snapshot!==entry.snapshot)].slice(0,6);
 renderHome();
}
async function resumeReading(entry){
 if(!client.dataset||entry.dataset!==client.dataset.dataset_id||entry.snapshot!==client.dataset.snapshot_id){failure({code:'VERSION_MISMATCH'});return;}
 setStudioView('library',{focus:false,refresh:false});const epoch=navigationEpoch;
 for(const [id,value] of Object.entries(entry.context.values)){
  if(id==='role'&&value&&![...$('role').options].some(option=>option.value===value))$('role').append(new Option(roleName(value),value));
  $(id).value=value;
 }
 page=entry.context.page;cursors=[...entry.context.cursors];listNeedsReset=false;
 const loaded=await search();
 if(!loaded||navigationEpoch!==epoch||studioView!=='library')return;
 listOriginId=entry.context.originId;listScrollY=entry.context.scrollY;
 showDetail(entry.id);
}
function openHomeRecord(record){
 setStudioView('library',{focus:false,refresh:false});listOriginId=record.record_id;
 const origin=[...$('results').querySelectorAll('[data-record-id]')].find(node=>node.dataset.recordId===record.record_id);
 origin?.scrollIntoView({block:'center'});listScrollY=window.scrollY;showDetail(record.record_id);
}
function reviewFromHome(status){
 if(!supports('search')||!filterSupported('review_status'))return;
 setStudioView('library',{focus:false,refresh:false});
 $('search').value='';$('project').value='';$('role').value='';$('review').value=status;
 invalidate();search(true);
}
function renderHome(){
 if(!$('home-projects'))return;
 const projectBox=$('home-projects'),continueBox=$('home-continue'),reviewBox=$('home-review'),discussionBox=$('home-discussions');
 for(const box of [projectBox,continueBox,reviewBox,discussionBox])box.replaceChildren();
 if(!client.dataset){
  projectBox.append(el('p','자료가 연결되면 프로젝트의 맥락을 여기서 살펴볼 수 있습니다.','empty-note'));
  reviewBox.append(el('p','연결된 자료가 없어 검토할 항목을 확인하지 않았습니다.','empty-note'));
 }else{
  projectBox.append(el('p',pageResultValid?'아래 현황은 지금 불러온 목록 범위입니다. 전체 자료 수나 최근 활동을 뜻하지 않습니다.':'프로젝트 이름은 제공된 목록 기준입니다. 자료 현황은 목록을 불러온 뒤 확인할 수 있습니다.','section-note'));
  for(const project of projects){
   const card=el('article',undefined,'studio-card project-card');
   card.append(el('span','PROJECT','eyebrow'),el('h3',project.name));
   const count=pageRecords.filter(record=>record.project_relations.some(link=>{const target=endpoint(link,record.record_id);return target?.dataset===client.dataset.dataset_id&&target.id===project.project_id;})).length;
   card.append(el('p',pageResultValid?`현재 목록 ${pageRecords.length}건 중 ${count}건 연결`:'자료 현황 미확인','project-count'));
   const open=button('프로젝트 살펴보기',()=>{setStudioView('library',{focus:false});selectProject(project.project_id);},'text-link');
   open.disabled=!supports('search')||!filterSupported('project');card.append(open);
   if(open.disabled)card.append(el('p',`프로젝트 필터 ${filterLabel('project')}`,'muted'));
   card.append(proofDetails('프로젝트 맥락',el('p',`관측 분류: ${label(project.classification_observed)}`),el('p',`사용자 확정 소속: ${label(project.membership_user_confirmed)}`)));
   projectBox.append(card);
  }
  if(!projects.length)projectBox.append(el('p',supports('projects')?'제공된 프로젝트 목록이 비어 있거나 아직 조회하지 못했습니다.':'현재 제공자는 프로젝트 목록을 지원하지 않습니다.','empty-note'));
  const flagged=pageResultValid?pageRecords.filter(record=>['HELD','EVIDENCE_REQUESTED'].includes(record.state.review)||record.state.observation==='CONFLICTING'||record.state.proposal==='CONFLICT'):[];
  reviewBox.append(el('p','지금 목록에 표시된 보류·근거 요청·충돌 자료입니다.','section-note'));
  for(const record of flagged){const item=el('div',undefined,'studio-reading-card');const open=button(`검토하기: ${record.title}`,()=>openHomeRecord(record),'record-link');open.disabled=!supports('detail');item.append(open,el('p',`검토 ${label(record.state.review)} · 관측 ${label(record.state.observation)} · 제안 ${label(record.state.proposal)}`,'muted'));reviewBox.append(item);}
  if(!flagged.length)reviewBox.append(el('p',pageResultValid?'이 목록 범위에는 보류·충돌 표시가 없습니다. 다른 페이지의 상태는 포함하지 않습니다.':'목록을 불러오면 확인할 자료를 표시합니다.','empty-note'));
  const actions=el('div',undefined,'home-actions');
  for(const [name,status] of [['보류 자료 찾아보기','HELD'],['근거 요청 자료 찾아보기','EVIDENCE_REQUESTED']]){const action=button(name,()=>reviewFromHome(status));action.disabled=!supports('search')||!filterSupported('review_status');actions.append(action);}
  reviewBox.append(actions);
  if(!filterSupported('review_status'))reviewBox.append(el('p',`검토 상태 필터 ${filterLabel('review_status')}`,'muted'));
 }
 const currentVisits=visited.filter(entry=>client.dataset&&entry.dataset===client.dataset.dataset_id&&entry.snapshot===client.dataset.snapshot_id);
 continueBox.append(el('p','이 탭의 현재 연결에서 본 자료입니다. 본문이나 진행률을 저장하지 않습니다.','section-note'));
 for(const entry of currentVisits){const item=el('div',undefined,'studio-reading-card');const resume=button(`이어서 읽기: ${entry.title}`,()=>resumeReading(entry),'record-link');resume.disabled=!supports('detail')||!supports('search');item.append(el('span',roleName(entry.role),'eyebrow'),resume);continueBox.append(item);}
 if(!currentVisits.length)continueBox.append(el('p','아직 이 탭에서 연 자료가 없습니다. 자료를 읽으면 이곳에서 이어갈 수 있습니다.','empty-note'));
 discussionBox.append(el('p','대화 기록 연결 전','status-label'),el('p','최근 논의를 확인할 대화 구조와 시간 정보는 아직 제공되지 않았습니다. 실제 대화를 요약하거나 분석한 내용은 표시하지 않습니다.','section-note'));
 const demo=button('합성 대화 보기 예시',()=>setStudioView('conversations'),'text-link');demo.disabled=!synthetic();discussionBox.append(demo);
 if(!synthetic())discussionBox.append(el('p','합성 모드에서만 표시 예시를 살펴볼 수 있습니다.','muted'));
}
const DEMO_CONVERSATIONS=[
 {topic:'기억을 보관하는 도시의 풍경',discussion:'가상의 도시를 배경으로 장소와 기억이 이야기에서 어떤 역할을 할지 논의하는 시연입니다.',userOpinion:'기록을 모으는 장소보다, 인물이 서로 다른 기억을 발견하는 장소가 되었으면 합니다.',aiOpinion:'하나의 광장을 세 인물의 시점으로 나누어 보여주면 기억의 차이를 드러낼 수 있습니다.',conclusion:null,status:'논의 중 · 시점별 장면은 아직 정하지 않음',messages:[{speaker:'user',text:'가상의 도시에서 기억을 어떻게 공간으로 표현할 수 있을까요?'},{speaker:'assistant',text:'기억이 겹치는 광장과 서로 다른 세 인물의 시점을 예시로 생각해 볼 수 있습니다.'},{speaker:'user',text:'장면의 결론은 열어 두고 먼저 인물별 관점을 살펴보고 싶습니다.'}],metadata:{example:'synthetic-only',display_model:'UI demonstration; Work contract pending'}},
 {topic:'연구 노트를 질문으로 다시 읽기',discussion:'완전히 가상인 노트 묶음을 정리하기 전에 질문과 근거를 구분하는 방법을 논의합니다.',userOpinion:'정리된 결론뿐 아니라 당시 망설였던 질문도 함께 남기고 싶습니다.',aiOpinion:'질문·관찰·가설을 나누어 표시하고, 확정하지 않은 관계는 보류 상태로 남길 수 있습니다.',conclusion:'이 가상 예시에서는 질문과 가설을 구별해 표시하기로 했습니다. 실제 저장이나 승인은 수행하지 않았습니다.',status:'시연 속 가상 합의 · 실제 확정 아님',messages:[{speaker:'user',text:'가상 노트에서 질문과 결론을 구분하고 싶습니다.'},{speaker:'assistant',text:'질문을 먼저, 관찰과 가설은 따로 읽는 구성을 시연해 보겠습니다.'}],metadata:{example:'synthetic-only',display_model:'UI demonstration; Work contract pending'}}
];
// An independent DOM component using a local presentation model. This is not a
// Work message schema, parser, classifier, or live conversation API adapter.
export function createConversationView(model){
 const box=el('article',undefined,'conversation-view');
 box.append(el('p','합성 대화 시연 · 실제 대화 분석 아님','demo-label'),el('h3',model.topic??'주제 미제공','conversation-topic'),el('p',model.discussion??'논의 내용 미제공','conversation-summary'));
 const opinions=el('div',undefined,'conversation-opinions');
 for(const [name,value,cls] of [['사용자 의견',model.userOpinion,'conversation-user'],['AI 의견',model.aiOpinion,'conversation-ai']]){const part=el('section',undefined,cls);part.append(el('h4',name),el('p',value??'의견 미제공'));opinions.append(part);}
 box.append(opinions);
 const outcome=el('section',undefined,'conversation-outcome');outcome.append(el('h4',model.conclusion?'시연에서 정리한 결론':'아직 확정하지 않은 논의'),el('p',model.conclusion??'아직 확정된 결론이 없습니다.'),el('p',model.status??'마지막 논의 상태 미제공','status-label'));box.append(outcome);
 const originals=el('details',undefined,'conversation-originals');originals.append(el('summary','원본 메시지 보기'));
 for(const [index,message] of (Array.isArray(model.messages)?model.messages:[]).entries()){const item=el('section',undefined,'conversation-message');item.append(el('h4',`${index+1}. ${message.speaker==='user'?'사용자':message.speaker==='assistant'?'AI':'참여자'}`),el('p',message.text??''));originals.append(item);}
 const meta=el('details',undefined,'conversation-metadata');meta.append(el('summary','시연 정보'),el('pre',JSON.stringify(model.metadata??{},null,2)));box.append(originals,meta);return box;
}
function renderConversations(){
 if(!$('conversation-content'))return;
 const box=$('conversation-content');box.replaceChildren();
 if(!synthetic()){box.append(el('p','대화 구조 연결 전','status-label'),el('p','현재 연결에서 대화 기록을 조회하지 않았습니다. 실제 대화를 가상 대화로 대체하지 않습니다.','empty-note'));return;}
 box.append(el('p','이 화면은 Work의 최종 대화 규격을 기다리는 독립 표시 컴포넌트의 예시입니다. 아래 주제와 메시지는 모두 가상이며 현재 데이터셋에서 가져오지 않았습니다.','section-note'));
 const choices=el('div',undefined,'conversation-choices');
 DEMO_CONVERSATIONS.forEach((model,index)=>{const choose=button(model.topic,()=>{selectedDemo=index;renderConversations();$('conversation-content').querySelector('.conversation-topic').focus();});choose.setAttribute('aria-pressed',String(index===selectedDemo));choices.append(choose);});
 box.append(choices,createConversationView(DEMO_CONVERSATIONS[selectedDemo]));box.querySelector('.conversation-topic').tabIndex=-1;
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
for(const [id,view] of [['nav-home','studio'],['nav-library','library'],['nav-conversations','conversations']])$(id).onclick=()=>setStudioView(view);
document.addEventListener('click',event=>{const link=event.target.closest('a[href^="#"]');if(!link)return;const view=link.getAttribute('href').slice(1);if(['studio','library','conversations'].includes(view)){event.preventDefault();setStudioView(view);}});
document.querySelector('.skip').onclick=event=>{event.preventDefault();const target=studioView==='library'&&currentDetailId?$('detail').querySelector('h2')??$('detail'):$(studioView==='studio'?'home-heading':studioView==='library'?'workspace-title':'conversations-heading');target.focus();};
window.addEventListener('hashchange',()=>setStudioView(location.hash.slice(1),{historyMode:'none'}));
if(matchMedia('(max-width: 700px)').matches){$('project-context').open=false;}
setStudioView(location.hash.slice(1)||'studio',{focus:false,historyMode:'replace'});
bootstrap();
