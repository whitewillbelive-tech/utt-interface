# UTT workspace write source & `work_events` chain — exact source-only handoff

This document describes **existing behavior**, not a replacement format or authorization to upgrade historical data. It accompanies [`utt_workspace.py`](./utt_workspace.py), a source-only extraction of original lines 1–273 from the prior integrated-workspace package. The original CLI entrypoint (lines 274–321) is intentionally excluded. No SQLite records, note text, actual event rows, account paths, DB files, credentials, or production hashes are supplied.

## Provenance and original source

- Inspected source: `UTT_Integrated_Writable_DB.zip` → `utt_workspace.py`, a real previous generated UTT integrated-workspace package.
- Original complete `utt_workspace.py` SHA-256: `c5d1e075321aa7d256791e945166ea0f8b3e70a6b027807dca435679605fc370`.
- Source-only excerpt (lines 1–273, with its original trailing blank line) SHA-256: `0f5aa42ed12fa5c88f0595111a46f9732ff84acdb60be0c92585118703429780`.
- The associated `upgrade_to_workspace.py` original SHA-256 is `0952ca09481ef6e005d4b66a21750ca89036cbdfae9dedf491833cd681c25dd2`; only the original seed rule below is reproduced.
- Schema is already present in this PR as `WORKSPACE_SCHEMA.sql`. The source excerpt is not a production-deployed server/API, and the original helper performs no Dropbox operations.

## Exact event hash calculation (original `utt_workspace.py`)

The existing `textjson(x)` uses **Python**:
```python
def sha(x): return hashlib.sha256(x.encode('utf-8')).hexdigest()
def textjson(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
```

For every **new non-import event** `event(c, kind, record, version, actor, reason, details, authorized='EXPLICIT_WORKSPACE_WRITE')`:

1. Read the latest row with `SELECT event_hash FROM work_events ORDER BY sequence DESC LIMIT 1`; this is `prevhash`, or `None` for an empty chain.
2. Generate `id = 'evt-' + uuid.uuid4().hex` and a UTC ISO-8601 timestamp from `now()`.
3. Construct **exactly** this payload (field names intentionally differ from the SQL column names):
   ```python
   payload = dict(id=id, kind=kind, record_id=record, version=version,
                  actor=actor, reason=reason, details=details,
                  predecessor=prevhash, at=now())
   digest = sha(textjson(payload))
   ```
4. Insert a `work_events` row with `event_id=id`, `event_kind=kind`, `record_id=record`, `record_version=version`, `actor=actor`, `authorization_basis=authorized`, `reason=reason`, `detail_json=textjson(details)`, `occurred_at=payload['at']`, `preceding_hash=prevhash`, `event_hash=digest`.
5. The call is inside the **same `BEGIN IMMEDIATE` transaction** as any modification to `work_records`, `work_revisions` and `work_meta.data_revision`; success commits together and failures roll back. Tag, project-link, and note operations append an event and bump `data_revision` without incrementing a record's `version`.

Consequences: `None` is JSON `null`; JSON keys are sorted lexicographically by Python `json.dumps`; `ensure_ascii=False` keeps Unicode; separators contain no added spaces; hash input is exactly UTF-8 JSON bytes with **no trailing newline**. Preserve list order. For a field that is conceptually absent, do not invent an empty string in place of the original `None`.

The original hash does **not** include the `authorization_basis` column. This is an observed limitation of the existing hash domain. Do not silently change the hash formula or relabel old events as newly verified. Enforce authorization separately when implementing a new API.

## Special initial `IMPORT` seed — not an ordinary event hash

The original `upgrade_to_workspace.py` creates exactly one initial event after importing B's existing rows. It is **not** a fabricated user confirmation for every record:

```python
c.execute('''INSERT INTO work_events(event_id,event_kind,record_id,record_version,actor,
     authorization_basis,reason,detail_json,preceding_hash,event_hash)
     VALUES(?,?,?,?,?,?,?,?,?,?)''',(
      'initial-historical-b-import','IMPORT',None,None,'SYSTEM','USER_DIRECTED_DB_UPGRADE',
      'Initialize writable DB from actual historical B, not operating Dropbox approval',
      json.dumps({'records':count,'legacy_source':'B_LOCAL_REORGANIZED_PREVIEW'},ensure_ascii=False),None,
      hashlib.sha256(b'initial-historical-b-import|UTT-INTEGRATED-WORKSPACE/0.1').hexdigest()
     ))
```

The first event's `event_hash` is therefore **the SHA-256 of the exact ASCII literal**
`initial-historical-b-import|UTT-INTEGRATED-WORKSPACE/0.1`,
not `sha(textjson(payload))` and not a digest of imported row data. It also has `preceding_hash=NULL`. Preserve the existing row and seed digest as they are; do not recalculate it using the later-event formula.

## Existing `verify(c)` chain validation

Iterate `SELECT * FROM work_events ORDER BY sequence`, starting with `prev=None`:

- For every event, compare `e['preceding_hash']` **exactly** to the previously visited event's `event_hash`.
- If `i == 0 and e['event_kind'] == 'IMPORT'`, compute `expected=sha('initial-historical-b-import|UTT-INTEGRATED-WORKSPACE/0.1')`.
- Otherwise reconstruct `p = {'id': e['event_id'], 'kind': e['event_kind'], 'record_id': e['record_id'], 'version': e['record_version'], 'actor': e['actor'], 'reason': e['reason'], 'details': json.loads(e['detail_json']), 'predecessor': e['preceding_hash'], 'at': e['occurred_at']}`, then compute `expected=sha(textjson(p))`.
- Compare `expected == e['event_hash']`, increment error count for any mismatch, and advance `prev=e['event_hash']`. Report `EVENT_CHAIN` if at least one comparison fails.

The original `verify(c)` also checks `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, current work-record body SHA-256s, correspondence to the latest record revision (title and body hash), the FTS item count, and orphan project links. Do not represent those checks as a semantic approval audit. The test does not prove that the historical event rows are author-authorized, nor that an old chain has never been rewritten consistently; its special import branch also does not independently validate every field in the seed row.

## Original write semantics and version boundaries

| Existing function | State updated and event |
| --- | --- |
| `create` | New `work_records` row, `CREATE` event, version 1 revision, global data revision bump |
| `edit` | Require exact `expected_version`; update title/body/summary/review, `EDIT` event, next revision, bump |
| `state_change` | Require exact version; `ARCHIVE`, `RESTORE`, `MERGE`, `UNMERGE`; record version + revision + event |
| `undo` | Restore fields from a selected historical revision as a **new** version; `RESTORE_REVISION` event |
| `attach_tag`, `link_project`, `add_note` | Insert link/tag/note, event (`TAG`, `PROJECT_LINK`, `NOTE`), bump global revision |
| `decide` | `USER` actor only in original helper; append decision + `DECISION` event and next record revision |

**Caution for integrating into UI/API:** The original function argument `actor='USER'` is caller-provided and does not itself authenticate a human. In particular `link_project` writes `USER_CONFIRMED` if passed `actor='USER'`; a new service must bind this to an authenticated direct user action rather than treating a caller-supplied string as approval. Do not automatically promote historical `B_LOCAL_REORGANIZED_PREVIEW` to an operating approval.

Use the exact existing writer and hash/seed logic when continuing a preexisting workspace's event history. Add new behavior in a separately specified contract if required, without altering historical hash bytes or conflating this workspace chain with the legacy `utt_event` chain in the preserved A/B tables.

## Handoff scope

Source-only documentation and Python functions; no transfer or processing of actual user records, Dropbox source bytes, local paths, tokens, original B ZIP or SQLite data. Use this material in **PR #16**, then implement and validate an actual write/read-publish flow. A source-only reference commit is not an operational DB change.
