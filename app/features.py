"""Independent UTT-READ-FEATURES/0.1.0 filter descriptions.

The exact wire object is ``{contract_version, filters}``; filters has exactly
query, project, document_role, review_status, tag and source. Each value is one
of SUPPORTED, CONDITIONAL, UNSUPPORTED or UNKNOWN. This separate response does
not add fields to the immutable UTT-ACCESS-READ/0.1.0 capabilities or request.

SUPPORTED describes implemented filter semantics, not live data access or a
successful connection. CONDITIONAL means SnapshotProvider project filtering
requires its documented complete AVAILABLE, USER_CONFIRMED local membership.
UNKNOWN never permits the UI to claim a remote filter works. Native D0 tag or
source filtering cannot imply support through READ/0.1, whose request has no
tag/source fields; both remain UNSUPPORTED here.

Only exact known implementation types and their real bound read methods are
recognized. No provider method, source file, environment setting or endpoint is
consulted. Imports are local to features_for() to avoid provider import cycles.
All returns are fresh dictionaries with no provider identifiers or diagnostics.
"""
from types import MethodType

from jsonschema import Draft202012Validator

VERSION = 'UTT-READ-FEATURES/0.1.0'
FILTERS = ('query', 'project', 'document_role', 'review_status', 'tag', 'source')
STATES = ('SUPPORTED', 'CONDITIONAL', 'UNSUPPORTED', 'UNKNOWN')
_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object',
    'properties': {
        'contract_version': {'const': VERSION},
        'filters': {
            'type': 'object',
            'properties': {field: {'enum': list(STATES)} for field in FILTERS},
            'required': list(FILTERS),
            'additionalProperties': False,
        },
    },
    'required': ['contract_version', 'filters'],
    'additionalProperties': False,
}
_VALIDATOR = Draft202012Validator(_SCHEMA)


class FeaturesError(ValueError):
    """A fixed validation error; untrusted values never enter its message."""
    def __init__(self):
        super().__init__('Filter feature response is invalid.')


def validate_features(value):
    """Return None for the exact wire shape, otherwise raise FeaturesError."""
    if not _VALIDATOR.is_valid(value):
        raise FeaturesError()
    return None


def features_for(provider):
    """Return static filter support for a known provider, without reading data.

    Unknown objects, subclasses, closures and HTTPS adapters stay conservative.
    A callback's ``__self__`` is inspected only for an actual Python bound
    method and both its exact owner type and implementation function must match.
    Declared attributes such as features/capabilities are never trusted.
    """
    from app.provider import ReadOnlyAdapter, SyntheticProvider
    from app.snapshot_provider import SnapshotProvider

    implementation = provider
    if type(provider) is ReadOnlyAdapter:
        transport = provider.transport
        implementation = None
        if type(transport) is MethodType:
            owner = transport.__self__
            for known in (SyntheticProvider, SnapshotProvider):
                if type(owner) is known and transport.__func__ is known.read:
                    implementation = owner
                    break

    support = {field: 'UNKNOWN' for field in FILTERS}
    # These filters do not exist in READ/0.1, independently of native support.
    support.update(tag='UNSUPPORTED', source='UNSUPPORTED')
    if type(implementation) is SyntheticProvider:
        support.update(query='SUPPORTED', project='SUPPORTED',
                       document_role='SUPPORTED', review_status='SUPPORTED')
    elif type(implementation) is SnapshotProvider:
        support.update(query='SUPPORTED', project='CONDITIONAL',
                       document_role='UNSUPPORTED', review_status='UNSUPPORTED')
    result = {'contract_version': VERSION, 'filters': support}
    validate_features(result)
    return result
