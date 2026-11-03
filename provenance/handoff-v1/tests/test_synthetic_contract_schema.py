"""Validate synthetic examples with the subset of JSON Schema used by this draft.

This tiny test helper is not a general JSON Schema implementation. It fails
closed on unrecognised keywords. For independent/full validation use a compliant
Draft 2020-12 validator when available. It does not inspect real data.
"""
import copy
import json
import re
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCHEMA=json.loads((ROOT/'contracts'/'utt-read-api.schema.json').read_text(encoding='utf-8'))
EXAMPLES=json.loads((ROOT/'fixtures'/'synthetic'/'contract_examples.json').read_text(encoding='utf-8'))
KNOWN={'$ref','$defs','$schema','$id','title','description','type','const','enum','required','properties',
       'additionalProperties','items','oneOf','allOf','if','then','else','minimum','maximum','minLength','minItems','pattern'}

def matches(schema,value):
    try:
        check(schema,value)
        return True
    except AssertionError:
        return False

def check(s,v):
    assert not (set(s)-KNOWN), 'unsupported keywords: '+str(set(s)-KNOWN)
    if '$ref' in s:
        prefix='#/$defs/'
        assert s['$ref'].startswith(prefix)
        check(SCHEMA['$defs'][s['$ref'][len(prefix):]],v)
    if 'type' in s:
        types=s['type'] if isinstance(s['type'],list) else [s['type']]
        def typed(t):
            return {'object':lambda:isinstance(v,dict), 'array':lambda:isinstance(v,list),
                    'string':lambda:isinstance(v,str), 'integer':lambda:type(v) is int,
                    'boolean':lambda:type(v) is bool, 'null':lambda:v is None}[t]()
        assert any(typed(t) for t in types), ('type',types,v)
    if 'const' in s: assert type(v) is type(s['const']) and v==s['const'], ('const',v)
    if 'enum' in s: assert v in s['enum'], ('enum',v)
    if isinstance(v,dict):
        for key in s.get('required',[]): assert key in v, ('missing',key)
        props=s.get('properties',{})
        if s.get('additionalProperties') is False: assert not (set(v)-set(props)), ('extra',set(v)-set(props))
        for key,child in props.items():
            if key in v: check(child,v[key])
    if isinstance(v,list):
        if 'minItems' in s: assert len(v)>=s['minItems']
        if 'items' in s:
            for item in v: check(s['items'],item)
    if isinstance(v,str):
        if 'minLength' in s: assert len(v)>=s['minLength']
        if 'pattern' in s: assert re.search(s['pattern'],v)
    if type(v) is int:
        if 'minimum' in s: assert v>=s['minimum']
        if 'maximum' in s: assert v<=s['maximum']
    if 'oneOf' in s: assert sum(matches(b,v) for b in s['oneOf'])==1, 'oneOf'
    for branch in s.get('allOf',[]): check(branch,v)
    if 'if' in s:
        if matches(s['if'],v) and 'then' in s: check(s['then'],v)
        elif not matches(s['if'],v) and 'else' in s: check(s['else'],v)

class ContractExamples(unittest.TestCase):
    def test_all_generated_examples_validate_against_draft_subset(self):
        for name, value in EXAMPLES.items():
            with self.subTest(name=name):
                check(SCHEMA['$defs']['request' if name.startswith('request_') else 'response'],value)

    def test_reject_wrong_version_extra_field_and_malformed_source(self):
        bad=copy.deepcopy(EXAMPLES['request_search']);bad['contract_version']='UTT-ACCESS-READ/99.0.0'
        self.assertFalse(matches(SCHEMA['$defs']['request'],bad))
        bad=copy.deepcopy(EXAMPLES['request_search']);bad['private_path']='x'
        self.assertFalse(matches(SCHEMA['$defs']['request'],bad))
        bad=copy.deepcopy(EXAMPLES['detail']);bad['result']['sources'][0]['location']['unit']='characters'
        self.assertFalse(matches(SCHEMA['$defs']['response'],bad))
        bad=copy.deepcopy(EXAMPLES['connection_failure']);bad['result']={}
        self.assertFalse(matches(SCHEMA['$defs']['response'],bad))

if __name__=='__main__':unittest.main()
