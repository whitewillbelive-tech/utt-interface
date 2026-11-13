"""Fictional conversation-view checks. No original messages or remote I/O."""
import copy
import unittest

from app.conversation_draft import validate_conversation_view


def fictional_inputs():
    """A fictional transcript; codepoint 2..3 names one astral character."""
    revision = 'fictional-revision-one'
    messages = [
        {'message_ref': 'fictional-m1', 'source_revision': revision,
         'role': 'user', 'text': '앞 🧭 동쪽'},
        {'message_ref': 'fictional-m2', 'source_revision': revision,
         'role': 'assistant', 'text': '가상 제안 🧭'},
        {'message_ref': 'fictional-m3', 'source_revision': revision,
         'role': 'unknown', 'text': '가상 미확인'},
    ]
    for message in messages:
        message['conversation_ref'] = 'fictional-conversation'
    def evidence(ref, precision='message', span=None):
        result = {'message_ref': ref, 'source_revision': revision, 'precision': precision}
        if span is not None:
            result['span'] = {'unit': 'unicode_code_point',
                              'start': span[0], 'end_exclusive': span[1]}
        return result
    projection = {
        'format_version': 'UTT-CONVERSATION-VIEW/0.1-draft',
        'source': {'conversation_ref': 'fictional-conversation', 'revision': revision,
                   'verification': 'synthetic'},
        'generated_status': 'unreviewed_synthetic',
        'projects': [{'display_name': '가상 프로젝트', 'basis': 'unknown'}],
        'topics': [
            {'topic_ref': 'fictional-topic-one', 'title': '가상 논의',
             'message_refs': ['fictional-m1', 'fictional-m2'],
             'claims': [
                 {'text': '가상 사용자 관점', 'actor': 'user', 'status': 'viewpoint',
                  'evidence': [evidence('fictional-m1', 'span', (2, 3))]},
                 {'text': '가상 AI 제안', 'actor': 'assistant', 'status': 'proposal',
                  'evidence': [evidence('fictional-m2')]},
             ]},
            {'topic_ref': 'fictional-topic-two', 'title': '또 다른 가상 논의',
             'message_refs': ['fictional-m1', 'fictional-m2', 'fictional-m3'],
             'claims': [
                 {'text': '가상 작성자 미확인', 'actor': 'unknown', 'status': 'unknown',
                  'evidence': [evidence('fictional-m3')]},
             ]},
        ],
        'cross_conversation_candidates': [{
            'target_ref': 'fictional-related', 'status': 'candidate',
            'merged': False, 'reason': '가상 후보',
        }],
    }
    return projection, messages


