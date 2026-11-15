# PR #16: native publication format, actual preservation evidence, and implementation direction

## Why this document exists

The user has repeatedly authorized modifying the **existing Dropbox operational UTT dataset**, not merely producing a local ZIP or a read-only demo. The management conversation is supplying the missing source/format/evidence directly to this PR branch so backend implementation no longer depends on opening a private Library ZIP.

Read `LEGACY_B2_SOURCE.md` alongside this document. It contains the extracted native publisher modules. No real database rows, source bodies, account identifiers, token values, operational generation hashes, or local user paths are included in this handoff. Logical filenames below are application protocol names. Actual input DB and credentials stay on the user's Windows PC.

This document clearly separates (a) observed legacy implementation, (b) results of actual local data comparisons, and (c) requirements for the new publisher. It is not a claim that remote publication has occurred.

## A. Observed native format and exact byte semantics

Source examined directly: existing B-2 uploader ZIP, original A reader code in the A performance ZIP, and the editable-workspace package's Python migration source. The historical production catalog/config/pins are deliberately NOT copied.

### Generation layout

The legacy App Folder-relative control path is `/system/active-baseline.json`.

A generation's logical root is `/snapshots/baselines/<generation_id>/`. Here `generation_id` equals the ordinary SHA-256 of the exact stored `system/manifest.json` bytes. The root in the active reference ends in `/`. This is an application contract; do not infer arbitrary paths or substitute a full Dropbox account path.

`system/manifest.json` has fields:

```text
format: "utt-baseline/1"
baseline_id: nonempty identity shared by manifest, COMMIT and active reference
files: array of { path: generation-relative path, bytes: integer, sha256: ordinary SHA-256 }
```

The manifest includes payload entries, including `system/baseline.json`, `system/schema.json`, table/object catalogs and shards, source assets and any declared backup. It excludes `system/manifest.json` itself and `system/COMMIT.json`, avoiding a circular digest.

`system/COMMIT.json` has fields:

```text
format: "utt-baseline/1"
baseline_id: same baseline identity
manifest_sha256: SHA-256 of exact stored manifest bytes
state: "complete"
```

The local approved catalog has:

```text
count: total number of generation files
bytes: sum of byte lengths of every generation file
manifest_sha256: SHA-256 of exact manifest bytes
files: array of { path, bytes, sha256 }
```

Unlike the manifest, this catalog includes the manifest AND COMMIT. Therefore `catalog.count = manifest.files.length + 2`. The legacy source hard-codes a particular catalog, count and byte total. The new publisher must derive and verify them from the selected publication candidate, not reuse the historical constants.

`/system/active-baseline.json` has exactly the legacy publication fields:

```text
format: "utt-active-baseline/1"
generation_id: manifest SHA-256
manifest_sha256: same manifest SHA-256
baseline_id: identity from candidate COMMIT
root: "/snapshots/baselines/<generation_id>/"
commit_sha256: SHA-256 of exact COMMIT bytes
files: complete catalog count, including manifest and COMMIT
bytes: complete catalog byte sum
```

The native activation encoder is `canonical(object) + "\n"`, UTF-8. `canonical()` recursively sorts object keys, preserves array order, uses JSON string escaping, and allows only safe integers as JSON numbers. Verify the **received bytes** without reserializing them before hashing. A publisher may generate canonical bytes, but a validator must compare against the original bytes.

Dropbox `content_hash` is not the ordinary file SHA-256. Keep those algorithms/fields separate. A generation identifier, manifest file digest, and remote Dropbox revision are also different concepts.

### Legacy source data layout

`system/baseline.json` includes `format`, `baseline_id`, `snapshot_kind`, `conversion_schema`, `conversion_timestamp_policy`, `source_db_sha256`, `source_zip_sha256`, `tables`, `sources`.

`snapshots/tables.json` is an array of descriptors with `name`, `columns`, `count`, `rowid`, `foreign_keys`, `sql`, `rows_sha256`, and `shards`. A shard descriptor contains `path`, `bytes`, `raw_bytes`, `rows`, `sha256`, `raw_sha256`. Shards are gzip-compressed JSONL; validate compressed and decompressed bytes separately.

Native table rows are arrays of typed two-element cells, with an initial rowid cell when `rowid=true`. Observed type tags are `null`, `integer`, `text`, `real`, `blob`. `integer` carries a string, so do not round large integers through JavaScript Number. Do not relabel a source string as a different native type. Use the actual schema/converter semantics for blob/real decoding rather than treating display text as proof of raw-byte equality.

`snapshots/objects.json` is an array of object-shard descriptors. `sources/index.json` describes source assets; object/table records and source asset bodies are distinct. `ledger/checkpoint.json` identifies retained historical event/decision/audit tables and a new-event frontier. A baseline import is not itself a historical user decision.

### Exact legacy publication sequence

