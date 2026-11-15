# Native B-2 publisher source — source-only handoff

The management conversation reopened `UNIFIED_ThinkTank_0.4.1_GateB2_Uploader_ApprovalPending_20261026.zip` and extracted the source below. This is source text, NOT a replacement application and NOT a report of a live publication.

Excluded: candidate payloads, SQLite databases, source documents, `web/config.json`, `web/approved-catalog.json`, production `pinned.mjs`, tokens, actual generation values and user paths. The relative names inside the code are protocol names, not an account-specific destination. Existing reader-only OAuth in the current repository remains separate from the publisher's three-scope grant.

The original B-2 is fixed to one historical catalog and uses single-request uploads. Reuse its sequencing and conflict semantics; do NOT run it unchanged against the new database. Replace fixed catalog values with a runtime-verified publication plan; use streaming upload sessions for large files. The source's report contains per-file paths and hashes: keep such reports local; shared reports must contain only non-sensitive status/aggregate values.

Original file SHA-256 values (source text only):

| Original member | SHA-256 |
| --- | --- |
| web/lib/uploader.mjs | cf40bc2685209623f7c9fddaa739423c64b3e47f3b339182264f12d19f247407 |
| web/lib/candidate.mjs | 170c3bd4606755461d4e8ac2050c10ed5c10bde0e944cdd555ff5f8474868b84 |
| web/lib/transport.mjs | 549400c3675e02c2dcfc898aa1aba1d612776b746734f1fe27b723ffaec6d85c |
| web/lib/security.mjs | 3341e6f82c411c0c51cc1fe313d606ffb6babf5f11cc5d6c44615b87afe5489e |

## web/lib/uploader.mjs