class SyntheticConversationDraftTests(unittest.TestCase):
    def setUp(self):
        self.projection, self.messages = fictional_inputs()

    def check(self, projection=None, messages=None, *, enabled=True):
        return validate_conversation_view(
            self.projection if projection is None else projection,
            self.messages if messages is None else messages,
            synthetic_only=enabled)

    def test_synthetic_shape_references_order_and_no_semantic_approval(self):
        before = copy.deepcopy((self.projection, self.messages))
        result = self.check()
        self.assertEqual((result.status, result.code), ('STRUCTURALLY_VALID', 'SEMANTICS_UNVERIFIED'))
        self.assertEqual((self.projection, self.messages), before)
        # The codepoint span selects exactly the emoji, not UTF-16 code units.
        self.assertEqual(self.messages[0]['text'][2:3], '🧭')
        self.assertNotIn('가상 사용자 관점', repr(result))
        self.assertNotIn('fictional-m1', repr(result))

    def test_explicit_synthetic_gate_never_auto_accepts_other_source(self):
        self.assertEqual(self.check(enabled=False).status, 'NOT_SUPPORTED')
        for field, value in [('verification', 'verified'), ('verification', 'unverified')]:
            with self.subTest(field=field, value=value):
                value_projection = copy.deepcopy(self.projection)
                value_projection['source'][field] = value
                self.assertEqual(self.check(projection=value_projection).status, 'NOT_SUPPORTED')
        for value in ['reviewed', 'unreviewed']:
            value_projection = copy.deepcopy(self.projection)
            value_projection['generated_status'] = value
            self.assertEqual(self.check(projection=value_projection).status, 'NOT_SUPPORTED')

    def test_draft_shape_is_strict_and_excludes_message_data(self):
        variants = []
        value = copy.deepcopy(self.projection); value['unexpected'] = 'fictional'; variants.append(value)
        value = copy.deepcopy(self.projection); value['messages'] = self.messages; variants.append(value)
        value = copy.deepcopy(self.projection); value['format_version'] = 'UTT-ACCESS-READ/0.1.0'; variants.append(value)
        value = copy.deepcopy(self.projection); value['topics'][0]['claims'][0]['evidence'] = []; variants.append(value)
        for value in variants:
            with self.subTest(value_type=list(value)):
                self.assertNotEqual(self.check(projection=value).status, 'STRUCTURALLY_VALID')

    def test_original_message_missing_or_unlisted_evidence_cannot_validate(self):
        missing = copy.deepcopy(self.projection)
        missing['topics'][0]['message_refs'][0] = 'fictional-not-in-transcript'
        missing['topics'][0]['claims'][0]['evidence'][0]['message_ref'] = 'fictional-not-in-transcript'
        self.assertEqual(self.check(projection=missing).status, 'UNAVAILABLE')
        unlisted = copy.deepcopy(self.projection)
        unlisted['topics'][0]['claims'][0]['evidence'][0]['message_ref'] = 'fictional-m3'
        self.assertNotEqual(self.check(projection=unlisted).status, 'STRUCTURALLY_VALID')

    def test_missing_original_text_is_unavailable_not_empty_or_success(self):
        messages = copy.deepcopy(self.messages)
        messages[0]['text'] = None
        result = self.check(messages=messages)
        self.assertEqual((result.status, result.code), ('UNAVAILABLE', 'TEXT_UNAVAILABLE'))

    def test_precision_requires_span_only_for_span_evidence(self):
        value = copy.deepcopy(self.projection)
        del value['topics'][0]['claims'][0]['evidence'][0]['span']
        self.assertEqual(self.check(projection=value).status, 'INVALID')
        value = copy.deepcopy(self.projection)
        value['topics'][0]['claims'][0]['evidence'][0]['precision'] = 'message'
        self.assertEqual(self.check(projection=value).status, 'INVALID')

    def test_related_conversations_are_unmerged_candidates_only(self):
        for change in ({'merged': True}, {'status': 'approved'}):
            with self.subTest(change=change):
                value = copy.deepcopy(self.projection)
                value['cross_conversation_candidates'][0].update(change)
                self.assertEqual(self.check(projection=value).status, 'INVALID')

    def test_duplicate_original_and_topic_refs_and_order_are_rejected(self):
        duplicate = copy.deepcopy(self.messages)
        duplicate.append(copy.deepcopy(duplicate[0]))
        self.assertNotEqual(self.check(messages=duplicate).status, 'STRUCTURALLY_VALID')
        duplicate_topic = copy.deepcopy(self.projection)
        duplicate_topic['topics'].append(copy.deepcopy(duplicate_topic['topics'][0]))
        self.assertNotEqual(self.check(projection=duplicate_topic).status, 'STRUCTURALLY_VALID')
        out_of_order = copy.deepcopy(self.projection)
        out_of_order['topics'][0]['message_refs'].reverse()
        self.assertNotEqual(self.check(projection=out_of_order).status, 'STRUCTURALLY_VALID')

    def test_changed_source_or_evidence_revision_is_stale(self):
        value = copy.deepcopy(self.projection)
        value['source']['revision'] = 'fictional-revision-new'
        self.assertEqual(self.check(projection=value).status, 'STALE')
        value = copy.deepcopy(self.projection)
        value['topics'][0]['claims'][0]['evidence'][0]['source_revision'] = 'fictional-revision-old'
        self.assertEqual(self.check(projection=value).status, 'STALE')

    def test_message_reference_cannot_cross_conversation_even_when_id_collides(self):
        messages = copy.deepcopy(self.messages)
        messages[0]['conversation_ref'] = 'fictional-another-conversation'
        result = self.check(messages=messages)
        self.assertEqual((result.status, result.code), ('INVALID', 'CONVERSATION_MISMATCH'))
        del messages[0]['conversation_ref']
        self.assertEqual(self.check(messages=messages).status, 'INVALID')

    def test_unknown_evidence_role_never_establishes_known_actor(self):
        messages = copy.deepcopy(self.messages)
        messages[0]['role'] = 'unknown'
        self.assertEqual(self.check(messages=messages).status, 'UNAVAILABLE')
        tool = copy.deepcopy(self.messages)
        tool[0]['role'] = 'tool'
        self.assertNotEqual(self.check(messages=tool).status, 'STRUCTURALLY_VALID')

    def test_actor_and_confirmed_status_do_not_invent_user_agreement(self):
        value = copy.deepcopy(self.projection)
        value['topics'][0]['claims'][0]['status'] = 'confirmed'
        result = self.check(projection=value)
        self.assertEqual((result.status, result.code), ('STRUCTURALLY_VALID', 'SEMANTICS_UNVERIFIED'))
        value['topics'][0]['claims'][0]['actor'] = 'assistant'
        self.assertNotEqual(self.check(projection=value).status, 'STRUCTURALLY_VALID')

    def test_both_actor_requires_both_roles_in_evidence(self):
        value = copy.deepcopy(self.projection)
        claim = value['topics'][0]['claims'][0]
        claim['actor'] = 'both'
        self.assertNotEqual(self.check(projection=value).status, 'STRUCTURALLY_VALID')
        claim['evidence'].append(copy.deepcopy(value['topics'][0]['claims'][1]['evidence'][0]))
        self.assertEqual(self.check(projection=value).status, 'STRUCTURALLY_VALID')

    def test_codepoint_ranges_reject_reversed_empty_and_out_of_bounds(self):
        for start, end in [(3, 2), (2, 2), (0, 7), (6, 7), (-1, 1), (True, 3), (2, 3.0)]:
            with self.subTest(start=start, end=end):
                value = copy.deepcopy(self.projection)
                span = value['topics'][0]['claims'][0]['evidence'][0]['span']
                span.update(start=start, end_exclusive=end)
                self.assertNotEqual(self.check(projection=value).status, 'STRUCTURALLY_VALID')
        # Whole string's last valid codepoint and end-exclusive boundary are accepted.
        value = copy.deepcopy(self.projection)
        value['topics'][0]['claims'][0]['evidence'][0]['span'].update(start=5, end_exclusive=6)
        self.assertEqual(self.check(projection=value).status, 'STRUCTURALLY_VALID')

    def test_surrogates_do_not_count_as_unicode_scalar_values(self):
        value = copy.deepcopy(self.messages)
        value[0]['text'] = '가\ud83e나'
        self.assertNotEqual(self.check(messages=value).status, 'STRUCTURALLY_VALID')

    def test_error_results_do_not_expose_message_or_identifiers(self):
        value = copy.deepcopy(self.projection)
        value['topics'][0]['claims'][0]['evidence'][0]['message_ref'] = 'fictional-private-id'
        result = self.check(projection=value)
        self.assertNotIn('fictional-private-id', repr(result))
        self.assertNotIn('가상 사용자 관점', repr(result))
        self.assertNotIn('🧭', repr(result))


if __name__ == '__main__':
    unittest.main()
