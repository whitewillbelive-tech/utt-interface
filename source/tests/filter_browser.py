"""UI-only filter-feature regressions against generated fictional records.

Run: PYTHONDONTWRITEBYTECODE=1 python3 source/tests/filter_browser.py

The existing in-memory synthetic server is the only data provider. Mode and
feature descriptions are browser interceptions, not operational connections.
No configured transport, source, credential, original log or screenshot is used.
"""
import asyncio
import copy
import json
from pathlib import Path
import re
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.server import make_server
from app.provider import FIELDS, OPERATIONS, error
from playwright.async_api import async_playwright, expect


VERSION = 'UTT-READ-FEATURES/0.1.0'
SUPPORTED = {'contract_version': VERSION, 'filters': {
    'query': 'SUPPORTED', 'project': 'SUPPORTED', 'document_role': 'SUPPORTED',
    'review_status': 'SUPPORTED', 'tag': 'UNSUPPORTED', 'source': 'UNSUPPORTED'}}


def features(**states):
    value = copy.deepcopy(SUPPORTED)
    value['filters'].update(states)
    return value


class FilterHarness:
    def __init__(self, page, value='server', *, readonly=True, feature_failure=None,
                 fail_query=False):
        self.page, self.value = page, value
        self.readonly, self.feature_failure = readonly, feature_failure
        self.fail_query = fail_query
        self.searches, self.requests, self.feature_methods = [], [], []
        self.changed = asyncio.Event()

    async def install(self):
        if self.readonly:
            await self.page.route('**/api/mode', lambda route: route.fulfill(
                json={'mode': 'readonly', 'simulation_namespace': None}))
        await self.page.route('**/api/features', self.feature)
        await self.page.route('**/api/read', self.read)

    async def feature(self, route):
        self.feature_methods.append(route.request.method)
        if self.feature_failure == 'abort':
            return await route.abort('failed')
        if self.feature_failure == 'http':
            return await route.fulfill(status=503, json={'error': 'fictional unavailable'})
        if self.feature_failure == 'json':
            return await route.fulfill(content_type='application/json', body='{')
        if self.value == 'server':
            response = await route.fetch()
            assert await response.json() == SUPPORTED
            return await route.fulfill(response=response)
        await route.fulfill(json=self.value)

    async def read(self, route):
        payload = route.request.post_data_json
        request = payload['request']
        # The independent GET contract must not extend READ/0.1 or invent an
        # operation, tag/source request field, endpoint or approval meaning.
        assert set(request) == FIELDS
        assert request['operation'] in OPERATIONS
        assert 'tag' not in request and 'source' not in request
        self.requests.append(copy.deepcopy(request))
        if request['operation'] == 'search':
            self.searches.append(copy.deepcopy(request))
            self.changed.set()
            if self.fail_query and request['query']:
                return await route.fulfill(json=error('search', 'INVALID_REQUEST'))
        await route.continue_()

    async def action(self, action):
        previous = len(self.searches)
        self.changed.clear()
        await action()
        await asyncio.wait_for(self.changed.wait(), 5)
        assert len(self.searches) > previous
        await expect(self.page.locator('#results')).to_have_attribute('aria-busy', 'false')
        return self.searches[-1]


async def start(page, base, harness):
    await harness.install()
    await page.goto(base)
    await expect(page.locator('.record')).to_have_count(3)
    await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')
    assert harness.feature_methods == ['GET']
    assert harness.searches


async def submit(page):
    await page.locator('#filters button[type=submit]').click()


async def assert_no_project_actions(page, root):
    # Metadata can stay visible as text or disabled buttons. Either choice must
    # prevent user selection while this filter has no confirmed support.
    buttons = page.locator(root).get_by_role('button', name=re.compile('구름 정원|모래 시계|가상 읽기 구성요소'))
    for button in await buttons.all():
        await expect(button).to_be_disabled()