1. Recheck all selected local payload bytes against the bound catalog before any network operation.
2. For a deterministic generation destination, reuse an existing file only after exact size+ordinary-SHA verification. A different payload at the same destination is a conflict.
3. If the generation already has a matching COMMIT, a missing payload is `COMMITTED_GENERATION_DAMAGED`, not permission to repair blindly.
4. Upload payloads and manifest with `add`, `autorename=false`, `strict_conflict=true`; independently download/reverify each expected object.
5. Complete a full remote verification pass, then publish and re-read COMMIT.
6. Read current active-reference bytes and Dropbox `rev`. Existing equal target bytes mean an idempotent success.
7. Replace a different active reference only using a plan bound to the observed previous bytes and revision. The legacy write mode is `{ ".tag": "update", "update": observed_rev }`, never blind overwrite. Preserve conflicts rather than automatically switching to a new revision.
8. Read active reference again and compare exact desired bytes. A lost upload response requires read-back reconciliation, not a blind retry.
9. If publication was paused for a material interval, revalidate the candidate generation before activation.

The original `Uploader.report()` includes paths/hashes. Those diagnostics belong locally; do not paste them into Codex, GitHub or this management conversation.

## B. Actual A → B → editable-workspace preservation evidence

The management runtime performed a NEW read-only comparison during this handoff, using the already-present actual source archives. The backend did not receive these DBs. This is real local data evidence, **not** a fresh remote Dropbox check.

The A SQLite bytes matched the `source_db_sha256` recorded in the historical B-2 candidate's `system/baseline.json`. The table comparisons used multisets of type-preserving complete rows: null/integer/real/text/blob are distinguished, and duplicate occurrences are counted. User table contents and row digests are omitted here.

| Comparison | Scope | Result |
| --- | --- | --- |
| A → B | 73 non-`sqlite_%` tables, including FTS shadow tables; 91,285 row occurrences checked | No missing table. 71 tables identical. Two changed FTS implementation tables: `item_fts_data` and `item_fts_idx`. |
| B → editable workspace | All 97 B non-`sqlite_%` tables; 181,100 row occurrences checked | All 97 tables identical; no missing or changed B table. |
| A/B/workspace | SQLite integrity check | `ok` for each. |
| A/B/workspace | SQLite foreign-key check | Zero errors for each. |

The two A→B differences are:

```text
item_fts_data: A 501 rows, B 519 rows
item_fts_idx:  A 498 rows, B 517 rows
```

These are FTS internal structures. They must not be treated as lost user records, and their physical equality is not required when validating the preserved logical search content and rebuilding indexes. The other A tables, including the logical FTS table, historical records, decisions, events and audit rows, compared equal.

Counts useful for diagnostics, not new hard-coded publication requirements:

```text
utt_object:       4,168 preserved historical A objects
utt_revision:     4,168 preserved historical A revisions
utt_event:       10,426 preserved historical A events
audit_log:       10,733 preserved historical audit rows
message_version:  1,216 preserved message versions
conclusion:          40 retained conclusions
reorg_record:     5,520 B records
work_records:     5,520 editable current records
```

The workspace retains B's native tables and adds editable `work_*` tables. It does NOT replace past approvals with new approvals, and adding records to the usable dataset is not acceptance of every claim in those records.

## C. Non-sensitive mapping and conflict rules

### Current editable authority

For the new workspace, current title/body/summary/state/version/archive/merge status comes from `work_records`. The historical `utt_*`, `reorg_*`, `read_*` and conversation tables are preserved evidence and compatibility inputs; they must not silently overwrite newer work edits.

The actual migration source uses these mappings:

```text
reorg_record.id -> work_records.record_id
read_record.payload_json.title -> work_records.title
  fallback: reorg_record.title, then an explicit missing-title label
reorg_record.title -> work_records.original_title
reorg_record.body -> work_records.body
UTF-8 body SHA-256 -> work_records.body_sha256
read_record.payload_json.display_summary -> work_records.summary
read_record.payload_json.document_role -> work_records.document_role
reorg_record.layer -> work_records.original_layer
reorg_record.review_status/adoption_status -> corresponding work fields
initial work decision_status -> UNREVIEWED (not a historical approval)
reorg_project -> work_projects
reorg_affiliation -> work_record_projects
  basis = INHERITED_CLASSIFICATION_ONLY, not USER_CONFIRMED
reorg_evidence -> work_sources
  validated_in_this_workspace = 0 until independently verified
topic -> work_topics
```

The historical 55 title differences are display title versus original title, not evidence that the current title may be truncated arbitrarily. Both are retained. Unknown message roles stay unknown; provisional conclusions stay provisional. No inferred user/AI role or user confirmation may be invented just to complete publication.

### Preservation and conflict policy for publication

