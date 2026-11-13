"""Structural checks for the *synthetic* conversation-view draft only.

This is not an API, a source adapter, or an approval/semantic verifier. It reads
no messages or files supplied by a user other than its two in-memory arguments.
Callers must explicitly select synthetic use; the marker is a guard against
accidental use, not proof that the caller's input is fictional.

The schema is the unmodified 2020-12 excerpt from the non-sensitive Work draft
handoff; it is not an approved canonical contract. All result values are fixed
codes: no source text, reference, path, or exception detail is returned/logged.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from jsonschema import Draft202012Validator

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / 'contracts' / 'utt-conversation-view-0.1-draft.schema.json'
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding='utf-8'))
Draft202012Validator.check_schema(_SCHEMA)
_VALIDATOR = Draft202012Validator(_SCHEMA)


@dataclass(frozen=True)
class ConversationCheck:
    """Status of bounded structural checks, never an approval or truth claim."""

    status: str
    code: str


def _result(status: str, code: str) -> ConversationCheck:
    return ConversationCheck(status, code)


def validate_conversation_view(projection, messages, *, synthetic_only=False) -> ConversationCheck:
    """Check a draft against an explicitly fictional, ordered message fixture.

    ``messages`` is an ordered list of exact dictionaries with ``conversation_ref``,
    ``message_ref``, ``source_revision``, ``role`` and ``text``. This is a local test fixture,
    not an established format for actual GPT messages. ``role`` may be user,
    assistant, tool or unknown. Every provided text must be Unicode scalar text;
    span coordinates are zero-based, code-point based and end-exclusive.

    A structural pass always returns SEMANTICS_UNVERIFIED. In particular,
    matching roles and a ``confirmed`` label cannot establish the content,
    accuracy, source provenance, user approval or existing ledger state.
    """
    if synthetic_only is not True:
        return _result('NOT_SUPPORTED', 'SYNTHETIC_ONLY')
    try:
        if not isinstance(projection, dict) or not _VALIDATOR.is_valid(projection):
            return _result('INVALID', 'INVALID_DRAFT')
        source = projection['source']
        if source['verification'] != 'synthetic' or projection['generated_status'] != 'unreviewed_synthetic':
            return _result('NOT_SUPPORTED', 'SYNTHETIC_ONLY')
        if not isinstance(messages, list):
            return _result('INVALID', 'INVALID_MESSAGES')
        if len(messages) > 10000 or len(projection['topics']) > 10000:
            return _result('NOT_SUPPORTED', 'LIMIT_EXCEEDED')

        revision = source['revision']
        by_ref = {}
        total_chars = 0
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or set(message) != {'conversation_ref', 'message_ref', 'source_revision', 'role', 'text'}:
                return _result('INVALID', 'INVALID_MESSAGE')
            if message['conversation_ref'] != source['conversation_ref']:
                return _result('INVALID', 'CONVERSATION_MISMATCH')
            ref, message_revision = message['message_ref'], message['source_revision']
            if not isinstance(ref, str) or not ref or not isinstance(message_revision, str) or not message_revision:
                return _result('INVALID', 'INVALID_MESSAGE')
            if message_revision != revision:
                return _result('STALE', 'REVISION_MISMATCH')
            if ref in by_ref:
                return _result('INVALID', 'DUPLICATE_MESSAGE')
            if message['role'] not in ('user', 'assistant', 'tool', 'unknown'):
                return _result('INVALID', 'INVALID_ROLE')
            text = message['text']
            if text is None:
                return _result('UNAVAILABLE', 'TEXT_UNAVAILABLE')
            if not isinstance(text, str) or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
                return _result('INVALID', 'INVALID_TEXT')
            total_chars += len(text)
            if total_chars > 5_000_000:
                return _result('NOT_SUPPORTED', 'LIMIT_EXCEEDED')
            by_ref[ref] = (index, message)

        seen_topics = set()
        for topic in projection['topics']:
            if topic['topic_ref'] in seen_topics:
                return _result('INVALID', 'DUPLICATE_TOPIC')
            seen_topics.add(topic['topic_ref'])
            positions = []
            for ref in topic['message_refs']:
                if ref not in by_ref:
                    return _result('UNAVAILABLE', 'MISSING_MESSAGE')
                positions.append(by_ref[ref][0])
            if positions != sorted(positions):
                return _result('INVALID', 'MESSAGE_ORDER')
            refs = set(topic['message_refs'])
            for claim in topic['claims']:
                roles = set()
                for evidence in claim['evidence']:
                    ref = evidence['message_ref']
                    if ref not in refs:
                        return _result('INVALID', 'EVIDENCE_OUTSIDE_TOPIC')
                    if evidence['source_revision'] != revision:
                        return _result('STALE', 'REVISION_MISMATCH')
                    message = by_ref[ref][1]
                    roles.add(message['role'])
                    if evidence['precision'] == 'span':
                        span = evidence['span']
                        start, end = span['start'], span['end_exclusive']
                        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(message['text']):
                            return _result('INVALID', 'INVALID_RANGE')
                actor = claim['actor']
                if claim['status'] == 'confirmed' and actor == 'assistant':
                    return _result('INVALID', 'CONFIRMATION_REQUIRES_USER')
                if claim['status'] == 'confirmed' and actor == 'unknown':
                    return _result('UNAVAILABLE', 'ACTOR_UNVERIFIED')
                expected = {'user', 'assistant'} if actor == 'both' else {actor} if actor != 'unknown' else set()
                if not expected.issubset(roles):
                    if 'unknown' in roles:
                        return _result('UNAVAILABLE', 'ACTOR_UNVERIFIED')
                    return _result('INVALID', 'ACTOR_MISMATCH')
        return _result('STRUCTURALLY_VALID', 'SEMANTICS_UNVERIFIED')
    except (KeyError, TypeError, ValueError, RecursionError):
        return _result('INVALID', 'INVALID_INPUT')
