"""UI-only Chromium regressions against generated fictional data.

Run from the checkout root:
    PYTHONDONTWRITEBYTECODE=1 python3 source/tests/ui_navigation_browser.py

Uses existing loopback servers and browser-only contract-preserving interception.
No real transport, data, credentials, filesystem configuration or screenshots.
"""
import asyncio
import json
from pathlib import Path
import re
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.server import make_server
from app.provider import error
from source.tests.source_ui_fixtures import RECORD_TEXT, SOURCE_TEXT, make_source_fixture_server
from playwright.async_api import async_playwright, expect


async def read_response(route):
    response = await route.fetch()
    return response, await response.json()


async def wait_for_list(page, count=3):
    await expect(page.locator('.record')).to_have_count(count)
    await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')


async def open_observation(page):
    await page.get_by_role('button', name='가상 관측', exact=True).click()
    await expect(page.locator('#detail h2')).to_have_text('가상 관측')


async def check_http_errors(browser, base):
    for status, code in [(401, 'AUTHENTICATION_REQUIRED'), (403, 'ACCESS_DENIED'), (503, 'CONNECTION_UNAVAILABLE'), (418, 'HTTP_ERROR')]:
        page = await browser.new_page()
        try:
            await page.route('**/api/read', lambda route: route.fulfill(status=status, json={'error': 'fictional rejection'}))
            await page.goto(base+'#library')
            await expect(page.locator('#notice')).to_contain_text(code)
            await expect(page.locator('.record')).to_have_count(0)
            if status in (401, 403):
                assert 'CONNECTION_UNAVAILABLE' not in await page.locator('#notice').inner_text()
        finally:
            await page.close()
    page = await browser.new_page()
    try:
        await page.route('**/api/read', lambda route: route.abort('failed'))
        await page.goto(base+'#library')
        await expect(page.locator('#notice')).to_contain_text('CONNECTION_UNAVAILABLE')
        await expect(page.locator('.record')).to_have_count(0)
    finally:
        await page.close()


async def check_mode_denial(browser, base):
    page = await browser.new_page()
    reads = []
    try:
        page.on('request', lambda req: reads.append(req.url) if req.url.endswith('/api/read') else None)
        await page.route('**/api/mode', lambda route: route.fulfill(status=403, json={'error': 'fictional rejection'}))
        await page.goto(base+'#library')
        await expect(page.locator('#notice')).to_contain_text('ACCESS_DENIED')
        await expect(page.locator('.record')).to_have_count(0)
        assert not reads, 'A denied mode probe must not continue into read or synthetic success.'
    finally:
        await page.close()