```javascript
import {SafeError,insist,sha256} from './security.mjs';
import {assertApproval,destination,activationBytes,assertReplacement} from './candidate.mjs';
import {MANIFEST_HASH,ROOT,ACTIVE} from './pinned.mjs';
import {diagnostic} from './transport.mjs';
const COMMIT='system/COMMIT.json';
export class Uploader{
 #candidate;#transport;#approval;#running=false;#stop=false;#onProgress;#records=new Map();#pointer;#finalVerified=false;
 constructor(candidate,transport,approval,{onProgress=()=>{}}={}){assertApproval(approval,candidate);this.#candidate=candidate;this.#transport=transport;this.#approval=approval;this.#onProgress=onProgress;this.localVerified=false;this.fullChecked=0;this.stage='ready';this.committed=false;this.active=false;this.errors=[];this.recoveries=[];}
 stop(){this.#stop=true;}
 #checkpoint(){this.#onProgress(this.report());}
 #boundary(){if(this.#stop)throw new SafeError('PAUSED_BY_USER');}
 async #same(remote,e){insist(remote&&remote.bytes.length===e.bytes&&await sha256(remote.bytes)===e.sha256,'REMOTE_CONTENT_CONFLICT');return remote;}
 #mark(e,status){this.#records.set(e.path,{path:e.path,bytes:e.bytes,expected_sha256:e.sha256,verified_sha256:e.sha256,status});this.#checkpoint();}
 async #ensure(e,committed){
  const path=destination(e.path);this.current=e.path;const prior=await this.#transport.download(path);
  if(prior){await this.#same(prior,e);this.#mark(e,'existing_verified');return;}
  insist(!committed,'COMMITTED_GENERATION_DAMAGED');const bytes=await this.#candidate.read(e.path);
  try{await this.#transport.upload(path,bytes);}catch(error){
   // Never replay a write. Exact read determines whether this attempt committed.
   const failure=diagnostic(error,this.stage);if([400,401,403,429].includes(error?.status)||failure.error_code==='INSUFFICIENT_SPACE')throw error;let recovered;
   try{recovered=await this.#transport.download(path);}catch{throw Object.assign(new SafeError('WRITE_OUTCOME_UNKNOWN'),{safeContext:failure});}
   if(recovered){await this.#same(recovered,e);this.recoveries.push({path:e.path,kind:'exact_read_after_write_error',cause:failure});this.#mark(e,'recovered_verified');return;}
   throw error;
  }
  const remote=await this.#transport.download(path);insist(remote,'POST_UPLOAD_MISSING');await this.#same(remote,e);this.#mark(e,'uploaded_verified');
 }
 async run(){
  insist(!this.#running,'ALREADY_RUNNING');assertApproval(this.#approval,this.#candidate);this.#running=true;this.#stop=false;this.errors=[];this.active=false;this.committed=false;this.#finalVerified=false;this.#records.clear();this.localVerified=false;this.fullChecked=0;
  try{
   this.stage='local_recheck';this.#checkpoint();for(const e of this.#candidate.catalog.files){this.#boundary();this.current=e.path;await this.#candidate.read(e.path);} // Rehash before any remote request.
   this.localVerified=true;const commit=this.#candidate.catalog.files.find(e=>e.path===COMMIT);const previousCommit=await this.#transport.download(destination(COMMIT));
   if(previousCommit)await this.#same(previousCommit,commit);
   this.stage='upload_and_verify';this.#checkpoint();
   for(const e of this.#candidate.catalog.files.filter(e=>e.path!==COMMIT)){this.#boundary();await this.#ensure(e,!!previousCommit);}
   this.stage='full_remote_verification';this.#checkpoint();
   for(const e of this.#candidate.catalog.files.filter(e=>e.path!==COMMIT)){this.#boundary();this.current=e.path;await this.#same(await this.#transport.download(destination(e.path)),e);this.fullChecked++;this.#mark(e,'final_verified');}
   this.#finalVerified=true;this.#boundary();this.stage='commit';this.#checkpoint();await this.#ensure(commit,false);this.committed=true;
   this.#boundary();this.stage='activation';this.current='active-reference';this.#checkpoint();await this.#activate();this.stage='complete';
  }catch(e){this.errors.push({...diagnostic(e,this.stage),path:this.current||null,prior_error:e.safeContext||null});this.stage=e.code==='PAUSED_BY_USER'?'paused':e.code==='ACTIVE_REFERENCE_EXISTS'?'activation_approval_required':'stopped';}
  finally{this.#running=false;this.#checkpoint();}return this.report();
 }
 async #activate(replacement){
  insist(this.committed&&this.#finalVerified,'GENERATION_NOT_VERIFIED');const desired=activationBytes(this.#candidate),hash=await sha256(desired);const prior=await this.#transport.download(ACTIVE);let rev;
  if(prior){
   if(prior.bytes.length===desired.length&&await sha256(prior.bytes)===hash){this.active=true;return;}
   // Preserve arbitrary/unrecognized pointers without editing them.
   let parsed;try{parsed=JSON.parse(new TextDecoder().decode(prior.bytes));}catch{throw new SafeError('ACTIVE_REFERENCE_INVALID');}
   insist(parsed?.format==='utt-active-baseline/1'&&/^[0-9a-f]{64}$/.test(parsed.generation_id)&&parsed.manifest_sha256===parsed.generation_id&&parsed.root==='/snapshots/baselines/'+parsed.generation_id+'/','ACTIVE_REFERENCE_INVALID');
   const previousHash=await sha256(prior.bytes);
   if(!replacement){this.#pointer={rev:prior.rev,previousHash,generation_id:parsed.generation_id};throw new SafeError('ACTIVE_REFERENCE_EXISTS');}
   assertReplacement(replacement,this.#candidate,prior.rev,previousHash);rev=prior.rev;
   // Only safe schema fields are kept for rollback assistance; original pointer is never logged.
   this.previousActivation={generation_id:parsed.generation_id,manifest_sha256:parsed.manifest_sha256,root:parsed.root,previous_sha256:previousHash};
  }else insist(!replacement,'ACTIVE_CHANGED_REAPPROVE');
  try{await this.#transport.upload(ACTIVE,desired,{rev,replacement,previousHash:prior?await sha256(prior.bytes):undefined});}
  catch(e){const d=await this.#transport.download(ACTIVE);if(d&&d.bytes.length===desired.length&&await sha256(d.bytes)===hash){this.active=true;this.recoveries.push({path:'active-reference',kind:'exact_read_after_activation_error'});return;}throw e;}
  const actual=await this.#transport.download(ACTIVE);insist(actual&&actual.bytes.length===desired.length&&await sha256(actual.bytes)===hash,'ACTIVATION_NOT_VERIFIED');this.active=true;
 }
 replacementInfo(){return this.#pointer?{...this.#pointer}:null;}
 async replaceActive(ticket){
  insist(!this.#running&&this.stage==='activation_approval_required','ACTIVATION_NOT_READY');this.#running=true;this.#stop=false;
  try{
   // The user may have waited: re-read the entire immutable generation before switching.
   this.stage='activation_reverification';for(const e of this.#candidate.catalog.files){this.#boundary();await this.#same(await this.#transport.download(destination(e.path)),e);}await this.#activate(ticket);this.stage='complete';this.errors=[];
  }catch(e){this.errors.push(diagnostic(e,this.stage));this.stage='stopped';}finally{this.#running=false;this.#checkpoint();}return this.report();
 }
 report(){const records=[...this.#records.values()];return {format:'utt-b2-report/1',manifest_sha256:MANIFEST_HASH,target:ROOT+'/',stage:this.stage,local_verified:this.localVerified,full_remote_verified:this.#finalVerified,full_checked_files:this.fullChecked,ok:this.active&&this.committed,committed:this.committed,active:this.active,total_files:this.#candidate.catalog.count,total_bytes:this.#candidate.catalog.bytes,verified_files:records.length,remaining_files:this.#candidate.catalog.count-records.length,verified_bytes:records.reduce((n,e)=>n+e.bytes,0),sent_bytes:this.#transport.sentBytes||0,api_requests:this.#transport.requests||0,error_count:this.errors.length,error_file_count:new Set(this.errors.map(e=>e.path).filter(p=>this.#candidate.catalog.files.some(e=>e.path===p))).size,resumable:!this.active,files:records,errors:this.errors.map(e=>({...e})),recoveries:this.recoveries.map(e=>({...e})),previous_activation:this.previousActivation||null,contains_secrets:false};}
}
```