async def check_synthetic_default_features(browser, base):
    page = await browser.new_page()
    try:
        harness = FilterHarness(page, readonly=False)
        await start(page, base, harness)
        for selector in ('#search', '#project', '#role', '#review'):
            await expect(page.locator(selector)).to_be_enabled()
        query = await harness.action(lambda: page.locator('#role').select_option('SYNTHETIC_NOTE'))
        assert query['document_role'] == 'SYNTHETIC_NOTE'
        query = await harness.action(lambda: page.locator('#review').select_option('HELD'))
        assert query['review_status'] == 'HELD'
        await expect(page.locator('.record')).to_have_count(2)
        query = await harness.action(lambda: page.locator('#project').select_option('synthetic-project-cloud'))
        assert query['project_id'] == 'synthetic-project-cloud'
        await expect(page.locator('.record')).to_have_count(2)
    finally:
        await page.close()


async def check_supported_filters_in_readonly_mode(browser, base):
    page = await browser.new_page()
    try:
        harness = FilterHarness(page, features())
        await start(page, base, harness)
        await expect(page.locator('#mode')).to_contain_text('읽기 공급자 응답 확인')
        for selector in ('#search', '#project', '#role', '#review'):
            await expect(page.locator(selector)).to_be_enabled()
        await harness.action(lambda: page.locator('#role').select_option('SYNTHETIC_NOTE'))
        query = await harness.action(lambda: page.locator('#review').select_option('HELD'))
        assert query['document_role'] == 'SYNTHETIC_NOTE' and query['review_status'] == 'HELD'
        await expect(page.locator('.record')).to_have_count(2)
        query = await harness.action(lambda: page.locator('#search').fill('가상 교차'))
        assert query['query'] == '가상 교차'
        await expect(page.locator('.record')).to_have_count(1)
        query = await harness.action(lambda: page.get_by_role('button', name='필터 초기화', exact=True).click())
        assert query['document_role'] is None and query['review_status'] is None and query['project_id'] is None
        await expect(page.locator('.record')).to_have_count(3)
        await page.get_by_role('button', name='가상 관측', exact=True).click()
        await expect(page.locator('#detail h2')).to_have_text('가상 관측')
        query = await harness.action(lambda: page.locator('#detail').get_by_role(
            'button', name=re.compile('^구름 정원')).click())
        assert query['project_id'] == 'synthetic-project-cloud'
        await expect(page.locator('#project')).to_have_value('synthetic-project-cloud')
    finally:
        await page.close()


async def check_mixed_role_review_and_reset(browser, base):
    states = [('SUPPORTED', 'UNKNOWN'), ('UNSUPPORTED', 'SUPPORTED'),
              ('UNKNOWN', 'UNSUPPORTED'), ('CONDITIONAL', 'CONDITIONAL')]
    for role, review in states:
        page = await browser.new_page()
        try:
            harness = FilterHarness(page, features(document_role=role, review_status=review))
            await start(page, base, harness)
            for selector, status in (('#role', role), ('#review', review)):
                if status == 'SUPPORTED':
                    await expect(page.locator(selector)).to_be_enabled()
                else:
                    await expect(page.locator(selector)).to_be_disabled()
            if role == 'UNKNOWN' or review == 'UNKNOWN':
                await expect(page.locator('#filter-support')).to_contain_text('미확인')
            # Assign stale values without dispatching changes, including to
            # disabled controls. Requests must still apply the feature policy.
            await page.locator('#role').evaluate("node => { node.value = 'SYNTHETIC_NOTE'; }")
            await page.locator('#review').evaluate("node => { node.value = 'HELD'; }")
            query = await harness.action(lambda: submit(page))
            assert query['document_role'] == ('SYNTHETIC_NOTE' if role == 'SUPPORTED' else None)
            assert query['review_status'] == ('HELD' if review == 'SUPPORTED' else None)
            query = await harness.action(lambda: page.get_by_role('button', name='필터 초기화', exact=True).click())
            assert query['document_role'] is None and query['review_status'] is None
            assert query['project_id'] is None and query['cursor'] is None
            await expect(page.locator('.record')).to_have_count(3)
        finally:
            await page.close()


