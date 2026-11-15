#!/usr/bin/env python3
"""Actual read/write operations for the UTT historical B integrated workspace.

Commands affect work_* tables of the supplied SQLite DB. Historical B tables,
roles, sources and original approval evidence are preserved. Writes create an
immutable logical revision + event; no remote Dropbox/GitHub calls are made.
"""
from __future__ import annotations
import argparse, datetime, hashlib, json, sqlite3, uuid, re
from pathlib import Path

class Conflict(Exception):pass
class InputError(Exception):pass

def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def sha(x):return hashlib.sha256(x.encode('utf-8')).hexdigest()
def textjson(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def rid():return 'usr-'+uuid.uuid4().hex

def connect(db,readonly=False):
 uri=Path(db).resolve().as_uri()+'?mode='+('ro' if readonly else 'rw')
 c=sqlite3.connect(uri,uri=True,timeout=10)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA foreign_keys=ON')
 c.execute('PRAGMA busy_timeout=5000')
 if readonly:c.execute('PRAGMA query_only=ON')
 if not c.execute("SELECT 1 FROM sqlite_master WHERE name='work_records' AND type='table'").fetchone():
  raise InputError('Not a migrated UTT workspace database')
 return c

def get(c,record_id):
 r=c.execute('SELECT * FROM work_records WHERE record_id=?',(record_id,)).fetchone()
 if not r:raise InputError('Record not found')
 return dict(r)

def begin(c):c.execute('BEGIN IMMEDIATE')

def bump(c):
 n=int(c.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()[0])+1
 c.execute("UPDATE work_meta SET value=? WHERE key='data_revision'",(str(n),))
 return n

def event(c,kind,record,version,actor,reason,details,authorized='EXPLICIT_WORKSPACE_WRITE'):
 if actor not in ('USER','ASSISTANT','SYSTEM'):raise InputError('Invalid actor')
 if not reason or not reason.strip():raise InputError('A change reason is required')
 prev=c.execute('SELECT event_hash FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()
 prevhash=prev[0] if prev else None
 id='evt-'+uuid.uuid4().hex
 payload=dict(id=id,kind=kind,record_id=record,version=version,actor=actor,reason=reason,
              details=details,predecessor=prevhash,at=now())
 digest=sha(textjson(payload))
 c.execute('''INSERT INTO work_events(event_id,event_kind,record_id,record_version,actor,
  authorization_basis,reason,detail_json,occurred_at,preceding_hash,event_hash)
  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
  (id,kind,record,version,actor,authorized,reason,textjson(details),payload['at'],prevhash,digest))
 return id

def save_revision(c,r,eid):
 c.execute('''INSERT INTO work_revisions(record_id,version,title,body,body_sha256,summary,
  review_status,adoption_status,decision_status,archived_at,merged_into,event_id,created_at)
  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
  (r['record_id'],r['version'],r['title'],r['body'],r['body_sha256'],r['summary'],
   r['review_status'],r['adoption_status'],r['decision_status'],r['archived_at'],r['merged_into'],eid,now()))

def list_records(c,q=None,limit=20,include_archived=False,project_id=None):
 if not (1<=limit<=100):raise InputError('limit must be between 1 and 100')
 conditions=[];args=[]
 if not include_archived:conditions.append('r.archived_at IS NULL')
 if q is not None:
  conditions.append('(r.title LIKE ? OR r.body LIKE ?)');args += ['%'+q+'%']*2
 if project_id:
  conditions.append('EXISTS (SELECT 1 FROM work_record_projects p WHERE p.record_id=r.record_id AND p.project_id=? AND p.archived_at IS NULL)');args.append(project_id)
 where='WHERE '+' AND '.join(conditions) if conditions else ''
 return [dict(r) for r in c.execute('''SELECT r.record_id,r.title,r.summary,r.document_role,r.review_status,r.decision_status,
  r.version,r.archived_at,r.merged_into FROM work_records r '''+where+' ORDER BY r.record_id LIMIT ?',(*args,limit))]

def create(c,title,body='',summary=None,role='USER_NOTE',actor='USER',reason='New user workspace record'):
 if not title or not title.strip():raise InputError('title cannot be blank')
 if not isinstance(body,str):raise InputError('body must be text')
 begin(c)
 try:
  record_id=rid();t=now()
  c.execute('''INSERT INTO work_records(record_id,title,original_title,body,body_sha256,summary,
   document_role,original_layer,review_status,adoption_status,decision_status,source_kind,created_at,updated_at)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
   record_id,title.strip(),None,body,sha(body),summary,role,None,'PENDING_USER_REVIEW',
   'NEW_WORKSPACE_RECORD','UNREVIEWED','USER_WORKSPACE',t,t))
  r=get(c,record_id)
  eid=event(c,'CREATE',record_id,1,actor,reason,{'data_scope':'USER_WORKSPACE'})
  save_revision(c,r,eid);bump(c);c.commit()
  return {'record_id':record_id,'version':1,'event_id':eid}
 except Exception:c.rollback();raise

def edit(c,record_id,expected_version,changes,actor='USER',reason='Edit workspace record'):
 allowed={'title','body','summary','review_status'}
 if not changes or not set(changes)<=allowed:raise InputError('Invalid edit fields')
 begin(c)
 try:
  before=get(c,record_id)
  if before['version']!=expected_version:raise Conflict('VERSION_MISMATCH')
  after=dict(before)
  for k,v in changes.items():
   if k=='title' and (not isinstance(v,str) or not v.strip()):raise InputError('title cannot be blank')
   if k in ('body','title') and not isinstance(v,str):raise InputError('must be text')
   after[k]=v
  if after==before:raise InputError('No change')
  after['body_sha256']=sha(after['body']);after['version']+=1;after['updated_at']=now()
  c.execute('''UPDATE work_records SET title=?,body=?,body_sha256=?,summary=?,review_status=?,version=?,updated_at=?
   WHERE record_id=? AND version=?''',(
   after['title'],after['body'],after['body_sha256'],after['summary'],after['review_status'],
   after['version'],after['updated_at'],record_id,expected_version))
  eid=event(c,'EDIT',record_id,after['version'],actor,reason,{'fields':sorted(changes)})
  save_revision(c,after,eid);bump(c);c.commit()
  return {'record_id':record_id,'version':after['version'],'event_id':eid}
 except Exception:c.rollback();raise

def state_change(c,record_id,expected_version,kind,other=None,actor='USER',reason='Workspace state change'):
 if kind not in ('ARCHIVE','RESTORE','MERGE','UNMERGE'):raise InputError('Invalid state action')
 begin(c)
 try:
  b=get(c,record_id)
  if b['version']!=expected_version:raise Conflict('VERSION_MISMATCH')
  r=dict(b)
  if kind=='ARCHIVE':
   if r['archived_at']:raise InputError('Already archived')
   r['archived_at']=now()
  if kind=='RESTORE':
   if not r['archived_at'] or r['merged_into']:raise InputError('Record not restorable')
   r['archived_at']=None
  if kind=='MERGE':
   if not other or other==record_id:raise InputError('Valid target record is required')
   target=get(c,other)
   if target['archived_at'] or target['merged_into']:raise InputError('Target not active')
   if r['merged_into'] or r['archived_at']:raise InputError('Source not active')
   r['merged_into']=other;r['archived_at']=now()
  if kind=='UNMERGE':
   if not r['merged_into']:raise InputError('Not merged')
   r['merged_into']=None;r['archived_at']=None
  r['version']+=1;r['updated_at']=now()
  c.execute('''UPDATE work_records SET archived_at=?,merged_into=?,version=?,updated_at=? WHERE record_id=?''',
            (r['archived_at'],r['merged_into'],r['version'],r['updated_at'],record_id))
  eid=event(c,kind,record_id,r['version'],actor,reason,{'merged_target_present':other is not None})
  save_revision(c,r,eid);bump(c);c.commit()
  return {'record_id':record_id,'version':r['version'],'event_id':eid}
 except Exception:c.rollback();raise

def undo(c,record_id,expected_version,restore_version,actor='USER',reason='Restore older record revision'):
 begin(c)
 try:
  cur=get(c,record_id)
  if cur['version']!=expected_version:raise Conflict('VERSION_MISMATCH')
  old=c.execute('SELECT * FROM work_revisions WHERE record_id=? AND version=?',(record_id,restore_version)).fetchone()
  if not old:raise InputError('Historical revision not found')
  r=dict(cur)
  for k in ('title','body','body_sha256','summary','review_status','adoption_status','decision_status','archived_at','merged_into'):
   r[k]=old[k]
  r['version']=expected_version+1;r['updated_at']=now()
  c.execute('''UPDATE work_records SET title=?,body=?,body_sha256=?,summary=?,review_status=?,adoption_status=?,
   decision_status=?,archived_at=?,merged_into=?,version=?,updated_at=? WHERE record_id=?''',(
   r['title'],r['body'],r['body_sha256'],r['summary'],r['review_status'],r['adoption_status'],
   r['decision_status'],r['archived_at'],r['merged_into'],r['version'],r['updated_at'],record_id))
  eid=event(c,'RESTORE_REVISION',record_id,r['version'],actor,reason,{'restored_version':restore_version})
  save_revision(c,r,eid);bump(c);c.commit()
  return {'record_id':record_id,'version':r['version'],'event_id':eid}
 except Exception:c.rollback();raise

def attach_tag(c,record_id,tag,actor='USER',reason='Tag workspace record'):
 if not tag or not tag.strip():raise InputError('Tag must not be empty')
 begin(c)
 try:
  get(c,record_id)
  t=tag.strip();existing=c.execute('SELECT tag_id FROM work_tags WHERE name=? COLLATE NOCASE',(t,)).fetchone()
  tag_id=existing[0] if existing else 'tag-'+uuid.uuid4().hex
  if not existing:c.execute('INSERT INTO work_tags VALUES(?,?)',(tag_id,t))
  if c.execute('SELECT 1 FROM work_record_tags WHERE record_id=? AND tag_id=?',(record_id,tag_id)).fetchone():raise InputError('Already tagged')
  c.execute('INSERT INTO work_record_tags VALUES(?,?)',(record_id,tag_id))
  eid=event(c,'TAG',record_id,None,actor,reason,{'tag_id':tag_id});bump(c);c.commit()
  return {'tag_id':tag_id,'event_id':eid}
 except Exception:c.rollback();raise

def link_project(c,record_id,project_id,role='RELATED',actor='USER',reason='Associate record with project'):
 begin(c)
 try:
  get(c,record_id)
  if not c.execute('SELECT 1 FROM work_projects WHERE project_id=?',(project_id,)).fetchone():raise InputError('Unknown project')
  link_id='link-'+uuid.uuid4().hex
  basis='USER_CONFIRMED' if actor=='USER' else 'CANDIDATE'
  c.execute('INSERT INTO work_record_projects(link_id,record_id,project_id,role,scope,basis) VALUES(?,?,?,?,?,?)',
            (link_id,record_id,project_id,role,None,basis))
  eid=event(c,'PROJECT_LINK',record_id,None,actor,reason,{'project_id':project_id,'link_id':link_id,'basis':basis})
  bump(c);c.commit()
  return {'link_id':link_id,'basis':basis,'event_id':eid}
 except Exception:c.rollback();raise

def add_note(c,record_id,note,author='USER',reason='Add an authored note'):
 if not note or not note.strip():raise InputError('Note must not be empty')
 begin(c)
 try:
  get(c,record_id)
  note_id='note-'+uuid.uuid4().hex
  c.execute('INSERT INTO work_notes(note_id,record_id,author,note_text) VALUES(?,?,?,?)',
            (note_id,record_id,author,note))
  eid=event(c,'NOTE',record_id,None,author,reason,{'note_id':note_id})
  bump(c);c.commit()
  return {'note_id':note_id,'event_id':eid}
 except Exception:c.rollback();raise

def decide(c,record_id,decision,reason,actor='USER',evidence=None):
 if decision not in {'CONFIRMED','DEFERRED','REJECTED','RETRACTED','UNRESOLVED'}:raise InputError('Invalid decision')
 if actor!='USER':raise InputError('Only direct USER action may assert a confirmed decision; AI must use a proposal')
 begin(c)
 try:
  record=get(c,record_id)
  decision_id='decision-'+uuid.uuid4().hex
  c.execute('''INSERT INTO work_decisions(decision_id,record_id,decision,explanation,actor,user_authorized,evidence_message_ref)
  VALUES(?,?,?,?,?,?,?)''',(decision_id,record_id,decision,reason,actor,1,evidence))
  version=record['version']+1
  c.execute('UPDATE work_records SET decision_status=?,version=?,updated_at=? WHERE record_id=?',
            (decision,version,now(),record_id))
  r=get(c,record_id)
  eid=event(c,'DECISION',record_id,version,actor,reason,{'decision_id':decision_id,'decision':decision})
  save_revision(c,r,eid);bump(c);c.commit()
  return {'decision_id':decision_id,'version':version,'event_id':eid}
 except Exception:c.rollback();raise

def history(c,record_id):
 get(c,record_id)
 return [dict(r) for r in c.execute('''SELECT version,title,body_sha256,review_status,decision_status,
  archived_at,merged_into,event_id,created_at FROM work_revisions WHERE record_id=? ORDER BY version''',(record_id,))]

def verify(c):
 problems=[]
 dbcheck=c.execute('PRAGMA integrity_check').fetchone()[0]
 fk=c.execute('PRAGMA foreign_key_check').fetchall()
 if dbcheck!='ok':problems.append('SQLITE_INTEGRITY')
 if fk:problems.append('FOREIGN_KEY')
 hash_bad=0;missing_rev=0
 for r in c.execute('SELECT * FROM work_records'):
  if sha(r['body'])!=r['body_sha256']:hash_bad+=1
  row=c.execute('SELECT title,body_sha256 FROM work_revisions WHERE record_id=? AND version=?',
                (r['record_id'],r['version'])).fetchone()
  if not row or row['body_sha256']!=r['body_sha256'] or row['title']!=r['title']:
   missing_rev+=1
 if hash_bad:problems.append('BODY_HASH')
 if missing_rev:problems.append('LATEST_REVISION')
 total=c.execute('SELECT count(*) FROM work_records').fetchone()[0]
 fts=c.execute('SELECT count(*) FROM work_fts').fetchone()[0]
 if total!=fts:problems.append('FTS_COUNT')
 orphans=c.execute('SELECT count(*) FROM work_record_projects WHERE record_id NOT IN (SELECT record_id FROM work_records)').fetchone()[0]
 if orphans:problems.append('ORPHAN_PROJECT_LINK')
 # Event chain integrity: initial seed uses a declared fixed hash; subsequent events are canonical hashes.
 events=list(c.execute('SELECT * FROM work_events ORDER BY sequence'))
 prev=None;broken=0
 for i,e in enumerate(events):
  if e['preceding_hash']!=prev:broken+=1
  if i==0 and e['event_kind']=='IMPORT':
   expected=sha('initial-historical-b-import|UTT-INTEGRATED-WORKSPACE/0.1')
  else:
   p={'id':e['event_id'],'kind':e['event_kind'],'record_id':e['record_id'],'version':e['record_version'],
      'actor':e['actor'],'reason':e['reason'],'details':json.loads(e['detail_json']),
      'predecessor':e['preceding_hash'],'at':e['occurred_at']}
   expected=sha(textjson(p))
  if expected!=e['event_hash']:broken+=1
  prev=e['event_hash']
 if broken:problems.append('EVENT_CHAIN')
 return {'result':'PASS' if not problems else 'FAIL','failures':problems,'records':total,'record_fts':fts,
         'legacy_records':c.execute('SELECT count(*) FROM reorg_record').fetchone()[0],
         'projects':c.execute('SELECT count(*) FROM work_projects').fetchone()[0],
         'project_links':c.execute('SELECT count(*) FROM work_record_projects').fetchone()[0],
         'source_evidence':c.execute('SELECT count(*) FROM work_sources').fetchone()[0],
         'events':len(events),'hash_failures':hash_bad,'missing_revisions':missing_rev,
         'foreign_key_failures':len(fk),'event_chain_errors':broken}