**Transcription note:** in the `report()` line above, the two original expressions are `errors:structuredClone(this.errors),recoveries:structuredClone(this.recoveries)`. Use the original expressions when extracting executable code. The originals' SHA values above refer to the original expressions, not the displayed shallow-copy transcription. No other adaptation is intended in this reference.

## web/lib/candidate.mjs

```javascript
import {insist,sha256,canonical} from './security.mjs';
import {MANIFEST_HASH,CATALOG_HASH,COUNT,TOTAL_BYTES,ROOT,ACTIVE} from './pinned.mjs';
const verified=new WeakSet(),catalogs=new WeakSet(),approval=new WeakMap(),replacements=new WeakMap();
export function relativePath(p){insist(typeof p==='string'&&/^[A-Za-z0-9_./-]+$/.test(p)&&!p.startsWith('/')&&!p.includes('\\')&&!p.split('/').some(s=>!s||s==='.'||s==='..')&&!/scrivener/i.test(p),'PATH_INVALID');return p;}
export async function loadCatalog(bytes){
 insist(await sha256(bytes)===CATALOG_HASH,'CATALOG_HASH_MISMATCH');const c=JSON.parse(new TextDecoder().decode(bytes));
 insist(c.count===COUNT&&c.bytes===TOTAL_BYTES&&c.manifest_sha256===MANIFEST_HASH,'CATALOG_MISMATCH');
 const paths=new Set();for(const e of c.files){relativePath(e.path);insist(!paths.has(e.path.toLowerCase()),'DUPLICATE_PATH');paths.add(e.path.toLowerCase());Object.freeze(e);}
 insist(paths.size===COUNT&&c.files.reduce((n,e)=>n+e.bytes,0)===TOTAL_BYTES,'CATALOG_MISMATCH');Object.freeze(c.files);Object.freeze(c);catalogs.add(c);return c;
}
export async function inspectCandidate(c,files,onProgress=()=>{}){
 insist(catalogs.has(c),'CATALOG_NOT_VERIFIED');const map=new Map();let selectedRoot;
 for(const f of files){const raw=f.webkitRelativePath||f.relativePath;insist(typeof raw==='string','FOLDER_SELECTION_REQUIRED');const i=raw.indexOf('/');insist(i>0,'FOLDER_SELECTION_REQUIRED');selectedRoot??=raw.slice(0,i);insist(raw.slice(0,i)===selectedRoot,'MULTIPLE_ROOTS');const p=relativePath(raw.slice(i+1));insist(!map.has(p),'DUPLICATE_PATH');map.set(p,f);}
 insist(map.size===COUNT,'FILE_COUNT_MISMATCH');let n=0,bytes=0;
 for(const e of c.files){const f=map.get(e.path);insist(f&&f.size===e.bytes,'LOCAL_SIZE_MISMATCH');insist(await sha256(await f.arrayBuffer())===e.sha256,'LOCAL_HASH_MISMATCH');bytes+=f.size;onProgress(++n,bytes);}
 insist(bytes===TOTAL_BYTES,'TOTAL_SIZE_MISMATCH');
 const manifest=JSON.parse(await map.get('system/manifest.json').text());const commit=JSON.parse(await map.get('system/COMMIT.json').text());
 insist(commit.manifest_sha256===MANIFEST_HASH&&commit.state==='complete','COMMIT_INVALID');
 insist(manifest.files.length===COUNT-2,'MANIFEST_COUNT_MISMATCH');
 for(const e of manifest.files){const approved=c.files.find(a=>a.path===e.path);insist(approved&&e.bytes===approved.bytes&&e.sha256===approved.sha256,'MANIFEST_ENTRY_MISMATCH');}
 const handle=Object.freeze({catalog:c,root:ROOT,commit:Object.freeze(commit),read:async p=>{const entry=c.files.find(e=>e.path===p);insist(entry,'FILE_NOT_ALLOWED');const data=new Uint8Array(await map.get(p).arrayBuffer());insist(data.length===entry.bytes&&await sha256(data)===entry.sha256,'LOCAL_CHANGED');return data;}});verified.add(handle);return handle;
}
export function assertCandidate(handle){insist(verified.has(handle),'CANDIDATE_NOT_VERIFIED');}
export function approveUpload(candidate,{boundaryConfirmed,spaceConfirmed,explicitConsent}={}){
 assertCandidate(candidate);insist(boundaryConfirmed===true&&spaceConfirmed===true&&explicitConsent===true,'UPLOAD_APPROVAL_REQUIRED');const t=Object.freeze({});approval.set(t,{candidate,kind:'initial_upload'});return t;
}
export function assertApproval(token,candidate){assertCandidate(candidate);insist(approval.get(token)?.candidate===candidate&&approval.get(token)?.kind==='initial_upload','UPLOAD_APPROVAL_REQUIRED');}
export function destination(p){return ROOT+'/'+relativePath(p);}
export function allowedRemote(candidate,path){return path===ACTIVE||candidate.catalog.files.some(e=>destination(e.path).toLowerCase()===path.toLowerCase());}

export function activationBytes(candidate){assertCandidate(candidate);return new TextEncoder().encode(canonical({format:'utt-active-baseline/1',generation_id:MANIFEST_HASH,manifest_sha256:MANIFEST_HASH,baseline_id:candidate.commit.baseline_id,root:ROOT+'/',commit_sha256:candidate.catalog.files.find(e=>e.path==='system/COMMIT.json').sha256,files:COUNT,bytes:TOTAL_BYTES})+'\n');}
export function approveReplacement(candidate,{rev,previousHash,explicitConsent}={}){assertCandidate(candidate);insist(explicitConsent===true&&typeof rev==='string'&&rev.length>0&&/^[0-9a-f]{64}$/.test(previousHash),'REPLACEMENT_APPROVAL_REQUIRED');const t=Object.freeze({});replacements.set(t,{candidate,rev,previousHash});return t;}
export function assertReplacement(t,candidate,rev,hash){const r=replacements.get(t);insist(r&&r.candidate===candidate&&r.rev===rev&&r.previousHash===hash,'REPLACEMENT_APPROVAL_REQUIRED');}
```

