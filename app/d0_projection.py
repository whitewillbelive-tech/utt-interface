"""Lossless structural inspection of caller-supplied native D0 JSON.

This in-memory artifact is explicitly NOT a UTT-ACCESS-READ response or a native
D0-to-UTT semantic adapter. The known response shapes come from the D0 read
client/static UI reference; source, relation and history meanings still require
the approved runtime's mapping. Inspection never opens files, consults runtime
configuration, connects, normalizes strings, deduplicates evidence or creates
IDs, events, snapshot identifiers or cursors.

The complete original document and every nested array entry remain available
through defensive copies. JSON pointers identify positions in this one response
only: they are not permanent record/relation/evidence identities. Native NULL,
empty strings and absent members stay different. In particular, stored_body is
only the native B stored body; it never supplies an original source body. Native
availability/verification labels remain opaque input, not verified assertions.
Keep this artifact inside the approved data runtime; do not log/export its data.
"""
import copy
import math
from dataclasses import dataclass, field
from enum import Enum

from app.d0_adapter import D0Evidence


FORMAT = 'D0-STRUCTURAL-INSPECTION/0.1.0'
_KINDS = frozenset(('records', 'detail', 'summary', 'filters'))


class Presence(str, Enum):
    ABSENT = 'ABSENT'
    NULL = 'NULL'
    VALUE = 'VALUE'


class JSONKind(str, Enum):
    NULL = 'null'
    BOOLEAN = 'boolean'
    INTEGER = 'integer'
    NUMBER = 'number'
    STRING = 'string'
    ARRAY = 'array'
    OBJECT = 'object'


class D0ProjectionError(ValueError):
    """Safe input error with no document or caller diagnostic attached."""
    def __init__(self, code='INVALID_INPUT'):
        self.code = code
        super().__init__('D0 structural inspection failed.')


@dataclass(frozen=True, repr=False)
class D0Node:
    """One exact native JSON value; children retain object/array order."""
    pointer: str
    kind: JSONKind
    _value: object = field(repr=False)
    children: tuple = field(default=(), repr=False)

    @property
    def value(self):
        return copy.deepcopy(self._value)

    def __repr__(self):
        return 'D0Node(kind=' + repr(self.kind.value) + ', content=<private>)'


@dataclass(frozen=True, repr=False)
class D0Field:
    pointer: str
    presence: Presence
    node: D0Node | None = field(repr=False)

    @property
    def value(self):
        # An absent field must not silently become a native JSON null.
        if self.node is None:
            raise D0ProjectionError('ABSENT_FIELD')
        return self.node.value

    def __repr__(self):
        return 'D0Field(presence=' + repr(self.presence.value) + ', content=<private>)'


@dataclass(frozen=True)
class D0Issue:
    """Structural diagnostic, not a semantic or source-verification verdict."""
    pointer: str
    code: str


@dataclass(frozen=True, repr=False)
class D0Projection:
    kind: str
    root: D0Node = field(repr=False)
    known_fields: tuple[D0Field, ...] = field(repr=False)
    issues: tuple[D0Issue, ...]
    format: str = field(default=FORMAT, init=False)
    mapping_status: str = field(default='UNVERIFIED', init=False)
    snapshot_status: str = field(default='UNKNOWN', init=False)
    page_consistency_status: str = field(default='UNKNOWN', init=False)
    relation_identity_status: str = field(default='UNKNOWN', init=False)
    history_status: str = field(default='UNKNOWN', init=False)
    source_access_status: str = field(default='UNKNOWN', init=False)
    original_source_body_status: str = field(default='UNKNOWN', init=False)

    @property
    def document(self):
        """Exact original parsed JSON, copied on every access."""
        return self.root.value

    def at(self, pointer):
        """Inspect a JSON pointer without defaulting, resolving IDs or coercing."""
        return _at(self.root, pointer)

    def entries(self, pointer):
        """All array entries in order, including duplicates and malformed items.

        Missing/null/wrong-shape collections raise instead of becoming empty.
        Check at(pointer).presence first when that distinction is needed.
        """
        selected = self.at(pointer)
        if selected.node is None or selected.node.kind != JSONKind.ARRAY:
            raise D0ProjectionError('NOT_AN_ARRAY')
        return selected.node.children

    @property
    def stored_body(self):
        """Native B stored-body value only; no source fallback or verification."""
        return self.at('/stored_body')

    @property
    def source_metadata(self):
        """Native sources field; presence does not establish source access."""
        return self.at('/sources')

    def __repr__(self):
        return ('D0Projection(kind=' + repr(self.kind) + ', mapping=UNVERIFIED, '
                'issues=' + str(len(self.issues)) + ', content=<private>)')


def _escape(value):
    return value.replace('~', '~0').replace('/', '~1')


def _node(value, pointer, ancestors, depth=0):
    if depth > 128:
        raise D0ProjectionError()
    kind = {type(None): JSONKind.NULL, bool: JSONKind.BOOLEAN,
            int: JSONKind.INTEGER, float: JSONKind.NUMBER,
            str: JSONKind.STRING, list: JSONKind.ARRAY,
            dict: JSONKind.OBJECT}.get(type(value))
    if kind is None or (type(value) is float and not math.isfinite(value)):
        raise D0ProjectionError()
    children = ()
    if type(value) in (dict, list):
        if id(value) in ancestors:
            raise D0ProjectionError()
        ancestors = ancestors | {id(value)}
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise D0ProjectionError()
            children = tuple(_node(item, pointer + '/' + _escape(key), ancestors, depth + 1)
                             for key, item in value.items())
        else:
            children = tuple(_node(item, pointer + '/' + str(index), ancestors, depth + 1)
                             for index, item in enumerate(value))
    return D0Node(pointer, kind, value, children)


