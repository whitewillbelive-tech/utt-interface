"""Check the original handoff without silently accepting missing or edited files."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exclude',action='append',default=[],help='Explicitly documented original input exclusion')
    args=parser.parse_args()
    manifest=json.loads((ROOT/'HANDOFF_MANIFEST.json').read_text())
    failures=[]; checked=0; excluded=[]
    for item in manifest['files']:
        name=item['path']
        if name in args.exclude:
            excluded.append(name);continue
        p=ROOT/name
        if not p.is_file():failures.append({'path':name,'status':'MISSING'});continue
        b=p.read_bytes()
        if len(b)!=item['size_bytes'] or hashlib.sha256(b).hexdigest()!=item['sha256']:
            failures.append({'path':name,'status':'MISMATCH'});continue
        checked+=1
    print(json.dumps({'verified':checked,'excluded':excluded,'failures':failures},ensure_ascii=False,indent=2))
    return bool(failures)

if __name__=='__main__':raise SystemExit(main())