## web/lib/transport.mjs

```javascript
import {ACTIVE} from './pinned.mjs';
import {insist,SafeError,validateConfig,exactScopes,sha256} from './security.mjs';
import {authorization,forget} from './oauth.mjs';
import {assertApproval,allowedRemote,activationBytes,assertReplacement,destination} from './candidate.mjs';
const endpoints={filesDownload:'https://content.dropboxapi.com/2/files/download',filesUpload:'https://content.dropboxapi.com/2/files/upload'};
export function tags(e){let o=e?.error?.error;const a=[];for(let i=0;i<5&&o&&typeof o==='object';i++){const t=o['.tag'];if(!['path','path_lookup','path_write','not_found','conflict','file','folder','insufficient_space','too_many_write_operations','no_write_permission','disallowed_name','restricted_content'].includes(t))break;a.push(t);o=o[t];}return a;}
export function missing(e){return e?.status===409&&['path/not_found','path_lookup/not_found'].includes(tags(e).join('/'));}
export function diagnostic(e,stage){
 const status=Number.isInteger(e?.status)?e.status:Number.isInteger(e?.safeTransport?.http_status)?e.safeTransport.http_status:null;
 const code=e instanceof SafeError?e.code:e?.name==='TimeoutError'?'TIMEOUT':e?.name==='AbortError'?'REQUEST_ABORTED':status!==null&&['SyntaxError','TypeError'].includes(e?.name)?'SDK_RESPONSE_INVALID':status===401?'REAUTH_REQUIRED':status===403?'PERMISSION_DENIED':status===429?'RATE_LIMITED':tags(e).includes('insufficient_space')?'INSUFFICIENT_SPACE':status===409?'REMOTE_CONFLICT':status>=500?'SERVER_ERROR':status?'HTTP_ERROR':e?.name==='TimeoutError'?'TIMEOUT':e?.name==='AbortError'?'REQUEST_ABORTED':'NETWORK_OR_CORS_UNDETERMINED';
 let retry=null;try{const raw=e?.headers?.get('Retry-After');if(raw){const n=/^\d+$/.test(raw)?Number(raw):Math.ceil((Date.parse(raw)-Date.now())/1000);if(Number.isFinite(n))retry=Math.max(0,Math.min(86400,n));}}catch{}
 return {stage,api:Object.keys(endpoints).includes(e?.safeTransport?.api)?e.safeTransport.api:null,attempts:e?.safeTransport?.attempts??0,duration_ms:e?.safeTransport?.duration_ms??null,error_code:code,http_response_received:status!==null,http_status:status,exception_name:['TypeError','TimeoutError','AbortError','SyntaxError'].includes(e?.name)?e.name:null,dropbox_error_tags:tags(e),retry_after_seconds:retry,contains_secrets:false};
}
export class SDKTransport{
 #sdk;#grant;#candidate;#approval;#active;#fetch;#abort;#last;
 constructor(config,grant,candidate,consent,{origin,Dropbox=globalThis.Dropbox?.Dropbox,fetcher=globalThis.fetch}={}){
  validateConfig(config,origin);exactScopes(grant?.scopes);insist(grant.app_key===config.app_key,'GRANT_APP_MISMATCH');assertApproval(consent,candidate);insist(typeof Dropbox==='function','SDK_NOT_LOADED');
  this.#grant=grant;this.#candidate=candidate;this.#approval=consent;this.#fetch=fetcher;this.requests=0;this.sentBytes=0;
  this.#sdk=new Dropbox({accessToken:authorization(grant).slice(7),fetch:(url,options)=>this.#guard(url,options)});
 }
 async #guard(url,options){
  assertApproval(this.#approval,this.#candidate);authorization(this.#grant);const a=this.#active;insist(a&&url===endpoints[a.method]&&!new URL(url).search,'ENDPOINT_BLOCKED');
  const wire=JSON.parse(options.headers['Dropbox-API-Arg']);insist(wire.path===a.args.path&&allowedRemote(this.#candidate,wire.path),'PATH_BLOCKED');
  if(a.method==='filesUpload'){insist(JSON.stringify(wire.mode)===JSON.stringify(a.args.mode),'MODE_MISMATCH');insist(wire.autorename===false&&wire.strict_conflict===true&&['add','update'].includes(wire.mode['.tag']),'UNSAFE_WRITE_MODE');insist(options.body.byteLength<=150*1024*1024,'SINGLE_UPLOAD_LIMIT');this.sentBytes+=options.body.byteLength;}
  this.requests++;const start=performance.now();this.#last={api:a.method,http_status:null,duration_ms:0,attempts:1};
  try{const r=await this.#fetch(url,{...options,mode:'cors',credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:this.#abort});this.#last.http_status=r.status;return r;}finally{this.#last.duration_ms=Math.round(performance.now()-start);}
 }
 async #call(method,args){
  insist(!this.#active,'CONCURRENT_REQUEST_BLOCKED');assertApproval(this.#approval,this.#candidate);authorization(this.#grant);insist(allowedRemote(this.#candidate,args.path),'PATH_BLOCKED');
  this.#active={method,args};this.#last={api:method,http_status:null,duration_ms:0,attempts:0};const started=performance.now();this.#abort=AbortSignal.timeout(method==='filesUpload'?300000:180000);
  try{return await this.#sdk[method](args);}catch(e){if(e&&typeof e==='object')e.safeTransport={...this.#last,duration_ms:Math.round(performance.now()-started)};if(e?.status===401)forget(this.#grant);throw e;}finally{this.#active=undefined;}
 }
 async download(path){try{const r=await this.#call('filesDownload',{path});const b=r.result.fileBlob?new Uint8Array(await r.result.fileBlob.arrayBuffer()):new Uint8Array(r.result.fileBinary);insist(r.result.path_lower===path.toLowerCase()&&r.result.size===b.length&&typeof r.result.rev==='string','REMOTE_METADATA_MISMATCH');return {bytes:b,rev:r.result.rev};}catch(e){if(missing(e))return null;if(e&&typeof e==='object'&&!e.safeTransport)e.safeTransport={...this.#last};throw e;}}
 async upload(path,bytes,{rev,replacement,previousHash}={}){
  if(path===ACTIVE){insist(await sha256(bytes)===await sha256(activationBytes(this.#candidate)),'ACTIVE_PAYLOAD_BLOCKED');if(rev)assertReplacement(replacement,this.#candidate,rev,previousHash);}
  else{insist(!rev,'IMMUTABLE_WRITE_REQUIRED');const e=this.#candidate.catalog.files.find(e=>destination(e.path)===path);insist(e&&bytes.length===e.bytes&&await sha256(bytes)===e.sha256,'UPLOAD_PAYLOAD_BLOCKED');}
  const mode=rev?{'.tag':'update',update:rev}:{'.tag':'add'};return this.#call('filesUpload',{path,contents:bytes,mode,autorename:false,strict_conflict:true,mute:true});}
}
```