async def check_capabilities(browser, base):
    for maximum, operations in [(2, ['capabilities', 'search']),
                                (1, ['capabilities', 'projects', 'search', 'detail', 'related_history'])]:
        page = await browser.new_page()
        sent = []
        try:
            async def capabilities(route):
                q = route.request.post_data_json['request']
                sent.append((q['operation'], q['page_size']))
                if q['operation'] == 'capabilities':
                    response, data = await read_response(route)
                    data['result']['operations'] = operations
                    data['result']['max_page_size'] = maximum
                    return await route.fulfill(response=response, json=data)
                return await route.continue_()
            await page.route('**/api/read', capabilities)
            await page.goto(base+'#library')
            await wait_for_list(page, maximum)
            assert all(operation in operations for operation, _ in sent), sent
            assert all(size <= maximum for operation, size in sent if operation != 'capabilities'), sent
            if 'detail' not in operations:
                await expect(page.locator('.record-title').first).to_be_disabled()
                await expect(page.locator('#project')).to_be_disabled()
                assert not any(operation in ('projects', 'detail', 'related_history') for operation, _ in sent)
            else:
                await expect(page.locator('#components')).to_contain_text('가상 읽기 구성요소')
                assert sum(operation == 'projects' for operation, _ in sent) >= 4, sent
        finally:
            await page.close()
    # A scoped project supplied by detail remains usable without a projects API.
    for can_search in (True, False):
        page = await browser.new_page()
        sent = []
        allowed = ['capabilities', 'search', 'detail'] if can_search else ['capabilities', 'projects']
        try:
            async def limited_operations(route):
                q = route.request.post_data_json['request']
                sent.append(q.copy())
                if q['operation'] == 'capabilities':
                    response, data = await read_response(route)
                    data['result']['operations'] = allowed
                    return await route.fulfill(response=response, json=data)
                return await route.continue_()
            await page.route('**/api/read', limited_operations)
            await page.goto(base+'#library')
            if can_search:
                await wait_for_list(page)
                await expect(page.locator('#project')).to_be_disabled()
                await open_observation(page)
                await expect(page.locator('#detail')).to_contain_text('공급자가 이력 조회를 지원하지 않습니다.')
                await expect(page.locator('#project')).to_be_enabled()
                await page.locator('#detail').get_by_role('button', name=re.compile('구름 정원')).click()
                await expect(page.locator('#project')).to_have_value('synthetic-project-cloud')
                await wait_for_list(page)
                assert sent[-1]['operation'] == 'search' and sent[-1]['project_id'] == 'synthetic-project-cloud'
                assert not any(q['operation'] in ('projects', 'related_history') for q in sent), sent
            else:
                await expect(page.locator('#notice')).to_contain_text('공급자가 검색을 지원하지 않습니다.')
                await expect(page.locator('#filters button[type=submit]')).to_be_disabled()
                await expect(page.locator('.record')).to_have_count(0)
                assert not any(q['operation'] == 'search' for q in sent), sent
            assert all(q['operation'] in allowed for q in sent), sent
        finally:
            await page.close()


async def check_history_availability(browser, base):
    for status, expected in [('UNKNOWN', '미확인'), ('INACCESSIBLE', '접근 불가')]:
        page = await browser.new_page()
        try:
            async def unavailable(route):
                if route.request.post_data_json['request']['operation'] != 'related_history':
                    return await route.continue_()
                response, data = await read_response(route)
                data['result'].update(history_status=status, events=[], relations=[], related_records=[])
                return await route.fulfill(response=response, json=data)
            await page.route('**/api/read', unavailable)
            await page.goto(base+'#library')
            await wait_for_list(page)
            await open_observation(page)
            await expect(page.locator('#detail')).to_contain_text('이력 상태: ' + expected)
            assert '사건 없음' not in await page.locator('#detail').inner_text()
        finally:
            await page.close()
    page = await browser.new_page()
    try:
        async def missing_detail(route):
            if route.request.post_data_json['request']['operation'] == 'detail':
                return await route.fulfill(json=error('detail', 'ITEM_NOT_FOUND'))
            return await route.continue_()
        await page.route('**/api/read', missing_detail)
        await page.goto(base+'#library')
        await wait_for_list(page)
        await page.get_by_role('button', name='가상 관측', exact=True).click()
        await expect(page.locator('#notice')).to_contain_text('ITEM_NOT_FOUND')
        assert 'CONNECTION_UNAVAILABLE' not in await page.locator('#notice').inner_text()
    finally:
        await page.close()


async def check_history_retry(browser, base):
    page = await browser.new_page()
    cursors = []
    try:
        async def retry_history(route):
            q = route.request.post_data_json['request']
            if q['operation'] == 'capabilities':
                response, data = await read_response(route)
                data['result']['max_page_size'] = 2
                return await route.fulfill(response=response, json=data)
            if q['operation'] == 'related_history' and q['cursor']:
                cursors.append(q['cursor'])
                if len(cursors) == 1:
                    return await route.fulfill(status=403, json={'error': 'fictional denial'})
            return await route.continue_()
        await page.route('**/api/read', retry_history)
        await page.goto(base+'#library')
        await wait_for_list(page, 2)
        await page.locator('#search').fill('가상 이력')
        await wait_for_list(page, 1)
        await page.get_by_role('button', name='가상 이력', exact=True).click()
        await expect(page.locator('#detail')).to_contain_text('synthetic-event-002')
        await page.get_by_role('button', name='다음 이력 페이지', exact=True).click()
        retry = page.get_by_role('button', name='이력 다시 시도', exact=True)
        await expect(retry).to_be_enabled()
        await expect(page.locator('#detail')).to_contain_text('ACCESS_DENIED')
        await retry.click()
        await expect(page.get_by_role('button', name='관련 자료: 가상 관측', exact=True)).to_be_visible()
        assert len(cursors) == 2 and cursors[0] == cursors[1], 'Retry must preserve the cursor.'
        assert (await page.locator('#detail').inner_text()).count('synthetic-event-002') == 1
    finally:
        await page.close()
    page = await browser.new_page()
    try:
        async def initial_history_denial(route):
            if route.request.post_data_json['request']['operation'] == 'related_history':
                return await route.fulfill(status=403, json={'error': 'fictional denial'})
            return await route.continue_()
        await page.route('**/api/read', initial_history_denial)
        await page.goto(base+'#library')
        await wait_for_list(page)
        await open_observation(page)
        await expect(page.get_by_role('button', name='이력 다시 시도', exact=True)).to_be_enabled()
        await expect(page.locator('#notice')).to_contain_text('ACCESS_DENIED')
        assert '자료를 읽고 있습니다.' not in await page.locator('#notice').inner_text()
    finally:
        await page.close()


