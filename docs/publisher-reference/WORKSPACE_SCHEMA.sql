-- Source-only DDL extracted from SQL in upgrade_to_workspace.py.
-- Describes the already-created local editable workspace; contains no rows.
-- Not an automatic migration to run on the remote operational dataset.
-- Requires the pre-existing B conversation/message/message_version tables for
-- the final legacy-status view. Publishing must not invent missing source data.

PRAGMA foreign_keys=ON;
CREATE TABLE work_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE work_records(
 record_id TEXT PRIMARY KEY,
 title TEXT NOT NULL CHECK(length(title)>0),
 original_title TEXT,
 body TEXT NOT NULL,
 body_sha256 TEXT NOT NULL CHECK(length(body_sha256)=64),
 summary TEXT,
 document_role TEXT NOT NULL,
 original_layer TEXT,
 review_status TEXT NOT NULL,
 adoption_status TEXT NOT NULL,
 decision_status TEXT NOT NULL DEFAULT 'UNREVIEWED',
 source_kind TEXT NOT NULL DEFAULT 'B_LOCAL_REORGANIZED_PREVIEW',
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 archived_at TEXT,
 merged_into TEXT REFERENCES work_records(record_id) CHECK(merged_into IS NULL OR merged_into!=record_id)
);
CREATE TABLE work_revisions(
 record_id TEXT NOT NULL REFERENCES work_records(record_id),
 version INTEGER NOT NULL CHECK(version>=1),
 title TEXT NOT NULL, body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
 summary TEXT, review_status TEXT NOT NULL, adoption_status TEXT NOT NULL,
 decision_status TEXT NOT NULL, archived_at TEXT, merged_into TEXT,
 event_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(record_id,version)
);
CREATE TABLE work_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE,
 event_kind TEXT NOT NULL,
 record_id TEXT REFERENCES work_records(record_id),
 record_version INTEGER,
 actor TEXT NOT NULL CHECK(actor IN ('USER','ASSISTANT','SYSTEM')),
 authorization_basis TEXT,
 reason TEXT NOT NULL,
 detail_json TEXT NOT NULL,
 occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 preceding_hash TEXT,
 event_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE work_projects(
 project_id TEXT PRIMARY KEY,
 name TEXT NOT NULL,
 state TEXT,
 provenance TEXT NOT NULL DEFAULT 'B_REORG_PROJECT'
);
CREATE TABLE work_record_projects(
 link_id TEXT PRIMARY KEY,
 record_id TEXT NOT NULL REFERENCES work_records(record_id),
 project_id TEXT NOT NULL REFERENCES work_projects(project_id),
 role TEXT NOT NULL, scope TEXT,
 basis TEXT NOT NULL CHECK(basis IN ('INHERITED_CLASSIFICATION_ONLY','USER_CONFIRMED','CANDIDATE')),
 original_evidence_id TEXT,
 added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 archived_at TEXT,
 UNIQUE(record_id,project_id,role,scope)
);
CREATE TABLE work_sources(
 evidence_id TEXT PRIMARY KEY,
 record_id TEXT NOT NULL REFERENCES work_records(record_id),
 source_id TEXT,
 locator TEXT,
 offset_unit TEXT,
 validated_in_this_workspace INTEGER NOT NULL DEFAULT 0 CHECK(validated_in_this_workspace IN (0,1))
);
CREATE TABLE work_topics(
 topic_id TEXT PRIMARY KEY,
 name TEXT NOT NULL,
 parent_id TEXT REFERENCES work_topics(topic_id),
 original_axis TEXT,
 notes TEXT,
 provenance TEXT NOT NULL DEFAULT 'B_TOPIC'
);
CREATE TABLE work_record_topics(
 record_id TEXT NOT NULL REFERENCES work_records(record_id),
 topic_id TEXT NOT NULL REFERENCES work_topics(topic_id),
 status TEXT NOT NULL CHECK(status IN ('INHERITED','CANDIDATE','USER_CONFIRMED')),
 PRIMARY KEY(record_id,topic_id)
);
CREATE TABLE work_tags(tag_id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE work_record_tags(
 record_id TEXT NOT NULL REFERENCES work_records(record_id),
 tag_id TEXT NOT NULL REFERENCES work_tags(tag_id),
 PRIMARY KEY(record_id,tag_id)
);
CREATE TABLE work_notes(
 note_id TEXT PRIMARY KEY,
 record_id TEXT REFERENCES work_records(record_id),
 topic_id TEXT REFERENCES work_topics(topic_id),
 author TEXT NOT NULL CHECK(author IN ('USER','ASSISTANT')),
 note_text TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'DRAFT',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CHECK(record_id IS NOT NULL OR topic_id IS NOT NULL)
);
CREATE TABLE work_decisions(
 decision_id TEXT PRIMARY KEY,
 record_id TEXT NOT NULL REFERENCES work_records(record_id),
 decision TEXT NOT NULL CHECK(decision IN ('CONFIRMED','DEFERRED','REJECTED','RETRACTED','UNRESOLVED')),
 explanation TEXT,
 actor TEXT NOT NULL CHECK(actor IN ('USER','ASSISTANT')),
 user_authorized INTEGER NOT NULL DEFAULT 0 CHECK(user_authorized IN (0,1)),
 evidence_message_ref TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE work_proposals(
 proposal_id TEXT PRIMARY KEY,
 record_id TEXT REFERENCES work_records(record_id),
 proposed_action TEXT NOT NULL,
 proposal_json TEXT NOT NULL,
 proposer TEXT NOT NULL CHECK(proposer IN ('USER','ASSISTANT')),
 state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','ACCEPTED','REJECTED','SUPERSEDED')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 resolved_at TEXT
);
CREATE INDEX work_records_review_idx ON work_records(review_status,record_id);
CREATE INDEX work_records_role_idx ON work_records(document_role,record_id);
CREATE INDEX work_records_archive_idx ON work_records(archived_at,record_id);
CREATE INDEX work_rev_by_record_idx ON work_revisions(record_id,version DESC);
CREATE INDEX work_events_by_record_idx ON work_events(record_id,sequence DESC);
CREATE INDEX work_links_by_project_idx ON work_record_projects(project_id,record_id);
CREATE INDEX work_sources_record_idx ON work_sources(record_id);
CREATE INDEX work_note_record_idx ON work_notes(record_id);
CREATE INDEX work_proposal_state_idx ON work_proposals(state,record_id);
CREATE VIRTUAL TABLE work_fts USING fts5(record_id UNINDEXED,title,body, tokenize='unicode61');
CREATE TRIGGER work_fts_insert AFTER INSERT ON work_records BEGIN
 INSERT INTO work_fts(record_id,title,body) VALUES(new.record_id,new.title,new.body);
END;
CREATE TRIGGER work_fts_update AFTER UPDATE OF title,body ON work_records BEGIN
 DELETE FROM work_fts WHERE record_id=old.record_id;
 INSERT INTO work_fts(record_id,title,body) VALUES(new.record_id,new.title,new.body);
END;
CREATE TRIGGER work_fts_delete AFTER DELETE ON work_records BEGIN
 DELETE FROM work_fts WHERE record_id=old.record_id;
END;
CREATE VIEW work_human_records AS
 SELECT record_id,title,summary,document_role,review_status,adoption_status,decision_status,
 version,archived_at,merged_into,created_at,updated_at
 FROM work_records;
CREATE VIEW work_human_dashboard AS
 SELECT r.record_id,r.title,r.summary,substr(r.body,1,240) AS body_preview,
 r.document_role,r.review_status,r.decision_status,r.version,r.archived_at,
 (SELECT group_concat(p.name,' / ') FROM work_record_projects l
  JOIN work_projects p ON p.project_id=l.project_id
  WHERE l.record_id=r.record_id AND l.archived_at IS NULL) AS project_names,
 (SELECT group_concat(t.name,', ') FROM work_record_tags x
  JOIN work_tags t ON t.tag_id=x.tag_id WHERE x.record_id=r.record_id) AS tags
 FROM work_records r;
CREATE VIEW work_legacy_conversation_status AS
 SELECT c.id AS conversation_id,c.title,c.provider,c.kind,
 COUNT(DISTINCT mv.id) AS message_versions,
 SUM(CASE WHEN mv.role NOT IN ('user','assistant','USER','ASSISTANT') THEN 1 ELSE 0 END) AS roles_not_verified
 FROM conversation c
 LEFT JOIN message m ON m.conversation_id=c.id
 LEFT JOIN message_version mv ON mv.message_id=m.id
 GROUP BY c.id;
