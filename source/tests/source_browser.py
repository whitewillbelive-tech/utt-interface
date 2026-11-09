"""UI-only source-read regressions with generated fictional responses.

Run from the checkout root:
    PYTHONDONTWRITEBYTECODE=1 python3 source/tests/source_browser.py

The server is the existing in-memory synthetic provider. Browser interception
supplies the independent source contract; no configured transport, data file,
credentials, operational source, screenshot or original log is accessed.
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.server import make_server
from app.source_read import VERSION, source_error, source_result
from playwright.async_api import Error as PlaywrightError, async_playwright, expect


FIRST = 'synthetic-source-one'
SECOND = 'synthetic-source-alternate'
SOURCE_TEXT = '별개의 합성 출처 🧭 본문\n두 번째 줄과 <표식>을 그대로 보존합니다.'
RECORD_TEXT = '조회 응답이 명시한 별도의 합성 기록 대상 🧩 본문'
REQUEST_FIELDS = {'contract_version', 'dataset_id', 'snapshot_id', 'record_id',
                  'source_id', 'source_version'}


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def body_fixture(source, record_id, *, text=SOURCE_TEXT, kind='SOURCE_BODY',
                 fragment='🧭 본문', version='synthetic-body-v12', unit='UTF-16',
                 availability='AVAILABLE', location=True):
    """Construct public contract values from a displayed fictional source."""
    metadata = copy.deepcopy(source)
    metadata.update(version='synthetic-metadata-v7', availability='AVAILABLE',
                    content_sha256=sha('fictional original file bytes, not UTF-8 body'),
                    verification='VERIFIED')
    start = utf16_length(text.split(fragment, 1)[0]) if fragment else 0
    metadata['location'] = ({
        'target': kind, 'unit': unit, 'start': start,
        'end': start + utf16_length(fragment), 'boundary': 'half_open',
        'target_sha256': sha(text), 'fragment_sha256': sha(fragment),
        'verification': 'VERIFIED',
    } if location else None)
    content = {
        'availability': availability, 'target_kind': kind,
        'target_id': source['source_id'] if kind == 'SOURCE_BODY' else record_id,
        'target_version': version, 'locator_target_version': version,
        'text': text if availability == 'AVAILABLE' else None,
        'text_sha256': sha(text) if availability == 'AVAILABLE' else None,
    }
    return metadata, content


class SourceHarness:
    """Intercept only fictional detail metadata and the source-read endpoint."""
    def __init__(self, page, transform=None, respond=None):
        self.page = page
        self.transform = transform
        self.respond = respond
        self.fixtures = {}
        self.identities = {}
        self.queries = []

    async def install(self):
        await self.page.route('**/api/read', self.read)
        await self.page.route('**/api/source', self.source)

    async def read(self, route):
        request = route.request.post_data_json['request']
        if request['operation'] != 'detail':
            return await route.continue_()
        response = await route.fetch()
        envelope = await response.json()
        if envelope['status'] != 'OK':
            return await route.fulfill(response=response, json=envelope)
        result = envelope['result']
        record_id = result['record']['record_id']
        adjusted = []
        for source in result['sources']:
            if source['source_id'] == SECOND:
                metadata, content = body_fixture(source, record_id, text=RECORD_TEXT,
                                                 kind='RECORD_BODY', fragment='🧩 본문',
                                                 version='synthetic-record-body-v13')
            else:
                metadata, content = body_fixture(source, record_id)
            if self.transform:
                metadata, content = self.transform(metadata, content, record_id)
            adjusted.append(metadata)
            self.fixtures[(record_id, source['source_id'])] = (metadata, content)
            self.identities[(record_id, source['source_id'])] = {
                'contract_version': VERSION,
                'dataset_id': envelope['snapshot']['dataset_id'],
                'snapshot_id': envelope['snapshot']['snapshot_id'],
                'record_id': record_id, 'source_id': source['source_id'],
                'source_version': metadata['version'],
            }
        result['sources'] = copy.deepcopy(adjusted)
        result['record']['source_refs'] = copy.deepcopy(adjusted)
        await route.fulfill(response=response, json=envelope)

    def envelope(self, request):
        metadata, content = self.fixtures[(request['record_id'], request['source_id'])]
        return source_result(request, metadata, content)

    async def source(self, route):
        request = route.request.post_data_json
        # Each request is tied to the exact detail response, not a mutable scope.
        assert set(request) == REQUEST_FIELDS, request
        assert request == self.identities[(request['record_id'], request['source_id'])]
        self.queries.append(copy.deepcopy(request))
        if self.respond:
            return await self.respond(route, request, self)
        await route.fulfill(json=self.envelope(request))


def source_card(page, source_id=FIRST):
    return page.locator(f'.source[data-source-id="{source_id}"]')


def source_button(card):
    return card.get_by_role('button', name='출처 본문 조회', exact=True)


async def wait_for_list(page, count=3):
    await expect(page.locator('.record')).to_have_count(count)
    await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')


async def open_observation(page, base):
    await page.goto(base+'#library')
    await wait_for_list(page)
    await page.get_by_role('button', name='가상 관측', exact=True).click()
    await expect(page.locator('#detail h2')).to_have_text('가상 관측')
    await expect(page.locator('.source')).to_have_count(2)


async def fulfill_after(route, release, envelope):
    await release.wait()
    try:
        await route.fulfill(json=envelope)
    except PlaywrightError as error:
        # An aborted request or closed page is the expected cancellation path.
        if not any(word in str(error).lower() for word in ('closed', 'aborted', 'invalid interception')):
            raise


async def check_exact_targets_and_independent_cards(browser, base):
    page = await browser.new_page()
    first_started, first_release = asyncio.Event(), asyncio.Event()
    try:
        async def respond(route, request, harness):
            if request['source_id'] == FIRST:
                first_started.set()
                return await fulfill_after(route, first_release, harness.envelope(request))
            await route.fulfill(json=harness.envelope(request))
        harness = SourceHarness(page, respond=respond)
        await harness.install()
        await open_observation(page, base)
        original_body = await page.locator('.body-text:not(.source-body)').text_content()
        first, second = source_card(page), source_card(page, SECOND)
        await source_button(first).click()
        await asyncio.wait_for(first_started.wait(), 5)
        await source_button(second).click()
        await expect(second.locator('pre.source-body')).to_have_text(RECORD_TEXT)
        await expect(second).to_contain_text('RECORD_BODY')
        await expect(second.locator('mark')).to_have_text('🧩 본문')
        await expect(first.locator('pre.source-body')).to_have_count(0)
        first_release.set()
        await expect(first.locator('pre.source-body')).to_have_text(SOURCE_TEXT)
        await expect(first).to_contain_text('SOURCE_BODY')
        await expect(first).to_contain_text('VERIFIED')
        await expect(first.locator('mark')).to_have_text('🧭 본문')
        # Neither source is the record body and each card keeps its own result.
        assert original_body not in (SOURCE_TEXT, RECORD_TEXT)
        assert await page.locator('.body-text:not(.source-body)').text_content() == original_body
        assert await first.locator('pre.source-body').text_content() == SOURCE_TEXT
        assert await second.locator('pre.source-body').text_content() == RECORD_TEXT
        for source_id in (FIRST, SECOND):
            metadata, content = harness.fixtures[(harness.queries[0]['record_id'], source_id)]
            assert metadata['version'] != content['target_version']
            assert metadata['content_sha256'] != content['text_sha256']
            await expect(source_card(page, source_id)).to_contain_text(metadata['version'])
            await expect(source_card(page, source_id)).to_contain_text(content['target_version'])
        assert len(harness.queries) == 2
    finally:
        first_release.set()
        await page.close()


async def check_empty_and_unavailable_content(browser, base):
    cases = [('AVAILABLE', '빈 본문 (이용 가능)'), ('MISSING', '누락'),
             ('UNKNOWN', '미확인'), ('INACCESSIBLE', '접근 불가'),
             ('NOT_SUPPORTED', '미지원')]
    for availability, expected in cases:
        page = await browser.new_page()
        try:
            def transform(metadata, content, record_id):
                return body_fixture(metadata, record_id, text='', fragment='',
                                    availability=availability, location=False)
            harness = SourceHarness(page, transform=transform)
            await harness.install()
            await open_observation(page, base)
            card = source_card(page)
            await source_button(card).click()
            await expect(card).to_contain_text(expected)
            if availability == 'AVAILABLE':
                await expect(card.locator('pre.source-body')).to_have_text('')
            else:
                await expect(card.locator('pre.source-body')).to_have_count(0)
                await expect(card).to_contain_text('NOT_AVAILABLE')
                assert '빈 본문 (이용 가능)' not in await card.inner_text()
            await expect(card.locator('mark')).to_have_count(0)
            assert len(harness.queries) == 1
        finally:
            await page.close()


async def check_unknown_version_and_failure_states(browser, base):
    page = await browser.new_page()
    try:
        def unknown(metadata, content, _record_id):
            metadata['version'] = None
            return metadata, content
        harness = SourceHarness(page, transform=unknown)
        await harness.install()
        await open_observation(page, base)
        card = source_card(page)
        await expect(source_button(card)).to_be_disabled()
        await expect(card).to_contain_text('미확인')
        await expect(card.get_by_role('heading', name='출처 1', exact=True)).to_be_visible()
        await expect(card).to_have_attribute('data-source-id', FIRST)
        assert not harness.queries
    finally:
        await page.close()
    for code in ('NOT_SUPPORTED', 'SOURCE_MISSING', 'ITEM_NOT_FOUND',
                 'CONNECTION_UNAVAILABLE', 'VERSION_MISMATCH'):
        page = await browser.new_page()
        try:
            async def respond(route, _request, _harness):
                await route.fulfill(json=source_error(code))
            harness = SourceHarness(page, respond=respond)
            await harness.install()
            await open_observation(page, base)
            card = source_card(page)
            await source_button(card).click()
            await expect(card).to_contain_text(code)
            await expect(card.locator('pre.source-body')).to_have_count(0)
            assert '빈 본문 (이용 가능)' not in await card.inner_text()
            # Failure in one card never creates a success or failure in the other.
            await expect(source_card(page, SECOND).locator('pre.source-body')).to_have_count(0)
            assert code not in await source_card(page, SECOND).inner_text()
        finally:
            await page.close()
    # A transport failure remains a connection error; a retry can fetch anew.
    page = await browser.new_page()
    try:
        async def retry(route, request, harness):
            if len(harness.queries) == 1:
                return await route.abort('failed')
            await route.fulfill(json=harness.envelope(request))
        harness = SourceHarness(page, respond=retry)
        await harness.install()
        await open_observation(page, base)
        card = source_card(page)
        await source_button(card).click()
        await expect(card).to_contain_text('CONNECTION_UNAVAILABLE')
        await source_button(card).click()
        await expect(card.locator('pre.source-body')).to_have_text(SOURCE_TEXT)
        assert len(harness.queries) == 2
    finally:
        await page.close()


async def check_unsupported_coordinates_and_surrogate_boundary(browser, base):
    for unit in ('byte', 'unicode_code_point'):
        page = await browser.new_page()
        try:
            def unsupported(metadata, content, _record_id):
                metadata['location']['unit'] = unit
                return metadata, content
            harness = SourceHarness(page, transform=unsupported)
            await harness.install()
            await open_observation(page, base)
            card = source_card(page)
            await source_button(card).click()
            await expect(card.locator('pre.source-body')).to_have_text(SOURCE_TEXT)
            await expect(card).to_contain_text(unit)
            await expect(card).to_contain_text('NOT_SUPPORTED')
            await expect(card.locator('mark')).to_have_count(0)
        finally:
            await page.close()
    page = await browser.new_page()
    try:
        def surrogate(metadata, content, _record_id):
            metadata['location'].update(start=1, end=1, fragment_sha256=sha(''))
            content.update(text='🧭', text_sha256=sha('🧭'))
            metadata['location']['target_sha256'] = sha('🧭')
            return metadata, content
        async def forged_verified(route, request, harness):
            # A manually forged response also covers backend versions that fix
            # the old empty-range-inside-surrogate verification bug.
            metadata, content = harness.fixtures[(request['record_id'], request['source_id'])]
            result = {key: request[key] for key in REQUEST_FIELDS if key != 'contract_version'}
            result.update(source=copy.deepcopy(metadata), content=copy.deepcopy(content),
                          locator_verification={'status': 'VERIFIED', 'reason': 'HASH_AND_RANGE_MATCH'})
            await route.fulfill(json={'contract_version': VERSION, 'status': 'OK',
                                     'result': result, 'error': None})
        harness = SourceHarness(page, transform=surrogate, respond=forged_verified)
        await harness.install()
        await open_observation(page, base)
        card = source_card(page)
        await source_button(card).click()
        await expect(card).to_contain_text('INVALID_RESPONSE')
        await expect(card.locator('mark')).to_have_count(0)
        await expect(card.locator('pre.source-body')).to_have_count(0)
    finally:
        await page.close()


async def check_same_source_retry_race(browser, base):
    page = await browser.new_page()
    old_started, old_release = asyncio.Event(), asyncio.Event()
    old_done = asyncio.Event()
    try:
        async def respond(route, request, harness):
            envelope = harness.envelope(request)
            if len(harness.queries) == 1:
                # Keep a valid but distinguishable first response pending.
                envelope['result']['content']['text'] = '지연된 이전 합성 응답'
                envelope['result']['content']['text_sha256'] = sha('지연된 이전 합성 응답')
                envelope['result']['source']['location'] = None
                envelope['result']['locator_verification'] = {'status': 'UNVERIFIED', 'reason': 'LOCATION_NOT_PROVIDED'}
                old_started.set()
                try:
                    return await fulfill_after(route, old_release, envelope)
                finally:
                    old_done.set()
            await route.fulfill(json=envelope)
        harness = SourceHarness(page, respond=respond)
        await harness.install()
        await open_observation(page, base)
        card = source_card(page)
        await source_button(card).click()
        await asyncio.wait_for(old_started.wait(), 5)
        await source_button(card).click()
        await expect(card.locator('pre.source-body')).to_have_text(SOURCE_TEXT)
        old_release.set()
        await asyncio.wait_for(old_done.wait(), 5)
        await expect(card.locator('pre.source-body')).to_have_text(SOURCE_TEXT)
        assert len(harness.queries) == 2
    finally:
        old_release.set()
        await page.close()


async def check_late_source_after_navigation(browser, base):
    for navigation in ('record', 'back', 'filter'):
        page = await browser.new_page()
        started, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        try:
            async def respond(route, request, harness):
                envelope = harness.envelope(request)
                started.set()
                try:
                    await fulfill_after(route, release, envelope)
                finally:
                    done.set()
            harness = SourceHarness(page, respond=respond)
            await harness.install()
            if navigation == 'record':
                await page.goto(base+'#library')
                await wait_for_list(page)
                await page.locator('#search').fill('가상 이력')
                await wait_for_list(page, 1)
                await page.get_by_role('button', name='가상 이력', exact=True).click()
                await expect(page.locator('#detail h2')).to_have_text('가상 이력')
            else:
                await open_observation(page, base)
            await source_button(source_card(page)).click()
            await asyncio.wait_for(started.wait(), 5)
            if navigation == 'record':
                await page.get_by_role('button', name='관련 자료: 가상 관측', exact=True).click()
                await expect(page.locator('#detail h2')).to_have_text('가상 관측')
            elif navigation == 'back':
                await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
                await expect(page.locator('#detail')).to_be_hidden()
            else:
                await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
                await page.locator('#search').fill('가상 미분류')
                await wait_for_list(page, 1)
                await expect(page.locator('#detail')).to_be_hidden()
            release.set()
            await asyncio.wait_for(done.wait(), 5)
            await expect(page.locator('pre.source-body')).to_have_count(0)
            if navigation == 'record':
                await expect(page.locator('#detail h2')).to_have_text('가상 관측')
            else:
                await expect(page.locator('#detail')).to_be_hidden()
                await expect(page.locator('#list-view')).to_be_visible()
        finally:
            release.set()
            await page.close()


async def check_mobile_list_return_after_source(browser, base):
    page = await browser.new_page(viewport={'width': 390, 'height': 844})
    try:
        harness = SourceHarness(page)
        await harness.install()
        await page.goto(base+'#library')
        await wait_for_list(page)
        await page.locator('#search').fill('가상')
        await page.locator('#role').select_option('SYNTHETIC_NOTE')
        await wait_for_list(page)
        await page.locator('#next').click()
        await expect(page.locator('#page-number')).to_have_text('2 페이지')
        origin = page.get_by_role('button', name='가상 이력', exact=True)
        await origin.scroll_into_view_if_needed()
        await origin.focus()
        original_scroll = await page.evaluate('scrollY')
        await origin.press('Enter')
        await page.get_by_role('button', name='관련 자료: 가상 관측', exact=True).click()
        await expect(page.locator('#detail h2')).to_have_text('가상 관측')
        card = source_card(page)
        await source_button(card).click()
        await expect(card.locator('pre.source-body')).to_have_text(SOURCE_TEXT)
        await expect(card.locator('mark')).to_have_text('🧭 본문')
        await expect(card.locator('pre.source-body')).to_have_attribute('tabindex', '0')
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
        await expect(origin).to_be_focused()
        await expect(page.locator('#page-number')).to_have_text('2 페이지')
        await expect(page.locator('#search')).to_have_value('가상')
        await expect(page.locator('#role')).to_have_value('SYNTHETIC_NOTE')
        await expect(page.locator('#sort')).to_have_value('id_asc')
        await page.wait_for_function('expected => Math.abs(scrollY - expected) <= 3', arg=original_scroll)
    finally:
        await page.close()


async def main():
    server = make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    checks = [check_exact_targets_and_independent_cards,
              check_empty_and_unavailable_content, check_unknown_version_and_failure_states,
              check_unsupported_coordinates_and_surrogate_boundary,
              check_same_source_retry_race, check_late_source_after_navigation,
              check_mobile_list_return_after_source]
    results, browser_errors = [], []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path='/usr/bin/chromium',
                                               headless=True, args=['--no-sandbox'])
            browser.on('page', lambda page: page.on('pageerror', lambda exc: browser_errors.append(type(exc).__name__)))
            for check in checks:
                try:
                    await check(browser, base)
                    results.append({'check': check.__name__, 'status': 'passed'})
                except Exception:
                    results.append({'check': check.__name__, 'status': 'failed'})
                    print(json.dumps({'checks': results}, ensure_ascii=False, indent=2), flush=True)
                    raise
            assert not browser_errors, browser_errors
            print(json.dumps({'passed': len(results), 'failed': 0,
                              'browser_errors': len(browser_errors), 'checks': results},
                             ensure_ascii=False, indent=2))
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        if hasattr(server.provider, 'close'):
            server.provider.close()


if __name__ == '__main__':
    asyncio.run(main())