async def check_relation_targets(browser, base):
    for kind in ('foreign', 'unknown', 'reverse'):
        page = await browser.new_page()
        try:
            async def relation_target(route):
                if route.request.post_data_json['request']['operation'] != 'detail':
                    return await route.continue_()
                response, data = await read_response(route)
                relation = data['result']['record']['project_relations'][0]
                if kind == 'foreign':
                    relation['to_dataset_id'] = 'synthetic-external'
                    relation['to_id'] = 'fictional-project'
                elif kind == 'unknown':
                    relation['to_id'] = 'synthetic-unknown-project'
                else:
                    relation['from_id'], relation['to_id'] = relation['to_id'], relation['from_id']
                return await route.fulfill(response=response, json=data)
            await page.route('**/api/read', relation_target)
            await page.goto(base+'#library')
            await wait_for_list(page)
            await open_observation(page)
            if kind == 'reverse':
                await page.locator('#detail').get_by_role('button', name=re.compile('구름 정원')).click()
                await expect(page.locator('#project')).to_have_value('synthetic-project-cloud')
                await expect(page.locator('#detail')).to_be_hidden()
                await wait_for_list(page)
            else:
                message = '다른 데이터셋 관계 · 현재 연결에서 이동 미지원' if kind == 'foreign' else '연결 대상 프로젝트 미확인'
                await expect(page.locator('#detail')).to_contain_text(message)
                await expect(page.locator('#project')).to_have_value('')
                await expect(page.locator('#detail h2')).to_have_text('가상 관측')
                assert await page.locator('#detail').get_by_role('button', name='fictional-project').count() == 0
        finally:
            await page.close()


