import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash, webcrypto} from 'node:crypto';
import {ReadClient} from '../../app/web/client.mjs';
import {validateSourceResponse} from '../../app/web/response-validation.mjs';

// Every identity, body and hash here is fictional and generated in this file.
const VERSION = 'UTT-SOURCE-READ/0.1.0';
const identity = Object.freeze({
  dataset_id: 'fictional-dataset', snapshot_id: 'fictional-snapshot',
  record_id: 'fictional-record', source_id: 'fictional-source-a',
  source_version: 'fictional-metadata-v2',
});
const request = Object.freeze({contract_version: VERSION, ...identity});
const recordText = 'Fictional record body; this is not the independent source body.';
const sourceText = 'Fictional source 🧭 excerpt.\n별도 합성 출처 본문.';
const hash = value => createHash('sha256').update(value, 'utf8').digest('hex');
const verification = (status, reason) => ({status, reason});
const reply = envelope => ({ok: true, json: async () => envelope});
const clone = value => structuredClone(value);

function fixture(values = identity, {text = sourceText, kind = 'SOURCE_BODY'} = {}) {
  const start = text.indexOf('🧭') < 0 ? 0 : text.indexOf('🧭');
  const end = text.indexOf('🧭') < 0 ? text.length : start + '🧭'.length;
  return {
    contract_version: VERSION, status: 'OK', error: null,
    result: {
      ...values,
      source: {
        source_id: values.source_id, dataset_id: values.dataset_id,
        availability: 'AVAILABLE', version: values.source_version,
        // An original container/file hash is deliberately not the text hash.
        content_sha256: hash('Fictional original container bytes.'),
        location: {
          target: kind, unit: 'UTF-16', start, end, boundary: 'half_open',
          target_sha256: hash(text), fragment_sha256: hash(text.slice(start, end)),
          verification: 'VERIFIED',
        },
        verification: 'VERIFIED',
      },
      content: {
        availability: 'AVAILABLE', target_kind: kind,
        target_id: kind === 'SOURCE_BODY' ? values.source_id : values.record_id,
        target_version: 'fictional-body-v7', locator_target_version: 'fictional-body-v7',
        text, text_sha256: hash(text),
      },
      locator_verification: verification('VERIFIED', 'HASH_AND_RANGE_MATCH'),
    },
  };
}

function unavailable(availability) {
  const envelope = fixture();
  envelope.result.source.availability = availability === 'NOT_SUPPORTED' ? 'AVAILABLE' : availability;
  envelope.result.source.location = null;
  envelope.result.content = {
    availability, target_kind: null, target_id: null, target_version: null,
    locator_target_version: null, text: null, text_sha256: null,
  };
  envelope.result.locator_verification = verification('NOT_AVAILABLE', 'CONTENT_UNAVAILABLE');
  return envelope;
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

test('source POST uses only the six exact contract fields and preserves the independent body', async () => {
  const envelope = fixture();
  const controller = new AbortController();
  const calls = [];
  const client = new ReadClient(async (path, options) => {
    calls.push({path, options});
    return reply(envelope);
  });
  // Source identity comes from detail, even if the mutable search client moved.
  client.dataset = {dataset_id: 'fictional-other-dataset', snapshot_id: 'fictional-other-snapshot'};
  client.scenario = 'not-a-source-contract-field';
  const result = await client.readSource(identity, controller.signal);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/source');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(calls[0].options.body), request);
  assert.deepEqual(Object.keys(result).sort(), ['highlight', 'response']);
  assert.deepEqual(result.response, envelope);
  assert.equal(result.response.result.content.text, sourceText);
  assert.notEqual(result.response.result.content.text, recordText);
  assert.deepEqual(result.highlight, {start: sourceText.indexOf('🧭'), end: sourceText.indexOf('🧭') + 2});
});

test('unknown or empty source version prevents any body request', async () => {
  let calls = 0;
  const client = new ReadClient(async () => { calls++; return reply(fixture()); });
  for (const source_version of [null, undefined, '']) {
    await assert.rejects(client.readSource({...identity, source_version}));
  }
  assert.equal(calls, 0);
});

test('metadata and target versions and file and text hashes are independently valid', async () => {
  const envelope = fixture();
  const {source, content} = envelope.result;
  assert.notEqual(source.version, content.target_version);
  assert.notEqual(source.content_sha256, content.text_sha256);
  assert.deepEqual(await validateSourceResponse(envelope, request, webcrypto),
    {start: source.location.start, end: source.location.end});
  source.verification = 'UNKNOWN';
  source.location.verification = 'UNVERIFIED';
  assert.deepEqual(await validateSourceResponse(envelope, request, webcrypto),
    {start: source.location.start, end: source.location.end});
});

test('SOURCE_BODY and RECORD_BODY bind to their own target IDs', async () => {
  for (const kind of ['SOURCE_BODY', 'RECORD_BODY']) {
    const envelope = fixture(identity, {kind});
    assert.ok(await validateSourceResponse(envelope, request, webcrypto));
    envelope.result.content.target_id = kind === 'SOURCE_BODY' ? identity.record_id : identity.source_id;
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto));
  }
  const syntheticTarget = fixture();
  syntheticTarget.result.source.location.target = 'SYNTHETIC_SOURCE';
  assert.ok(await validateSourceResponse(syntheticTarget, request, webcrypto));
});

