"""Verify immutable ZIP-derived input and working-tree changes independently.

No exclusion switch. Only an exact, documented development digest is accepted.
Filename boundary checks never read a potentially sensitive file's contents.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SHA256 = '8f3b19a4c17c250e7f618da2935226ad3aad64c2307401020728f98ad9930f4d'
ZIP_SHA256 = '30137cea3bd7117f1a0dfe4a34ec0807b381a28e99cf5a072ad6c41a73623e48'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(root, name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        return False
    return all(not (root/Path(*p.parts[:i])).is_symlink() for i in range(1,len(p.parts)+1)) and (root/name).is_file()


def verify_original(base):
    if base.is_symlink():
        return {'ok':False,'failures':[{'status':'UNSAFE_BASELINE_ROOT'}]}, None
    manifest_path = base/'HANDOFF_MANIFEST.json'
    if not regular(base,'HANDOFF_MANIFEST.json') or digest(manifest_path) != MANIFEST_SHA256:
        return {'ok':False,'failures':[{'path':'HANDOFF_MANIFEST.json','status':'INVALID_MANIFEST'}]}, None
    manifest = json.loads(manifest_path.read_text())
    entries = manifest['files']
    names = [item['path'] for item in entries]
    if len(set(names)) != len(names) or 'HANDOFF_MANIFEST.json' in names:
        return {'ok':False,'failures':[{'status':'INVALID_FILE_LIST'}]}, None
    failures=[]; verified=[]
    for item in entries:
        name=item['path'];path=base/name
        if not regular(base,name):failures.append({'path':name,'status':'MISSING_OR_UNSAFE'});continue
        if path.stat().st_size!=item['size_bytes'] or digest(path)!=item['sha256']:
            failures.append({'path':name,'status':'MISMATCH'});continue
        verified.append(name)
    actual={p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file() or p.is_symlink()}
    for name in sorted(actual-set(names)-{'HANDOFF_MANIFEST.json'}):
        failures.append({'path':name,'status':'UNEXPECTED_BASELINE_FILE'})
    return {'ok':not failures,'expected':len(entries),'verified':len(verified),'failures':failures},manifest


def verify_worktree(root,manifest,policy):
    rows=[]
    for item in manifest['files']:
        name=item['path'];path=root/name;status='missing';reason=None
        if regular(root,name):
            actual=digest(path)
            if actual==item['sha256'] and path.stat().st_size==item['size_bytes']:status='unchanged'
            else:
                expected=policy.get('intentional_changes',{}).get(name,{})
                if expected.get('sha256')==actual and expected.get('original_sha256')==item['sha256'] and expected.get('reason'):
                    status='intentional_change';reason=expected['reason']
                else:status='unexplained_change'
        rows.append({'path':name,'status':status,**({'reason':reason} if reason else {})})
    manifest_ok=regular(root,'HANDOFF_MANIFEST.json') and digest(root/'HANDOFF_MANIFEST.json')==MANIFEST_SHA256
    counts=dict(Counter(r['status'] for r in rows))
    return {'ok':manifest_ok and not any(r['status'] in ('missing','unexplained_change') for r in rows),
            'manifest_unchanged':manifest_ok,'counts':counts,'files':rows}


def forbidden_filename(name):
    parts=PurePosixPath(name).parts;leaf=parts[-1].lower() if parts else ''
    if leaf=='.env' or (leaf.startswith('.env.') and leaf!='.env.example'):return True
    if any(p.lower() in {'cache','backups','exports','local-data','node_modules','.venv','artifacts','__pycache__'} for p in parts):return True
    return any(leaf.endswith(ext) for ext in ('.sqlite','.sqlite3','.db','.zip','.tar','.gz','.bak','.log','.pem','.key')) or '.db-' in leaf


def git_names(root,args):
    result=subprocess.run(['git','-C',str(root),'ls-files','-z',*args],check=True,capture_output=True)
    return [name for name in result.stdout.decode().split('\0') if name]


def audit(root=ROOT):
    baseline,manifest=verify_original(root/'provenance/handoff-v1')
    if manifest is None:return {'ok':False,'original':baseline}
    policy=json.loads((root/'docs/HANDOFF_WORKTREE_POLICY.json').read_text())
    working=verify_worktree(root,manifest,policy)
    tracked=git_names(root,[]);untracked=git_names(root,['--others','--exclude-standard'])
    boundaries=[{'path':n,'status':'FORBIDDEN_TRACKED_FILENAME'} for n in tracked if forbidden_filename(n)]
    original_names={e['path'] for e in manifest['files']}|{'HANDOFF_MANIFEST.json'}
    additions=[]
    for name in sorted(set(tracked+untracked)-original_names):
        known=(name in policy['development_files'] or any(name.startswith(p) for p in policy['development_prefixes']))
        additions.append({'path':name,'status':'intentional_addition' if known else 'unexplained_addition'})
    unknown=any(r['status']=='unexplained_addition' for r in additions)
    return {'ok':baseline['ok'] and working['ok'] and not boundaries and not unknown,
            'source_zip_sha256':ZIP_SHA256,'original':baseline,'working_tree':working,
            'development_additions':additions,'tracked_filename_check':{'checked':len(tracked),'failures':boundaries}}


def main():
    result=audit();print(json.dumps(result,ensure_ascii=False,indent=2));return not result['ok']

if __name__=='__main__':raise SystemExit(main())