async def check_readonly_boundary(browser, base, readonly):
    page = await browser.new_page()
    try:
        # The server remains a fictional generator. Mode and published feature
        # support model an unconfirmed readonly provider; no real transport exists.
        await page.route('**/api/mode', lambda route: route.fulfill(json={'mode': 'readonly', 'simulation_namespace': None}))
        await page.route('**/api/features', lambda route: route.fulfill(json={
            'contract_version': 'UTT-READ-FEATURES/0.1.0',
            'filters': {'query': 'UNKNOWN', 'project': 'UNKNOWN',
                        'document_role': 'UNKNOWN', 'review_status': 'UNKNOWN',
                        'tag': 'UNSUPPORTED', 'source': 'UNSUPPORTED'},
        }))
        search_sorts = []
        search_filters = []
        async def descending_sort(route):
            payload = route.request.post_data_json
            if payload['request']['operation'] == 'capabilities':
                response, data = await read_response(route)
                data['result']['sort_order'] = ['dataset_id:asc', 'record_id:desc']
                return await route.fulfill(response=response, json=data)
            if payload['request']['operation'] == 'search':
                search_sorts.append(payload['sort'])
                search_filters.append((payload['request']['document_role'], payload['request']['review_status']))
            return await route.continue_()
        await page.route('**/api/read', descending_sort)
        await page.goto(base+'#library')
        await wait_for_list(page)
        await expect(page.locator('#sort')).to_have_value('id_desc')
        await expect(page.locator('#role')).to_be_disabled()
        await expect(page.locator('#review')).to_be_disabled()
        await expect(page.locator('#role')).to_have_value('')
        await expect(page.locator('#review')).to_have_value('')
        await expect(page.locator('.record-title').first).to_have_text('가상 빈 설명')
        await page.get_by_role('button', name='필터 초기화', exact=True).click()
        await wait_for_list(page)
        await expect(page.locator('#sort')).to_have_value('id_desc')
        await expect(page.locator('.record-title').first).to_have_text('가상 빈 설명')
        assert len(search_sorts) >= 2 and set(search_sorts) == {'id_desc'}, search_sorts
        assert search_filters and all(filters == (None, None) for filters in search_filters), search_filters
        await expect(page.locator('#mode')).to_contain_text('읽기 공급자 응답 확인')
        assert not await page.locator('#scenario').is_visible() or not await page.locator('#scenario').is_enabled()
        await page.locator('#search').fill('가상 관측')
        await wait_for_list(page, 1)
        await open_observation(page)
        await expect(page.locator('#detail')).to_contain_text('이력 상태:')
        await expect(page.locator('.simulation')).to_have_count(0)
        assert '합성 자료를 읽고 있습니다.' not in await page.locator('#notice').inner_text()
    finally:
        await page.close()
    page = await browser.new_page()
    try:
        await page.goto(readonly+'#library')
        await expect(page.locator('#notice')).to_contain_text('CONNECTION_UNAVAILABLE')
        await expect(page.locator('.record')).to_have_count(0)
        await expect(page.locator('.simulation')).to_have_count(0)
    finally:
        await page.close()


async def check_mobile_return(browser, base):
    page = await browser.new_page(viewport={'width': 390, 'height': 844})
    try:
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
        await expect(page.locator('.body-text:not(.source-body)')).to_have_text(RECORD_TEXT)
        async with page.expect_response(lambda response: response.url.endswith('/api/source')) as source_response:
            await page.get_by_role('button', name='출처 본문 조회', exact=True).first.click()
        assert (await (await source_response.value).json())['result']['content']['text'] == SOURCE_TEXT
        await expect(page.locator('pre.source-body')).to_have_text(SOURCE_TEXT)
        assert await page.locator('pre.source-body').text_content() == SOURCE_TEXT
        await expect(page.locator('pre.source-body mark')).to_have_text('🧭 동쪽')
        await expect(page.locator('.source-verification')).to_have_text('위치 검증: VERIFIED · HASH_AND_RANGE_MATCH')
        await expect(page.locator('.body-text:not(.source-body)')).to_have_text(RECORD_TEXT)
        assert await page.locator('.body-text:not(.source-body)').text_content() == RECORD_TEXT
        await expect(page.locator('.body-text:not(.source-body) mark')).to_have_count(0)
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
    servers = [make_source_fixture_server(), make_server(0, mode='readonly')]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    base, readonly = [f'http://127.0.0.1:{server.server_port}' for server in servers]
    checks = [check_http_errors, check_mode_denial, check_capabilities, check_history_availability,
              check_history_retry, check_relation_targets, check_readonly_boundary, check_mobile_return]
    results = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
            errors = []
            browser.on('page', lambda page: page.on('pageerror', lambda exc: errors.append(type(exc).__name__)))
            for check in checks:
                try:
                    await check(browser, base, readonly) if check is check_readonly_boundary else await check(browser, base)
                    results.append({'check': check.__name__, 'status': 'passed'})
                except Exception:
                    results.append({'check': check.__name__, 'status': 'failed'})
                    print(json.dumps({'checks': results}, ensure_ascii=False, indent=2), flush=True)
                    raise
            assert not errors, errors
            print(json.dumps({'passed': len(results), 'failed': 0, 'browser_errors': len(errors), 'checks': results}, ensure_ascii=False, indent=2))
            await browser.close()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
            if hasattr(server.provider, 'close'):
                server.provider.close()


if __name__ == '__main__':
    asyncio.run(main())