test('AVAILABLE empty string remains an available verified empty body', async () => {
  const envelope = fixture(identity, {text: ''});
  const client = new ReadClient(async () => reply(envelope));
  const result = await client.readSource(identity);
  assert.equal(result.response.result.content.availability, 'AVAILABLE');
  assert.equal(result.response.result.content.text, '');
  assert.deepEqual(result.highlight, {start: 0, end: 0});
});

test('all unavailable body statuses remain distinct and have no fallback or highlight', async () => {
  for (const availability of ['MISSING', 'UNKNOWN', 'INACCESSIBLE', 'NOT_SUPPORTED']) {
    const envelope = unavailable(availability);
    const client = new ReadClient(async () => reply(envelope));
    const result = await client.readSource(identity);
    assert.equal(result.response.result.content.availability, availability);
    assert.equal(result.response.result.content.text, null);
    assert.equal(result.highlight, null);
    const withFabricatedBody = clone(envelope);
    withFabricatedBody.result.content.text = recordText;
    await assert.rejects(validateSourceResponse(withFabricatedBody, request, webcrypto));
  }
});

test('each echoed identity and nested source scope is checked against the exact request', async () => {
  for (const field of Object.keys(identity)) {
    const envelope = fixture();
    envelope.result[field] = `other-${identity[field]}`;
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto), field);
  }
  for (const field of ['dataset_id', 'source_id', 'version']) {
    const envelope = fixture();
    envelope.result.source[field] = 'fictional-unrelated-value';
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto), `source.${field}`);
  }
});

test('malformed envelope, content, verification and extra wire fields are rejected', async () => {
  const changes = [
    value => { value.contract_version = 'UTT-ACCESS-READ/0.1.0'; },
    value => { value.result = null; },
    value => { value.error = {code: 'INVALID_REQUEST', message: 'Synthetic error', retryable: false}; },
    value => { value.extra = true; },
    value => { value.result.extra = true; },
    value => { delete value.result.content.text_sha256; },
    value => { value.result.content.text = null; },
    value => { value.result.content.target_kind = null; },
    value => { value.result.content.target_version = ''; },
    value => { value.result.content.text_sha256 = 'not-a-sha256'; },
    value => { value.result.locator_verification.status = 'UNKNOWN'; },
    value => { value.result.locator_verification.reason = 'CONTENT_UNAVAILABLE'; },
    value => { value.result.source.location.boundary = 'closed'; },
    value => { value.result.source.location.start = 0.5; },
  ];
  for (const change of changes) {
    const envelope = fixture();
    change(envelope);
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto));
  }
});

