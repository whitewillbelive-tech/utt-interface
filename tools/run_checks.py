"""Run the supported checks with explicit active test roots and per-run evidence.

Never discover tests from the repository root/provenance. All requests and
fixtures produced by these commands are synthetic. Requires local socket access
for Chromium. A missing tool or failed command makes the run fail.
"""
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
import json
from pathlib import Path
import platform
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]


def output(command):
    try:return subprocess.run(command,cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    except (OSError,subprocess.CalledProcessError):return 'unavailable'


def main():
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    directory=ROOT/'artifacts'/run_id;directory.mkdir(parents=True)
    versions={'python':platform.python_version(),'node':output(['node','--version']),'chromium':output(['chromium','--version'])}
    for name in ('playwright','jsonschema'):
        try:versions[name]=version(name)
        except PackageNotFoundError:versions[name]='unavailable'
    commands=[
        [sys.executable,'tools/verify_handoff.py'],
        [sys.executable,'fixtures/synthetic/generate.py'],
        [sys.executable,'-m','unittest','discover','-s','tests','-p','test*.py','-v'],
        *[['node','--check',str(p.relative_to(ROOT))] for p in sorted((ROOT/'app/web').glob('*.mjs'))],
        ['node','--test','--test-isolation=none',*[str(p.relative_to(ROOT)) for p in sorted((ROOT/'source/tests').glob('*.test.mjs'))]],
        [sys.executable,'tools/validate_schema.py'],
        [sys.executable,'tools/browser_check.py'],
        ['git','diff','--check'],
    ]
    report={'run_id':run_id,'commit':output(['git','rev-parse','HEAD']),
            'working_tree_dirty':bool(output(['git','status','--porcelain'])),
            'versions':versions,'kind':'local, not CI','steps':[],'status':'running'}
    target=directory/'results.json'
    def save():target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save()
    for number,command in enumerate(commands,1):
        log=directory/f'{number:02d}.log'
        try:
            result=subprocess.run(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            log.write_text(result.stdout);code=result.returncode
        except OSError as exc:
            log.write_text(type(exc).__name__+': command unavailable\n');code=127
        report['steps'].append({'command':command,'exit_code':code,'status':'passed' if code==0 else 'failed','log':str(log.relative_to(ROOT))})
        save();print(f'{number:02d}: {"PASS" if code==0 else "FAIL"} '+ ' '.join(command),flush=True)
    report['status']='passed' if all(s['exit_code']==0 for s in report['steps']) else 'failed'
    report['finished_at_utc']=datetime.now(timezone.utc).isoformat();save()
    (ROOT/'artifacts/latest-validation.json').write_text(json.dumps({'run_id':run_id,'results':str(target.relative_to(ROOT))},indent=2)+'\n')
    print(f'Results: {target}',flush=True)
    return report['status']!='passed'

if __name__=='__main__':raise SystemExit(main())
