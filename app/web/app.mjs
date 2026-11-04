import {ReadClient,LatestRequest,sourceRange} from './client.mjs';
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,action,cls)=>{const n=el('button',text,cls);n.type='button';n.onclick=action;return n;};
const labels={UNREVIEWED:'미검토',HELD:'보류',EVIDENCE_REQUESTED:'근거 요청',CANCELLED:'취소',OBSERVED:'관측됨',CONFLICTING:'관측 충돌',NONE:'없음',DRAFT:'초안',CONFLICT:'충돌',NOT_APPROVED:'미승인',UNKNOWN:'미확인',AVAILABLE:'이용 가능',MISSING:'누락 확인',NOT_REQUESTED:'미요청',INACCESSIBLE:'접근 불가',USER_CONFIRMED:'사용자 확정',UNRESOLVED:'미해결'};
const label=v=>v==null?'미확인':labels[v]??v;
const client=new ReadClient(), listGate=new LatestRequest(), detailGate=new LatestRequest(), bootGate=new LatestRequest();
let page=0,cursors=[null],nextCursor=null,projects=[],mode=null,capabilities=null,providerSort='id_asc',listOriginId=null,listScrollY=0,currentDetailId=null,timer;
const NS='utt.synthetic.session.v1';
const supports=operation=>capabilities?.operations.includes(operation)===true;
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
  INTERNAL_ERROR:'읽기 공급자가 요청을 완료하지 못했습니다.',HTTP_ERROR:'읽기 요청이 거절되었거나 처리되지 않았습니다.'};
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
 $('project').disabled=!canSearch||projects.length===0;
 $('sort').disabled=!canSearch||!synthetic();
 for(const n of $('projects').querySelectorAll('button'))n.disabled=!canSearch;
 for(const n of $('components').querySelectorAll('button'))n.disabled=!canSearch;
}
function hideDetail(){detailGate.cancel();currentDetailId=null;$('detail').hidden=true;$('detail').replaceChildren();$('list-view').hidden=false;}
function invalidate(){clearTimeout(timer);listGate.cancel();hideDetail();nextCursor=null;$('results').replaceChildren();$('next').disabled=true;$('prev').disabled=true;}
async function bootstrap(){
 invalidate();const token=bootGate.begin();client.dataset=null;capabilities=null;mode=null;projects=[];client.scenario='normal';
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
  $('mode').textContent=synthetic()?'합성 개발 자료 · 운영 연결 없음':'읽기 공급자 응답 확인 · 운영 승인 없음';
  $('dataset-kind').textContent=synthetic()?'SYNTHETIC ONLY':'읽기 전용';
  $('connection-state').textContent='공급자: 읽기 응답 확인 · 실제 정본: 미확인';
  $('synthetic-scenarios').hidden=!synthetic();
  $('dataset').textContent=`${cap.snapshot.dataset_id} / ${cap.snapshot.dataset_kind} / 기준 ${cap.snapshot.snapshot_id}`;
  const savedRole=$('role').value;
  $('role').replaceChildren(new Option('모든 유형',''));
  if(synthetic())$('role').append(new Option('합성 노트','SYNTHETIC_NOTE'));
  if(savedRole){if(![...$('role').options].some(o=>o.value===savedRole))$('role').append(new Option(savedRole,savedRole));$('role').value=savedRole;}
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
  const d=await client.read('search',{query:$('search').value,project_id:$('project').value||null,document_role:$('role').value||null,review_status:$('review').value||null,cursor:cursors[page],page_size:pageSize()},token.signal);
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
  $('prev').disabled=page===0;$('next').disabled=!nextCursor;notify(`${synthetic()?'합성 자료':'읽기 자료'} 조회 완료 · 실제 정본 상태는 미확인`);
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
 currentDetailId=id;const token=detailGate.begin();listGate.cancel();$('list-view').hidden=true;$('results').setAttribute('aria-busy','false');
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
   if(target.id&&supports('search'))box.append(button(`${target.text} · ${label(l.assertion_status)}`,()=>selectProject(target.id),'text-link'));
   else box.append(el('p',`${target.text} · ${label(l.assertion_status)}`,'muted'));
  }
  for(const project of d.result.related_projects){
   if(r.project_relations.some(l=>endpoint(l,r.record_id)?.dataset===client.dataset.dataset_id&&endpoint(l,r.record_id)?.id===project.project_id))continue;
   if(supports('search'))box.append(button(`상세 응답 관련 프로젝트: ${project.name}`,()=>selectProject(project.project_id),'text-link'));
   else box.append(el('p',`상세 응답 관련 프로젝트: ${project.name}`));
  }
  box.append(el('p','관측 관계·사용자 확정 소속·제안·정본 승인은 독립 상태입니다.'));
  box.append(el('h3','보존 본문'),el('p',`본문 상태: ${label(r.content_availability)}`,'muted'));
  const body=el('pre',r.raw_text===null?`원문 ${label(r.content_availability)} · 요약으로 대체하지 않습니다.`:r.raw_text===''?'빈 원문 (이용 가능)':r.raw_text,'body-text');body.tabIndex=0;box.append(body);
  box.append(el('h3','출처와 원문 위치'),el('p',`출처 상태: ${label(r.source_status)}`));
  if(!d.result.sources.length)box.append(el('p','이 조회 범위의 출처 항목 없음. 전체 출처의 부재를 뜻하지 않습니다.'));
  for(const s of d.result.sources){
   const section=el('section',undefined,'source');section.append(el('h4',s.source_id),el('p',`${label(s.availability)} · 버전 ${s.version??'미확인'}`));
   const status=el('p','좌표 검증 전');section.append(status);
   if(s.location)section.append(el('p',`${s.location.target} · ${s.location.unit} · [${s.location.start}, ${s.location.end}) · 반열린 구간`));
   section.append(button('출처 위치 확인',async()=>{
    try{
     const range=await sourceRange(r.raw_text,s,r.raw_text_sha256);if(!token.current())return;
     status.textContent=range.ok?`현재 본문·조각 해시 일치: ${range.fragment}`:range.reason;
     if(range.ok){const mark=el('mark',r.raw_text.slice(range.start,range.end));mark.tabIndex=-1;
      body.replaceChildren(document.createTextNode(r.raw_text.slice(0,range.start)),mark,document.createTextNode(r.raw_text.slice(range.end)));mark.focus();mark.scrollIntoView({block:'center'});}
    }catch{if(token.current())status.textContent='해시 검증을 수행할 수 없습니다. 좌표를 적용하지 않습니다.';}
   }));box.append(section);
  }
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