def _at(root, pointer):
    if type(pointer) is not str or (pointer and not pointer.startswith('/')):
        raise D0ProjectionError('INVALID_POINTER')
    if pointer == '':
        return D0Field('', Presence.VALUE, root)
    tokens = pointer[1:].split('/')
    decoded = []
    for token in tokens:
        index = 0
        while index < len(token):
            if token[index] == '~':
                if index + 1 == len(token) or token[index + 1] not in '01':
                    raise D0ProjectionError('INVALID_POINTER')
                index += 1
            index += 1
        decoded.append(token.replace('~1', '/').replace('~0', '~'))
    current = root
    for token in decoded:
        if current.kind == JSONKind.OBJECT:
            keys = tuple(current._value)
            current = (current.children[keys.index(token)] if token in current._value else None)
        elif current.kind == JSONKind.ARRAY:
            if not token.isascii() or not token.isdigit() or (token != '0' and token.startswith('0')):
                current = None
            else:
                # Avoid parsing an arbitrarily large caller-provided index.
                current = (current.children[int(token)]
                           if len(token) <= len(str(len(current.children)))
                           and int(token) < len(current.children) else None)
        else:
            current = None
        if current is None:
            return D0Field(pointer, Presence.ABSENT, None)
    presence = Presence.NULL if current.kind == JSONKind.NULL else Presence.VALUE
    return D0Field(pointer, presence, current)


def inspect_d0(kind, document):
    """Inspect one explicit native JSON document without any external access.

    Invalid JSON types/cycles fail safely. Missing or wrongly typed known native
    fields produce issues while the entire original document is still retained.
    Even issue-free inspection establishes structure only, never UTT semantics.
    """
    if type(kind) is not str or kind not in _KINDS or type(document) is not dict:
        raise D0ProjectionError()
    # Validate exact JSON types before deepcopy, avoiding arbitrary object hooks.
    _node(document, '', set())
    root = _node(copy.deepcopy(document), '', set())
    shapes = {
        'records': (('/total', JSONKind.INTEGER, True), ('/results', JSONKind.ARRAY, True)),
        'detail': (('/canonical_record', JSONKind.OBJECT, True),
                   ('/comparison', JSONKind.OBJECT, True),
                   ('/stored_body', JSONKind.STRING, False),
                   ('/sources', JSONKind.ARRAY, False),
                   ('/canonical_record/title', JSONKind.STRING, False),
                   ('/canonical_record/content', JSONKind.OBJECT, False),
                   ('/canonical_record/content/display_blocks', JSONKind.ARRAY, False),
                   ('/projects', JSONKind.ARRAY, False),
                   ('/b_relations', JSONKind.ARRAY, False),
                   ('/baseline_relations', JSONKind.ARRAY, False),
                   ('/lineage', None, False), ('/baseline_evidence', None, False),
                   ('/baseline_manuscript', None, False), ('/baseline_segments', None, False)),
        'summary': (('/stats', JSONKind.OBJECT, True),),
        'filters': (('/tags', JSONKind.ARRAY, True), ('/projects', JSONKind.ARRAY, True),
                    ('/sources', JSONKind.ARRAY, True)),
    }[kind]
    known, issues = [], []
    for pointer, expected, required in shapes:
        selected = _at(root, pointer)
        known.append(selected)
        if selected.presence == Presence.ABSENT:
            if required:
                issues.append(D0Issue(pointer, 'MISSING_FIELD'))
        elif (expected is not None and selected.node.kind != expected
              and (required or selected.presence != Presence.NULL)):
            issues.append(D0Issue(pointer, 'WRONG_JSON_TYPE'))
    if kind == 'records':
        total, results = _at(root, '/total'), _at(root, '/results')
        if total.node is not None and total.node.kind == JSONKind.INTEGER and total.value < 0:
            issues.append(D0Issue('/total', 'NEGATIVE_TOTAL'))
        if results.node is not None and results.node.kind == JSONKind.ARRAY:
            for item in results.node.children:
                if item.kind != JSONKind.OBJECT:
                    issues.append(D0Issue(item.pointer, 'WRONG_JSON_TYPE'))
                    continue
                identity = _at(root, item.pointer + '/id')
                known.append(identity)
                if identity.presence == Presence.ABSENT:
                    issues.append(D0Issue(identity.pointer, 'MISSING_FIELD'))
                elif not ((identity.node.kind == JSONKind.STRING and identity.value != '')
                          or identity.node.kind == JSONKind.INTEGER):
                    issues.append(D0Issue(identity.pointer, 'INVALID_NATIVE_ID'))
    return D0Projection(kind, root, tuple(known), tuple(issues))


def inspect_evidence(evidence):
    """Inspect an already-returned D0Evidence, without making a D0 request."""
    if type(evidence) is not D0Evidence:
        raise D0ProjectionError()
    return inspect_d0(evidence.kind, evidence.document)