test('body hash verifies the exact UTF-8 text even when location verification is unavailable', async () => {
  for (const text of [sourceText + 'changed', sourceText.normalize('NFD')]) {
    const envelope = fixture();
    envelope.result.content.text = text;
    envelope.result.source.location = null;
    envelope.result.locator_verification = verification('UNVERIFIED', 'LOCATION_NOT_PROVIDED');
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto));
  }
});

test('unconfirmed or mismatching locator versions retain the body without highlighting', async () => {
  for (const [locatorVersion, status, reason] of [
    [null, 'UNVERIFIED', 'TARGET_VERSION_UNCONFIRMED'],
    ['fictional-older-body-v3', 'MISMATCH', 'TARGET_VERSION_MISMATCH'],
  ]) {
    const envelope = fixture();
    envelope.result.content.locator_target_version = locatorVersion;
    envelope.result.locator_verification = verification(status, reason);
    const client = new ReadClient(async () => reply(envelope));
    const result = await client.readSource(identity);
    assert.equal(result.response.result.content.text, sourceText);
    assert.equal(result.highlight, null);
    envelope.result.locator_verification = verification('VERIFIED', 'HASH_AND_RANGE_MATCH');
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto));
  }
});

test('missing locator, missing hash, target hash mismatch and fragment mismatch never highlight', async () => {
  const cases = [
    ['UNVERIFIED', 'LOCATION_NOT_PROVIDED', value => { value.source.location = null; }],
    ['UNVERIFIED', 'HASH_NOT_PROVIDED', value => { value.source.location.fragment_sha256 = null; }],
    ['MISMATCH', 'TARGET_HASH_MISMATCH', value => { value.source.location.target_sha256 = hash('Other synthetic target'); }],
    ['MISMATCH', 'FRAGMENT_HASH_MISMATCH', value => { value.source.location.fragment_sha256 = hash('Other synthetic fragment'); }],
  ];
  for (const [status, reason, mutate] of cases) {
    const envelope = fixture();
    mutate(envelope.result);
    envelope.result.locator_verification = verification(status, reason);
    assert.equal(await validateSourceResponse(envelope, request, webcrypto), null);
    envelope.result.locator_verification = verification('VERIFIED', 'HASH_AND_RANGE_MATCH');
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto));
  }
});

test('byte, code point and unsupported targets stay unsupported without coordinate conversion', async () => {
  for (const unit of ['byte', 'unicode_code_point']) {
    const envelope = fixture();
    envelope.result.source.location.unit = unit;
    envelope.result.locator_verification = verification('NOT_SUPPORTED', 'UNIT_NOT_SUPPORTED');
    assert.equal(await validateSourceResponse(envelope, request, webcrypto), null);
    envelope.result.locator_verification = verification('VERIFIED', 'HASH_AND_RANGE_MATCH');
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto));
  }
  const envelope = fixture();
  envelope.result.source.location.target = 'FICTIONAL_OTHER_TARGET';
  envelope.result.locator_verification = verification('NOT_SUPPORTED', 'TARGET_NOT_SUPPORTED');
  assert.equal(await validateSourceResponse(envelope, request, webcrypto), null);
  envelope.result.locator_verification = verification('VERIFIED', 'HASH_AND_RANGE_MATCH');
  await assert.rejects(validateSourceResponse(envelope, request, webcrypto));
});

test('UTF-16 boundaries reject both split emoji endpoints and the empty range inside a surrogate pair', async () => {
  const emoji = sourceText.indexOf('🧭');
  for (const [start, end] of [
    [emoji + 1, emoji + 2], [emoji, emoji + 1], [emoji + 1, emoji + 1],
    [sourceText.length + 1, sourceText.length + 1], [emoji + 2, emoji],
  ]) {
    const envelope = fixture();
    Object.assign(envelope.result.source.location, {
      start, end, fragment_sha256: hash(sourceText.slice(start, end)),
    });
    // A server bug can claim VERIFIED for the empty slice inside the emoji.
    await assert.rejects(validateSourceResponse(envelope, request, webcrypto), `${start}:${end}`);
    envelope.result.locator_verification = verification('MISMATCH', 'INVALID_RANGE');
    assert.equal(await validateSourceResponse(envelope, request, webcrypto), null);
  }
  for (const position of [emoji, emoji + 2, sourceText.length]) {
    const envelope = fixture();
    Object.assign(envelope.result.source.location, {start: position, end: position, fragment_sha256: hash('')});
    assert.deepEqual(await validateSourceResponse(envelope, request, webcrypto), {start: position, end: position});
  }
});

