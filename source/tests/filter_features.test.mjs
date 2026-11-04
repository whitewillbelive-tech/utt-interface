import test from 'node:test';
import assert from 'node:assert/strict';
import {ReadClient} from '../../app/web/client.mjs';

const VERSION = 'UTT-READ-FEATURES/0.1.0';
const FILTERS = ['query', 'project', 'document_role', 'review_status', 'tag', 'source'];
function features(state = 'SUPPORTED') {
  return {contract_version: VERSION, filters: {
    query: state, project: state, document_role: state, review_status: state,
    tag: 'UNSUPPORTED', source: 'UNSUPPORTED',
  }};
}
const reply = value => ({ok: true, status: 200, json: async () => value});

test('features uses a same-origin GET with no extra request contract and returns the original envelope', async () => {
  const envelope = features();
  const controller = new AbortController();
  const calls = [];
  const client = new ReadClient(async (path, options) => {
    calls.push({path, options});
    return reply(envelope);
  });
  assert.equal(await client.readFeatures(controller.signal), envelope);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/features');
  assert.equal(calls[0].options.method ?? 'GET', 'GET');
  assert.equal(calls[0].options.body, undefined);
  assert.equal(calls[0].options.signal, controller.signal);
});

test('all four published support states remain distinct without implying a successful connection', async () => {
  for (const state of ['SUPPORTED', 'CONDITIONAL', 'UNSUPPORTED', 'UNKNOWN']) {
    const envelope = features(state);
    const client = new ReadClient(async () => reply(envelope));
    assert.equal(client.dataset, null);
    assert.deepEqual(await client.readFeatures(), envelope);
    assert.equal(client.dataset, null);
  }
});

test('missing or different feature contract versions yield unknown support', async () => {
  for (const version of [undefined, null, '', 'UTT-READ-FEATURES/0.2.0', 'UTT-ACCESS-READ/0.1.0']) {
    const envelope = features();
    if (version === undefined) delete envelope.contract_version;
    else envelope.contract_version = version;
    const client = new ReadClient(async () => reply(envelope));
    assert.equal(await client.readFeatures(), null);
  }
});

test('feature envelope and filter keys are exact and malformed states remain unknown', async () => {
  const malformed = [null, [], {}, {contract_version: VERSION},
    {contract_version: VERSION, filters: null}, {contract_version: VERSION, filters: []}];
  const extraEnvelope = features();
  extraEnvelope.operations = ['fictional-operation'];
  malformed.push(extraEnvelope);
  const extraFilter = features();
  extraFilter.filters.fictional = 'SUPPORTED';
  malformed.push(extraFilter);
  for (const key of FILTERS) {
    const absent = features();
    delete absent.filters[key];
    malformed.push(absent);
    for (const invalid of [null, true, 1, '', 'supported', 'AVAILABLE', {}, []]) {
      const invalidState = features();
      invalidState.filters[key] = invalid;
      malformed.push(invalidState);
    }
  }
  for (const envelope of malformed) {
    const client = new ReadClient(async () => reply(envelope));
    assert.equal(await client.readFeatures(), null);
  }
});

test('HTTP failure and offline features produce null without another endpoint or synthetic fallback', async () => {
  for (const status of [401, 403, 404, 500, 503]) {
    const calls = [];
    const client = new ReadClient(async path => {
      calls.push(path);
      return {...reply(features()), ok: false, status};
    });
    assert.equal(await client.readFeatures(), null);
    assert.deepEqual(calls, ['/api/features']);
  }
  const offline = new ReadClient(async () => { throw new TypeError('Fictional offline feature request'); });
  assert.equal(await offline.readFeatures(), null);
});

test('invalid feature JSON stays unknown and unavailable reads do not reuse an earlier success', async () => {
  const invalidJson = new ReadClient(async () => ({ok: true, json: async () => { throw new SyntaxError('Fictional malformed JSON'); }}));
  assert.equal(await invalidJson.readFeatures(), null);
  const envelope = features();
  const replies = [reply(envelope), {ok: false, status: 404}, reply({contract_version: 'fictional-unknown-version', filters: envelope.filters})];
  const client = new ReadClient(async () => replies.shift());
  assert.equal(await client.readFeatures(), envelope);
  assert.equal(await client.readFeatures(), null);
  assert.equal(await client.readFeatures(), null);
});

test('feature request and JSON decoding cancellation preserve AbortError', async () => {
  for (const stage of ['fetch', 'json']) {
    const cancellation = new DOMException('Fictional cancellation', 'AbortError');
    const client = new ReadClient(async () => {
      if (stage === 'fetch') throw cancellation;
      return {ok: true, json: async () => { throw cancellation; }};
    });
    await assert.rejects(client.readFeatures(), error => error === cancellation);
  }
});