async def check_unconfirmed_project_every_entry_point(browser, base):
    for status in ('CONDITIONAL', 'UNKNOWN', 'UNSUPPORTED'):
        page = await browser.new_page()
        try:
            harness = FilterHarness(page, features(project=status))
            await start(page, base, harness)
            await expect(page.locator('#project')).to_be_disabled()
            await assert_no_project_actions(page, '#projects')
            await assert_no_project_actions(page, '#components')
            if status == 'CONDITIONAL':
                await expect(page.locator('#filter-support')).to_contain_text('조건부')
            if status == 'UNKNOWN':
                await expect(page.locator('#filter-support')).to_contain_text('미확인')
            await page.get_by_role('button', name='가상 관측', exact=True).click()
            await expect(page.locator('#detail h2')).to_have_text('가상 관측')
            await assert_no_project_actions(page, '#detail')
            await page.locator('#project').evaluate("node => { node.value = 'synthetic-project-cloud'; }")
            query = await harness.action(lambda: submit(page))
            assert query['project_id'] is None
            await expect(page.locator('.record')).to_have_count(3)
            assert all(q['project_id'] is None for q in harness.searches)
        finally:
            await page.close()


async def check_query_unknown_success_failure_and_unsupported(browser, base):
    for fail_query in (False, True):
        page = await browser.new_page()
        try:
            harness = FilterHarness(page, features(query='UNKNOWN'), fail_query=fail_query)
            await start(page, base, harness)
            assert harness.searches[0]['query'] in (None, '')
            await expect(page.locator('#search')).to_be_enabled()
            await expect(page.locator('#filter-support')).to_contain_text('미확인')
            query = await harness.action(lambda: page.locator('#search').fill('가상 관측'))
            assert query['query'] == '가상 관측'
            if fail_query:
                await expect(page.locator('#notice')).to_contain_text('INVALID_REQUEST')
                await expect(page.locator('.record')).to_have_count(0)
                assert '조건에 맞는 기록이 없습니다.' not in await page.locator('#results').inner_text()
            else:
                await expect(page.locator('.record')).to_have_count(1)
                await expect(page.locator('.record-title')).to_have_text('가상 관측')
            await expect(page.locator('#filter-support')).to_contain_text('미확인')
        finally:
            await page.close()
    page = await browser.new_page()
    try:
        harness = FilterHarness(page, features(query='UNSUPPORTED'))
        await start(page, base, harness)
        await expect(page.locator('#search')).to_be_disabled()
        await page.locator('#search').evaluate("node => { node.value = '가상 관측'; }")
        query = await harness.action(lambda: submit(page))
        assert query['query'] is None
        await expect(page.locator('.record')).to_have_count(3)
        query = await harness.action(lambda: page.get_by_role('button', name='필터 초기화', exact=True).click())
        assert query['query'] is None
        assert all(q['query'] is None for q in harness.searches)
    finally:
        await page.close()


async def check_unavailable_or_invalid_features_preserve_list(browser, base):
    missing = features()
    missing['filters'].pop('review_status')
    extra = features()
    extra['filters']['invented_filter'] = 'SUPPORTED'
    cases = [('server', 'abort'), ('server', 'http'), ('server', 'json'),
             ({**features(), 'contract_version': 'UTT-READ-FEATURES/fictional-old'}, None),
             (missing, None), (extra, None), (features(document_role='supported'), None)]
    for value, failure in cases:
        page = await browser.new_page()
        try:
            harness = FilterHarness(page, value, feature_failure=failure)
            await start(page, base, harness)
            await expect(page.locator('#filter-support')).to_contain_text('미확인')
            await expect(page.locator('#search')).to_be_enabled()
            for selector in ('#role', '#review', '#project'):
                await expect(page.locator(selector)).to_be_disabled()
            assert harness.searches[0]['query'] in (None, '')
            assert all(q['project_id'] is None and q['document_role'] is None
                       and q['review_status'] is None for q in harness.searches)
            await expect(page.locator('.record')).to_have_count(3)
        finally:
            await page.close()


async def main():
    server = make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    checks = [check_synthetic_default_features, check_supported_filters_in_readonly_mode,
              check_mixed_role_review_and_reset, check_unconfirmed_project_every_entry_point,
              check_query_unknown_success_failure_and_unsupported,
              check_unavailable_or_invalid_features_preserve_list]
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