- Publication applies the user's explicit instruction to modify the operational DB. It does not re-request permission to begin implementation, and it does not convert all imported records into canon/user-approved statements.
- Compare the **current remote parent** against the plan at execution time. Historical comparison above is a preflight reference, not a claim that the remote parent has remained unchanged.
- Preserve current remote parent-only records, source assets, approval/event history and user edits, or stop with a specific reconciliation conflict. Never replace a newer parent with the old local package solely because a known historical filename/hash matches.
- Use stable IDs and type-preserving comparisons. Similar titles/text do not authorize merging different records. Exactly identical records may be deduplicated only when identity, provenance and occurrence/relationship semantics also agree.
- If a record differs in the source only, target only, or both, distinguish these cases. A concurrent source+target edit is not automatically settled by newest timestamp or newest file.
- Keep work revisions/events associated with their records. Publication/import events are new system events, not forged historical user decisions.
- Version-conflict detection belongs in real code; unsupported paths must not be silently treated as empty success.
- Non-destructive staging and CAS are a means to change the real operational reference safely, not a perpetual prohibition against modifying the user's DB.

### Minimal workspace input structure

The exact workspace DDL is supplied in `WORKSPACE_SCHEMA.sql` in this directory. The user-supplied package has a SQLite member and Python modules `utt_workspace.py`, `utt_workspace_read_provider.py`, `upgrade_to_workspace.py`, and `verify_workspace_package.py`. The DB remains local to the user; the publisher can inspect its schema using Python sqlite3 without access to the user's content in Codex.

The workspace source metadata includes `format_version=UTT-INTEGRATED-WORKSPACE/0.1`, `dataset_id`, `base_dataset`, `active_dropbox_baseline`, `operational_adoption`, `read_contract`, `write_contract`, `data_revision`, and creation metadata. Historical initial values such as UNKNOWN/UNAPPROVED describe construction time; change publication metadata only after the corresponding operation is verified, not record-level approval states.

## D. Reader compatibility is an implementation obligation, not an unknown legacy format

The original A reader was opened directly. It imports fixed `MANIFEST_HASH`, `CATALOG_HASH`, `ROOT`, `ACTIVE`, `COUNT`, `TOTAL_BYTES`. Its `validateHead()` explicitly compares the active-reference generation/root/count/digests to those pins.

Its `buildModel()` also asserts 69 table descriptors, 86,868 table rows, 4,168 objects, 559 sources, 55 manuscripts and 5,727 segments. These are old release-specific assertions, not universal invariants. Merely changing the remote active-reference will NOT make that unmodified old reader accept a new workspace DB.

Therefore, the publishing work must include a minimal compatible resolver/reader integration, or a checked compatibility handoff before activation. Do not disable integrity checks indiscriminately; replace fixed historical counts/pins with values derived from the validated new generation. Do not relabel a workspace-only SQLite payload as a legacy A payload that the unchanged A loader understands.

The current utt-interface `UTT-ACCESS-READ/0.1.0` contract remains a read API (`writes_supported=false`). A writable SQLite file does not create an HTTP writing API. Workspace commands and actual DB write integration must be explicitly connected; existing successful UI behavior must remain intact. Creating a second active authority or publishing a ZIP that no reader uses is not the user's requested result.

## E. Required continuation of PR #16

Continue the existing PR and branch; do not replace it with another blocked simulator. The original source and preservation rules are now supplied here. Missing private Library access is no longer a reason to stop implementing the uploader.

Implement a real Windows entry point that:

- accepts the user's local workspace DB path (or locally extracts a validated package) without transmitting it to Codex;
- creates a consistent SQLite snapshot using SQLite backup/transaction semantics, not a byte copy of a live WAL database;
- validates schema, integrity, historical preservation and work-state invariants;
- uses the existing registered callback and an explicit publisher PKCE grant with `files.metadata.read`, `files.content.read`, `files.content.write`, separately from reader grants;
- reads and validates the current active-reference/COMMIT/manifest through fixed Dropbox HTTPS endpoints;
- creates a fully bound publication plan for the verified parent and new candidate, with an explicit documented payload format and reader support;
- performs actual bounded upload-session start/append/finish for large objects, reconciles offsets/uncertain outcomes, independently reads back data and verifies bytes;
- publishes COMMIT only after complete verification, switches the active reference with its observed revision, then re-reads the selected generation;
- handles interruption/resumption and concurrent updates without hidden overwrite or data loss;
- gives the user a concrete executable Windows command, not just a hash helper or an unconditional BLOCKED live entry;
- leaves credentials and sensitive diagnostics on the user PC, and retains the already-approved App Folder boundary.

Do not fabricate production evidence in development. Test all live code paths with injected test transports as appropriate, but complete the real transport/CLI implementation too. On the actual run, authentication and local-file selection are normal execution inputs, not missing source contracts. Do not require the user to upload the real DB again.

Completion distinctions: source handoff delivered; executable publishing tool implemented; Windows user execution; remote operational activation verified. Report only steps actually performed. A failed real prerequisite must name the concrete error and affected item, not restart generic approval/permission investigations.
