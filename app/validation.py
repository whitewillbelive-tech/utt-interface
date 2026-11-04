"""Complete draft response schema and scope checks. No response repair/coercion."""
import hashlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator

_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'contracts/utt-read-api.schema.json').read_text())
_VALIDATOR = Draft202012Validator({'$ref': '#/$defs/response', '$defs': _SCHEMA['$defs']})


class ResponseError(Exception):
    def __init__(self, code='INTERNAL_ERROR'):
        self.code = code
        super().__init__('Read response rejected')


def validate_response(value, q, sort):
    if not _VALIDATOR.is_valid(value) or value['operation'] != q['operation']:
        raise ResponseError()
    scope = value['snapshot']
    if scope is not None:
        if q['dataset_id'] is not None and scope['dataset_id'] != q['dataset_id']:
            raise ResponseError()
        if q['snapshot_id'] is not None and scope['snapshot_id'] != q['snapshot_id']:
            raise ResponseError('VERSION_MISMATCH')
    if value['status'] == 'ERROR':
        return
    r = value['result']
    dataset = scope['dataset_id']

    def local(item):
        if item['dataset_id'] != dataset:
            raise ResponseError()

    def relation(item, anchor=None):
        endpoints = ((item['from_dataset_id'], item['from_id']),
                     (item['to_dataset_id'], item['to_id']))
        if not any(ds == dataset and (anchor is None or ident == anchor) for ds, ident in endpoints):
            raise ResponseError()

    def record(item, listed=False):
        local(item)
        for source in item['source_refs']:
            local(source)
        for link in item['project_relations']:
            relation(link, item['record_id'])
        if listed and (item['raw_text'] is not None or item['raw_text_sha256'] is not None):
            raise ResponseError()
        if item['raw_text_sha256'] is not None:
            if hashlib.sha256(item['raw_text'].encode('utf-8')).hexdigest() != item['raw_text_sha256']:
                raise ResponseError()

    op = q['operation']
    if op == 'capabilities':
        if r['dataset'] != scope or any(item not in ('capabilities', 'projects', 'search', 'detail', 'related_history')
                                        for item in r['operations']):
            raise ResponseError()
    elif op == 'projects':
        for item in r['projects'] + r['components']:
            local(item)
        for item in r['relations']:
            relation(item)
    elif op == 'search':
        for item in r['records']:
            record(item, listed=True)
    elif op == 'detail':
        record(r['record'])
        if r['record']['record_id'] != q['record_id']:
            raise ResponseError()
        for item in r['related_projects'] + r['sources']:
            local(item)
    elif op == 'related_history':
        for item in r['related_records']:
            record(item, listed=True)
        for item in r['relations']:
            relation(item)
        for item in r['events']:
            local(item)
            if item['record_id'] != q['record_id']:
                raise ResponseError()
    if 'page' in r:
        page = r['page']
        expected_sort = (['dataset_id:asc', 'record_id:' + ('asc' if sort == 'id_asc' else 'desc')]
                         if op == 'search' else ['item_kind:asc', 'dataset_id:asc', 'item_id:asc'])
        rows = sum(len(r[key]) for key in ('projects', 'components', 'relations', 'records', 'related_records', 'events') if key in r)
        if page['page_size'] != q['page_size'] or rows > page['page_size'] or page['sort'] != expected_sort:
            raise ResponseError()
        if q['cursor'] is not None and page['next_cursor'] == q['cursor']:
            raise ResponseError()
        # Detect observable page corruption without interpreting opaque cursors.
        for key, id_key in (('projects', 'project_id'), ('components', 'component_id'),
                            ('relations', 'relation_id'), ('records', 'record_id'),
                            ('related_records', 'record_id'), ('events', 'event_id')):
            if key not in r:
                continue
            ids = [item[id_key] for item in r[key]]
            reverse = op == 'search' and sort == 'id_desc'
            if len(ids) != len(set(ids)) or ids != sorted(ids, reverse=reverse):
                raise ResponseError()