## web/lib/security.mjs

```javascript
export const SCOPES=Object.freeze(['files.metadata.read','files.content.read','files.content.write']);
export const ROOTS=Object.freeze(['ledger','snapshots','sources','manuscripts','reviews','inbox','exports','backups','system']);
export class SafeError extends Error { constructor(code){super(code);this.name='SafeError';this.code=code;} }
export function insist(ok,code){if(!ok)throw new SafeError(code);}
export function exactScopes(scopes){
  const xs=typeof scopes==='string'?scopes.trim().split(/\s+/):scopes;
  insist(Array.isArray(xs)&&xs.length===SCOPES.length&&new Set(xs).size===SCOPES.length&&SCOPES.every(s=>xs.includes(s)),'SCOPE_MISMATCH');
  return [...SCOPES];
}
export function validateConfig(c,origin){
  insist(c?.gate==='A'&&c.live_enabled===true,'LIVE_DISABLED');
  insist(c.access_type==='app_folder'&&c.app_name==='UnifiedThinkTank','APP_FOLDER_REQUIRED');
  insist(/^[A-Za-z0-9]{8,80}$/.test(c.app_key||''),'APP_KEY_REQUIRED');
  insist(c.boundary_review?.verified===true&&c.boundary_review?.method==='developer_console'
    && typeof c.boundary_review?.reviewed_by==='string'&&c.boundary_review.reviewed_by.length>1
    && /^\d{4}-\d{2}-\d{2}T/.test(c.boundary_review?.reviewed_at||'')
    && c.boundary_review.app_key===c.app_key,'CONSOLE_EVIDENCE_REQUIRED');
  exactScopes(c.scopes);
  let u;try{u=new URL(c.redirect_uri);}catch{throw new SafeError('REDIRECT_INVALID');}
  insist((u.protocol==='https:'||(u.protocol==='http:'&&['localhost','127.0.0.1'].includes(u.hostname)))
    &&u.origin===origin&&!u.username&&!u.password&&!u.search&&!u.hash
    &&u.pathname.endsWith('/index.html'),'REDIRECT_INVALID');
  return structuredClone(c);
}
export function appPath(value,{directory=false}={}){
  insist(typeof value==='string'&&value.length<500&&value.startsWith('/')&&!value.endsWith('/'),'PATH_INVALID');
  insist(!/scrivener/i.test(value)&&!/[\\%:#?\x00-\x20\x7f-\uffff]/.test(value),'PATH_INVALID');
  const parts=value.slice(1).split('/');
  insist(ROOTS.includes(parts[0])&&parts.every(p=>p&&p!=='.'&&p!=='..'&&/^[a-z0-9._-]+$/.test(p)),'PATH_INVALID');
  insist(directory||parts.length>1,'PATH_INVALID');
  return value;
}
export function probePath(value){
  appPath(value);
  insist(/^\/system\/gate-a\/probe-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$/.test(value),'PROBE_PATH_REQUIRED');
  return value;
}
export function canonical(value){
  if(value===null||typeof value==='boolean'||typeof value==='string')return JSON.stringify(value);
  if(typeof value==='number'){insist(Number.isSafeInteger(value),'INTEGER_REQUIRED');return String(value);}
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  insist(value&&Object.getPrototypeOf(value)===Object.prototype,'JSON_OBJECT_REQUIRED');
  return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
}
export async function sha256(bytes){
  if(typeof bytes==='string')bytes=new TextEncoder().encode(bytes);
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');
}
export function b64url(bytes){return btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');}
export function randomCode(n=32){return b64url(crypto.getRandomValues(new Uint8Array(n)));}
```

## Dependency decisions

- `pinned.mjs` is deliberately not copied because it contains the historical publication pins. Its exports are `MANIFEST_HASH`, `CATALOG_HASH`, `COUNT`, `TOTAL_BYTES`, `ROOT`, `ACTIVE`. For the new publisher these values must come from a validated local publication plan, not invented constants or the old catalog.
- B-2's browser OAuth was tied to `index.html`. Reuse the repository's tested Windows PKCE callback mechanics, with a separate publisher grant for the three scopes above. Do not turn reader grants into writer grants or change the reader's two-scope tests.
- Config and approved-catalog contents are local inputs; their absence from this repository is intentional. Their native field structures and binding rules are provided in `NATIVE_FORMAT_AND_MIGRATION.md` in this directory.
- Original source uses in-memory whole-file SHA and a single-upload bound. The new publisher must stream large files and use a consistent SQLite export; the reference is not production-ready for the new workspace DB.