test('source error envelopes remain failures including version and connection mismatches', async () => {
  for (const code of [
    'ITEM_NOT_FOUND', 'SOURCE_MISSING', 'CONNECTION_UNAVAILABLE', 'VERSION_MISMATCH',
    'UNSUPPORTED_CONTRACT', 'INVALID_REQUEST', 'INTERNAL_ERROR', 'NOT_SUPPORTED',
  ]) {
    const envelope = {contract_version: VERSION, status: 'ERROR', result: null,
      error: {code, message: 'Synthetic service error.', retryable: code === 'CONNECTION_UNAVAILABLE'}};
    const client = new ReadClient(async () => reply(envelope));
    await assert.rejects(client.readSource(identity), error => error.code === code);
  }
});

test('HTTP access errors, invalid JSON and network failure do not become available empty content', async () => {
  for (const [status, code] of [[401, 'AUTHENTICATION_REQUIRED'], [403, 'ACCESS_DENIED'], [503, 'CONNECTION_UNAVAILABLE']]) {
    const client = new ReadClient(async () => ({ok: false, status}));
    await assert.rejects(client.readSource(identity), error => error.code === code);
  }
  const invalidJson = new ReadClient(async () => ({ok: true, json: async () => { throw new SyntaxError('Synthetic invalid JSON'); }}));
  await assert.rejects(invalidJson.readSource(identity), error => error.code === 'INVALID_RESPONSE');
  const disconnected = new ReadClient(async () => { throw new TypeError('Synthetic offline'); });
  await assert.rejects(disconnected.readSource(identity), error => error.code === 'CONNECTION_UNAVAILABLE');
  const aborted = new ReadClient(async () => { throw new DOMException('Synthetic cancellation', 'AbortError'); });
  await assert.rejects(aborted.readSource(identity), error => error.name === 'AbortError');
});

test('concurrent source reads pin independent identities and tolerate reverse response order', async () => {
  const firstIdentity = {...identity};
  const secondIdentity = {...identity, source_id: 'fictional-source-b', source_version: 'fictional-metadata-v5'};
  const firstEnvelope = fixture();
  const secondEnvelope = fixture(secondIdentity, {text: 'Second independent fictional source body.'});
  const enteredA = deferred(), enteredB = deferred(), responseA = deferred(), responseB = deferred();
  const client = new ReadClient(async (_path, options) => {
    const sent = JSON.parse(options.body);
    if (sent.source_id === identity.source_id) {
      enteredA.resolve(sent);
      return responseA.promise;
    }
    enteredB.resolve(sent);
    return responseB.promise;
  });
  const first = client.readSource(firstIdentity);
  assert.deepEqual(await enteredA.promise, request);
  const second = client.readSource(secondIdentity);
  assert.deepEqual(await enteredB.promise, {contract_version: VERSION, ...secondIdentity});
  firstIdentity.dataset_id = 'fictional-navigation-after-request';
  client.dataset = {dataset_id: 'fictional-navigation-dataset', snapshot_id: 'fictional-navigation-snapshot'};
  responseB.resolve(reply(secondEnvelope));
  const secondResult = await second;
  assert.equal(secondResult.response.result.source_id, secondIdentity.source_id);
  responseA.resolve(reply(firstEnvelope));
  const firstResult = await first;
  assert.equal(firstResult.response.result.source_id, identity.source_id);
  assert.equal(firstResult.response.result.dataset_id, identity.dataset_id);
  assert.equal(secondResult.response.result.content.text, secondEnvelope.result.content.text);
});
