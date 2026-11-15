"""Synthetic coverage for the workspace-specific detailed read boundary."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.workspace_extended_read import list_workspace_catalog, list_workspace_members, read_workspace_record
from tests.test_publisher_workspace import make_fixture


class WorkspaceExtendedReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = make_fixture(Path(self.temp.name) / 'fictional.sqlite')
        self.addCleanup(self.conn.close)
        self.ident = 'fictional-record'

    def test_exact_record_relationships_and_multiple_evidence_are_preserved(self):
        c = self.conn
        c.execute("UPDATE work_records SET archived_at='fictional-time' WHERE record_id=?", (self.ident,))
        digest = hashlib.sha256(b'fictional child').hexdigest()
        c.execute('''INSERT INTO work_records(record_id,title,body,body_sha256,document_role,review_status,adoption_status,merged_into)
                     VALUES(?,?,?,?,?,?,?,?)''', ('fictional-child','child','fictional child',digest,'NOTE','PENDING','UNREVIEWED',self.ident))
        c.execute("INSERT INTO work_projects(project_id,name,state) VALUES('fictional-project','project',NULL)")
        c.executemany("INSERT INTO work_record_projects(link_id,record_id,project_id,role,scope,basis,original_evidence_id) VALUES (?,?,?,?,?,?,?)", [
            ('link-a',self.ident,'fictional-project','PRIMARY',None,'INHERITED_CLASSIFICATION_ONLY',None),
            ('link-b',self.ident,'fictional-project','SECONDARY','','CANDIDATE','evidence-a')])
        c.execute("INSERT INTO work_topics(topic_id,name,parent_id,notes) VALUES ('topic-a','topic',NULL,'')")
        c.execute("INSERT INTO work_record_topics VALUES (?,?,?)", (self.ident,'topic-a','INHERITED'))
        c.executemany("INSERT INTO work_sources VALUES (?,?,?,?,?,?)", [
            ('evidence-a',self.ident,'source-a','page 2','PAGE',0),
            ('evidence-b',self.ident,'source-a','page 3','PAGE',1),
            ('evidence-c',self.ident,None,None,None,0)])
        c.executemany("INSERT INTO work_decisions(decision_id,record_id,decision,explanation,actor,user_authorized) VALUES (?,?,?,?,?,?)", [
            ('decision-a',self.ident,'UNRESOLVED',None,'ASSISTANT',0),
            ('decision-b',self.ident,'CONFIRMED','','USER',1)])
        c.execute("INSERT INTO work_tags VALUES ('tag-a','tag')")
        c.execute("INSERT INTO work_record_tags VALUES (?,?)", (self.ident,'tag-a'))
        c.execute("INSERT INTO work_notes(note_id,record_id,author,note_text) VALUES ('note-a',?,'ASSISTANT','fictional note')", (self.ident,))
        c.execute("INSERT INTO work_proposals(proposal_id,record_id,proposed_action,proposal_json,proposer) VALUES ('proposal-a',?,'EDIT','{}','ASSISTANT')", (self.ident,))
        c.commit()
        before = c.total_changes
        result = read_workspace_record(c, self.ident)
        self.assertEqual((result['contract_version'], result['status']), ('UTT-WORKSPACE-DETAIL/0.1','OK'))
        self.assertEqual(result['record']['archived_at'], 'fictional-time')
        self.assertIsNone(result['record']['merged_into'])
        self.assertEqual(result['merged_children'][0]['record_id'], 'fictional-child')
        self.assertEqual(len(result['projects']), 2)
        self.assertIsNone(result['projects'][0]['link']['scope'])
        self.assertEqual(result['projects'][1]['link']['scope'], '')
        self.assertEqual(result['projects'][0]['link']['basis'], 'INHERITED_CLASSIFICATION_ONLY')
        self.assertEqual(result['topics'][0]['link']['status'], 'INHERITED')
        self.assertEqual([x['evidence_id'] for x in result['sources']], ['evidence-a','evidence-b','evidence-c'])
        self.assertEqual([x['locator'] for x in result['sources']], ['page 2','page 3',None])
        self.assertEqual({x['actor']:x['user_authorized'] for x in result['decisions']}, {'ASSISTANT':0,'USER':1})
        self.assertEqual(result['revisions'][0]['version'], 1)
        self.assertEqual(result['events'][0]['actor'], 'ASSISTANT')
        self.assertEqual(result['events'][0]['detail_json'], '{}')
        self.assertEqual(result['tags'][0]['tag_id'], 'tag-a')
        self.assertEqual(result['notes'][0]['author'], 'ASSISTANT')
        self.assertEqual(result['proposals'][0]['state'], 'PENDING')
        self.assertEqual(c.total_changes, before)
        self.assertFalse(c.in_transaction)

    def test_existing_caller_transaction_is_not_committed_or_rolled_back(self):
        self.conn.execute("UPDATE work_records SET summary='fictional pending' WHERE record_id=?", (self.ident,))
        self.assertTrue(self.conn.in_transaction)
        result = read_workspace_record(self.conn, self.ident)
        self.assertEqual(result['record']['summary'], 'fictional pending')
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(read_workspace_record(self.conn, self.ident)['record']['summary'])

    def test_missing_table_and_corrupt_body_fail_without_fabricating_empty_results(self):
        self.conn.execute("UPDATE work_records SET body='fictional mismatch' WHERE record_id=?", (self.ident,))
        self.conn.commit()
        self.assertEqual(read_workspace_record(self.conn, self.ident)['error']['code'], 'CONTENT_MISMATCH')
        self.conn.execute('DROP TABLE work_sources')
        self.conn.commit()
        self.assertEqual(read_workspace_record(self.conn, self.ident)['error']['code'], 'UNSUPPORTED_SCHEMA')

    def test_unknown_record_and_bad_input_are_fixed_redacted_errors(self):
        for bad, code in [('not-present-secret', 'ITEM_NOT_FOUND'), (None, 'INVALID_REQUEST'), ('', 'INVALID_REQUEST'), ('x\x00y','INVALID_REQUEST')]:
            result = read_workspace_record(self.conn, bad)
            self.assertEqual(result['error']['code'], code)
            self.assertNotIn('not-present-secret', str(result))

    def test_catalog_returns_stored_rows_in_pages(self):
        self.conn.executemany('INSERT INTO work_projects(project_id,name,state) VALUES (?,?,?)', [
            ('p-b','beta','held'), ('p-a','alpha',None), ('p-c','gamma','')])
        self.conn.commit()
        first = list_workspace_catalog(self.conn, 'projects', limit=2)
        self.assertEqual([item['project_id'] for item in first['items']], ['p-a','p-b'])
        self.assertIsNone(first['items'][0]['state'])
        self.assertEqual(first['next_offset'], 2)
        second = list_workspace_catalog(self.conn, 'projects', limit=2, offset=2)
        self.assertEqual(second['items'][0]['state'], '')
        self.assertIsNone(second['next_offset'])
        self.assertEqual(list_workspace_catalog(self.conn, [], limit=2)['error']['code'], 'INVALID_REQUEST')
        self.assertEqual(list_workspace_catalog(self.conn, 'projects', limit=True)['error']['code'], 'INVALID_REQUEST')

    def test_result_limit_is_error_not_silent_truncation(self):
        self.conn.executemany('INSERT INTO work_sources(evidence_id,record_id) VALUES (?,?)',
                              ((f'fictional-evidence-{i}', self.ident) for i in range(10001)))
        self.conn.commit()
        result = read_workspace_record(self.conn, self.ident)
        self.assertEqual(result['error']['code'], 'RESULT_LIMIT_EXCEEDED')
        self.assertNotIn('sources', result)

    def test_project_and_topic_members_keep_links_and_lifecycle_state(self):
        self.conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('p','project')")
        self.conn.execute("INSERT INTO work_topics(topic_id,name) VALUES ('t','topic')")
        self.conn.execute("UPDATE work_records SET archived_at='fictional-date' WHERE record_id=?", (self.ident,))
        self.conn.execute("INSERT INTO work_record_projects(link_id,record_id,project_id,role,basis,archived_at) VALUES ('l',?,'p','PRIMARY','CANDIDATE','fictional-link-date')", (self.ident,))
        self.conn.execute("INSERT INTO work_record_topics VALUES (?,'t','INHERITED')", (self.ident,))
        self.conn.commit()
        project = list_workspace_members(self.conn, 'project', 'p')
        self.assertEqual(project['items'][0]['link']['basis'], 'CANDIDATE')
        self.assertEqual(project['items'][0]['link']['archived_at'], 'fictional-link-date')
        self.assertEqual(project['items'][0]['record']['archived_at'], 'fictional-date')
        topic = list_workspace_members(self.conn, 'topic', 't')
        self.assertEqual(topic['items'][0]['link']['status'], 'INHERITED')
        self.assertEqual(list_workspace_members(self.conn, 'project', 'missing')['error']['code'], 'ITEM_NOT_FOUND')
        self.assertEqual(list_workspace_members(self.conn, 'topic', 't', offset=-1)['error']['code'], 'INVALID_REQUEST')


if __name__ == '__main__':
    unittest.main()
