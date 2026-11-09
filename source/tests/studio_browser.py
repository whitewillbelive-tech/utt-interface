"""Studio UI regressions and screenshots using generated fiction only.

Run: PYTHONDONTWRITEBYTECODE=1 python3 source/tests/studio_browser.py

Uses the existing explicit synthetic source fixture and an unconnected readonly
server. No actual source, configured data transport, credentials or original
logs are read. Screenshots are produced only in confirmed synthetic mode and
written to the ignored artifacts directory.
"""
import asyncio
import copy
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.server import make_server
from app.provider import FIELDS, OPERATIONS
from source.tests.source_ui_fixtures import SOURCE_TEXT, make_source_fixture_server
from playwright.async_api import Error as PlaywrightError, async_playwright, expect


ARTIFACTS = ROOT / 'artifacts'
API_PATHS = {'/api/mode', '/api/features', '/api/read', '/api/source'}
FEATURES_RESTRICTED = {'contract_version': 'UTT-READ-FEATURES/0.1.0', 'filters': {
    'query': 'SUPPORTED', 'project': 'CONDITIONAL', 'document_role': 'UNSUPPORTED',
    'review_status': 'UNKNOWN', 'tag': 'UNSUPPORTED', 'source': 'UNSUPPORTED'}}


class Capture:
    """Capture public synthetic scope metadata without writing response logs."""
    def __init__(self, page):
        self.page = page
        self.searches, self.sorts, self.pages, self.projects, self.api, self.network = [], [], [], [], [], []
        self.changed = asyncio.Event()

    async def install(self):
        self.page.on('request', self.request)
        await self.page.route('**/api/read', self.read)

    def request(self, request):
        path = urlsplit(request.url).path
        if request.resource_type in ('fetch', 'xhr'):
            self.network.append(request.resource_type)
        if path.startswith('/api/'):
            self.api.append(path)

    async def read(self, route):
        payload = route.request.post_data_json
        request = payload['request']
        assert set(request) == FIELDS
        assert request['operation'] in OPERATIONS
        response = await route.fetch()
        value = await response.json()
        if value['status'] == 'OK':
            if request['operation'] == 'projects':
                self.projects.extend(copy.deepcopy(value['result']['projects']))
            if request['operation'] == 'search':
                # Search records contain metadata only, including no raw body.
                assert all(row['raw_text'] is None and row['raw_text_sha256'] is None
                           for row in value['result']['records'])
                self.searches.append(copy.deepcopy(request))
                self.sorts.append(payload['sort'])
                self.pages.append(copy.deepcopy(value['result']['records']))
        await route.fulfill(response=response, json=value)
        if request['operation'] == 'search':
            self.changed.set()

    async def action(self, action):
        previous = len(self.searches)
        self.changed.clear()
        await action()
        await asyncio.wait_for(self.changed.wait(), 5)
        assert len(self.searches) > previous
        await expect(self.page.locator('#results')).to_have_attribute('aria-busy', 'false')
        return self.searches[-1]

    def assert_known_api(self):
        assert set(self.api) <= API_PATHS, 'Studio introduced an uncontracted API route.'


async def ready(page, base, capture):
    await capture.install()
    await page.goto(base)
    await expect(page.locator('#studio-home')).to_be_visible()
    await expect(page.locator('#nav-home')).to_have_attribute('aria-current', 'page')
    await expect(page.locator('#results .record')).to_have_count(3)
    await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')
    await expect(page.locator('#home-projects .project-card')).to_have_count(2)


async def keyboard_nav(page, target, heading):
    await page.locator(target).focus()
    await page.locator(target).press('Enter')
    await expect(page.locator(heading)).to_be_focused()


async def no_overflow(page):
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Horizontal document overflow.'


async def settle_ui(page):
    await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def home_project(page, name):
    return page.locator('#home-projects .project-card').filter(
        has=page.get_by_role('heading', name=name, exact=True))


async def check_honest_home_scopes(browser, base, _readonly):
    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
    try:
        capture = Capture(page)
        await ready(page, base, capture)
        assert await page.evaluate('location.hash') == '#studio'
        await expect(page.locator('#library-view')).to_be_hidden()
        await expect(page.locator('#conversations-view')).to_be_hidden()
        records = capture.pages[-1]
        assert len(records) == 3
        expected = {'구름 정원': 2, '모래 시계': 1}
        for project in capture.projects:
            count = sum(any(link['to_id'] == project['project_id']
                            and link['to_dataset_id'] == record['dataset_id']
                            for link in record['project_relations']) for record in records)
            assert count == expected[project['name']]
            await expect(home_project(page, project['name']).locator('.project-count')).to_have_text(
                f'현재 목록 3건 중 {count}건 연결')
        await expect(page.locator('#home-projects')).to_contain_text('전체 자료 수나 최근 활동을 뜻하지 않습니다.')
        await expect(page.locator('#home-continue button')).to_have_count(0)
        await expect(page.locator('#home-continue')).to_contain_text('아직 이 탭에서 연 자료가 없습니다.')
        await expect(page.locator('#home-review').get_by_role('button', name='검토하기: 가상 교차 관계', exact=True)).to_be_enabled()
        await expect(page.locator('#home-review .studio-reading-card')).to_have_count(1)
        await expect(page.locator('#home-discussions')).to_contain_text('대화 기록 연결 전')
        assert '기억을 보관하는 도시의 풍경' not in await page.locator('#home-discussions').inner_text()
        query = await capture.action(lambda: page.locator('#home-review').get_by_role(
            'button', name='보류 자료 찾아보기', exact=True).click())
        assert query['review_status'] == 'HELD' and query['cursor'] is None
        await expect(page.locator('#library-view')).to_be_visible()
        await expect(page.locator('#review')).to_have_value('HELD')
        await expect(page.locator('#results .record')).to_have_count(2)
        capture.assert_known_api()
    finally:
        await page.close()


async def check_project_query_source_return(browser, base, _readonly):
    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
    try:
        capture = Capture(page)
        await ready(page, base, capture)
        query = await capture.action(lambda: home_project(page, '구름 정원').get_by_role(
            'button', name='프로젝트 살펴보기', exact=True).click())
        assert query['project_id'] == 'synthetic-project-cloud'
        await expect(page.locator('#library-view')).to_be_visible()
        query = await capture.action(lambda: page.locator('#search').fill('가상 관측'))
        assert query['project_id'] == 'synthetic-project-cloud' and query['query'] == '가상 관측'
        await expect(page.locator('#results .record')).to_have_count(1)
        origin = page.locator('#results').get_by_role('button', name='가상 관측', exact=True)
        await origin.scroll_into_view_if_needed()
        await origin.focus()
        original_scroll = await page.evaluate('scrollY')
        await origin.press('Enter')
        await expect(page.locator('#detail h2')).to_have_text('가상 관측')
        await expect(page.locator('#filters')).to_be_hidden()
        await expect(page.locator('#library-view > .view-header')).to_be_hidden()
        await page.locator('.skip').focus()
        await page.locator('.skip').press('Enter')
        await expect(page.locator('#detail .reading-header h2')).to_be_focused()
        source = page.locator('.source').first
        await expect(source.locator('details').first).to_be_visible()
        assert not await source.locator('details').first.evaluate('node => node.open')
        await source.get_by_role('button', name='출처 본문 조회', exact=True).click()
        await expect(source.locator('.source-body')).to_have_text(SOURCE_TEXT)
        await expect(source.locator('.location-status')).to_be_visible()
        await expect(source.locator('.location-status')).to_have_text('본문과 위치가 일치합니다.')
        await expect(source.locator('.source-target')).to_have_text('출처 본문')
        await expect(source.locator('.source-verification')).to_contain_text('VERIFIED')
        await expect(source.locator('.source-body mark')).to_have_text('🧭 동쪽')
        await no_overflow(page)
        await expect(page.locator('#mode')).to_contain_text('합성')
        ARTIFACTS.mkdir(exist_ok=True)
        await page.evaluate('scrollTo(0, 0)')
        await page.screenshot(path=str(ARTIFACTS / 'studio-detail.png'), full_page=True)
        await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
        await expect(origin).to_be_focused()
        await expect(page.locator('#filters')).to_be_visible()
        await expect(page.locator('#library-view > .view-header')).to_be_visible()
        await expect(page.locator('#search')).to_have_value('가상 관측')
        await expect(page.locator('#project')).to_have_value('synthetic-project-cloud')
        await expect(page.locator('#page-number')).to_have_text('1 페이지')
        await page.wait_for_function('expected => Math.abs(scrollY - expected) <= 3', arg=original_scroll)
        capture.assert_known_api()
    finally:
        await page.close()


async def check_scoped_resume_after_changed_query(browser, base, _readonly):
    page = await browser.new_page(viewport={'width': 390, 'height': 844})
    try:
        capture = Capture(page)
        await ready(page, base, capture)
        await keyboard_nav(page, '#nav-library', '#workspace-title')
        await capture.action(lambda: page.locator('#search').fill('가상'))
        await capture.action(lambda: page.locator('#role').select_option('SYNTHETIC_NOTE'))
        original_query = copy.deepcopy(await capture.action(lambda: page.locator('#next').click()))
        assert original_query['cursor'] is not None
        await expect(page.locator('#page-number')).to_have_text('2 페이지')
        origin = page.locator('#results .record-title[data-record-id="synthetic-record-006"]')
        await origin.scroll_into_view_if_needed()
        await origin.focus()
        original_scroll = await page.evaluate('scrollY')
        await origin.press('Enter')
        await expect(page.locator('#detail h2')).to_have_text('가상 이력')
        await page.get_by_role('button', name='관련 자료: 가상 관측', exact=True).click()
        await expect(page.locator('#detail h2')).to_have_text('가상 관측')
        await no_overflow(page)
        await keyboard_nav(page, '#nav-home', '#home-heading')
        await expect(page.locator('#home-continue').get_by_role('button', name='이어서 읽기: 가상 관측', exact=True)).to_be_visible()
        await expect(page.locator('#home-continue').get_by_role('button', name='이어서 읽기: 가상 이력', exact=True)).to_be_visible()
        await expect(home_project(page, '구름 정원').locator('.project-count')).to_have_text('현재 목록 3건 중 1건 연결')
        await expect(home_project(page, '모래 시계').locator('.project-count')).to_have_text('현재 목록 3건 중 2건 연결')
        # Metadata-only resume state must not persist any full body.
        storage = await page.evaluate('JSON.stringify([Object.values(sessionStorage), Object.values(localStorage)])')
        assert SOURCE_TEXT not in storage
        await keyboard_nav(page, '#nav-library', '#workspace-title')
        await capture.action(lambda: page.locator('#search').fill('가상 미분류'))
        await expect(page.locator('#results .record')).to_have_count(1)
        await capture.action(lambda: page.locator('#sort').select_option('id_desc'))
        assert capture.sorts[-1] == 'id_desc'
        await keyboard_nav(page, '#nav-home', '#home-heading')
        restored = await capture.action(lambda: page.locator('#home-continue').get_by_role(
            'button', name='이어서 읽기: 가상 관측', exact=True).press('Enter'))
        for field in ('dataset_id', 'snapshot_id', 'query', 'project_id', 'document_role',
                      'review_status', 'cursor', 'page_size'):
            assert restored[field] == original_query[field], field
        await expect(page.locator('#detail h2')).to_have_text('가상 관측')
        await expect(page.locator('#search')).to_have_value('가상')
        await expect(page.locator('#role')).to_have_value('SYNTHETIC_NOTE')
        await expect(page.locator('#sort')).to_have_value('id_asc')
        assert capture.sorts[-1] == 'id_asc'
        await expect(page.locator('#page-number')).to_have_text('2 페이지')
        await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
        await expect(origin).to_be_focused()
        await page.wait_for_function('expected => Math.abs(scrollY - expected) <= 3', arg=original_scroll)
        await no_overflow(page)
        capture.assert_known_api()
    finally:
        await page.close()


async def check_cancelled_debounce_search_and_resume(browser, base, _readonly):
    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
    pending, plans, read_operations = None, [], []

    def record_operation(request):
        if urlsplit(request.url).path == '/api/read':
            read_operations.append(request.post_data_json['request']['operation'])

    def delay_next(query, stale_title=False):
        plan = {'query': query, 'stale_title': stale_title, 'used': False,
                'started': asyncio.Event(), 'release': asyncio.Event(), 'done': asyncio.Event()}
        plans.append(plan)
        return plan

    async def delayed_read(route):
        query = route.request.post_data_json['request']
        active = pending
        if (active is None or active['used'] or query['operation'] != 'search'
                or query['query'] != active['query']):
            return await route.fallback()
        active['used'] = True
        response = await route.fetch()
        envelope = await response.json()
        assert envelope['status'] == 'OK'
        if active['stale_title']:
            # A distinguishable, otherwise valid fictional response proves a
            # late request cannot overwrite a newer scope or current page.
            envelope['result']['records'][0]['title'] = '지연된 이전 합성 응답'
        active['started'].set()
        await active['release'].wait()
        try:
            await route.fulfill(response=response, json=envelope)
        except PlaywrightError as error:
            if not any(word in str(error).lower() for word in ('closed', 'aborted', 'interception')):
                raise
        finally:
            active['done'].set()

    try:
        capture = Capture(page)
        page.on('request', record_operation)
        await ready(page, base, capture)
        feature_calls = capture.api.count('/api/features')
        source_calls = capture.api.count('/api/source')
        await keyboard_nav(page, '#nav-library', '#workspace-title')
        old_page = await capture.action(lambda: page.locator('#next').click())
        assert old_page['cursor'] is not None
        await expect(page.locator('#page-number')).to_have_text('2 페이지')
        search_count = len(capture.searches)
        search_requests = read_operations.count('search')
        # Trigger input and route navigation in one browser task, so the
        # debounce cannot fire before the user leaves the library.
        await page.evaluate("""() => {
            const input = document.querySelector('#search');
            input.value = '가상 미분류';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            document.querySelector('#nav-home').click();
        }""")
        await expect(page.locator('#studio-home')).to_be_visible()
        # The UI debounce is 180 ms. Keep the home route active beyond that
        # window so a forgotten timer cannot hide behind the next action.
        await page.wait_for_timeout(250)
        assert len(capture.searches) == search_count
        assert read_operations.count('search') == search_requests
        returned = await capture.action(lambda: page.go_back())
        assert returned['query'] == '가상 미분류' and returned['cursor'] is None
        await expect(page.locator('#page-number')).to_have_text('1 페이지')
        await expect(page.locator('#results .record-title')).to_have_text('가상 미분류')
        await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')
        await settle_ui(page)
        assert read_operations.count('search') == search_requests + 1, 'Browser Back issued duplicate searches.'
        assert len(capture.searches) == search_count + 1
        assert await page.evaluate('location.hash') == '#library'

        # Hold the first response, leave, and allow the replacement request to
        # complete independently on return. Then select a newer query before
        # releasing the old response, making any stale overwrite observable.
        await page.route('**/api/read', delayed_read)
        pending = delay_next('가상 관측', stale_title=True)
        await page.locator('#search').fill('가상 관측')
        await asyncio.wait_for(pending['started'].wait(), 5)
        await expect(page.locator('#results')).to_have_attribute('aria-busy', 'true')
        await keyboard_nav(page, '#nav-home', '#home-heading')
        await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')
        replaced = await capture.action(lambda: page.locator('#nav-library').click())
        assert replaced['query'] == '가상 관측' and replaced['cursor'] is None
        await expect(page.locator('#results .record-title')).to_have_text('가상 관측')
        latest = await capture.action(lambda: page.locator('#search').fill('가상 미분류'))
        assert latest['query'] == '가상 미분류' and latest['cursor'] is None
        pending['release'].set()
        await asyncio.wait_for(pending['done'].wait(), 5)
        await settle_ui(page)
        await expect(page.locator('#results .record-title')).to_have_text('가상 미분류')
        await expect(page.locator('#page-number')).to_have_text('1 페이지')
        await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')
        assert '지연된 이전 합성 응답' not in await page.locator('#results').inner_text()

        # A pending resume restores its search scope first. Navigating away
        # must cancel that step without issuing its later detail request.
        await page.locator('#results').get_by_role('button', name='가상 미분류', exact=True).click()
        await expect(page.locator('#detail h2')).to_have_text('가상 미분류')
        await keyboard_nav(page, '#nav-home', '#home-heading')
        pending = delay_next('가상 미분류')
        detail_calls = read_operations.count('detail')
        await page.locator('#home-continue').get_by_role(
            'button', name='이어서 읽기: 가상 미분류', exact=True).click()
        await asyncio.wait_for(pending['started'].wait(), 5)
        await expect(page.locator('#library-view')).to_be_visible()
        await expect(page.locator('#results')).to_have_attribute('aria-busy', 'true')
        await keyboard_nav(page, '#nav-home', '#home-heading')
        pending['release'].set()
        await asyncio.wait_for(pending['done'].wait(), 5)
        await settle_ui(page)
        await expect(page.locator('#studio-home')).to_be_visible()
        await expect(page.locator('#library-view')).to_be_hidden()
        await expect(page.locator('#detail h2')).to_have_count(0)
        await expect(page.locator('#results')).to_have_attribute('aria-busy', 'false')
        assert read_operations.count('detail') == detail_calls
        assert capture.api.count('/api/features') == feature_calls
        assert capture.api.count('/api/source') == source_calls
        capture.assert_known_api()
    finally:
        for plan in plans:
            plan['release'].set()
        await page.close()


async def check_readonly_and_unsupported_boundaries(browser, base, readonly):
    page = await browser.new_page()
    try:
        capture = Capture(page)
        await capture.install()
        await page.goto(readonly)
        await expect(page.locator('#studio-home')).to_be_visible()
        await expect(page.locator('#notice')).to_contain_text('CONNECTION_UNAVAILABLE')
        await expect(page.locator('#home-projects .project-card')).to_have_count(0)
        await expect(page.locator('#home-continue button')).to_have_count(0)
        await expect(page.locator('#results .record')).to_have_count(0)
        await expect(page.locator('#home-discussions').get_by_role('button', name='합성 대화 보기 예시', exact=True)).to_be_disabled()
        await keyboard_nav(page, '#nav-conversations', '#conversations-heading')
        await expect(page.locator('#conversation-content')).to_contain_text('대화 구조 연결 전')
        await expect(page.locator('#conversation-content .conversation-view')).to_have_count(0)
        assert not capture.searches
        capture.assert_known_api()
    finally:
        await page.close()
    page = await browser.new_page()
    try:
        await page.route('**/api/mode', lambda route: route.fulfill(
            json={'mode': 'readonly', 'simulation_namespace': None}))
        await page.route('**/api/features', lambda route: route.fulfill(json=FEATURES_RESTRICTED))
        capture = Capture(page)
        await ready(page, base, capture)
        for button in await page.locator('#home-projects .project-card button').all():
            await expect(button).to_be_disabled()
        for name in ('보류 자료 찾아보기', '근거 요청 자료 찾아보기'):
            await expect(page.locator('#home-review').get_by_role('button', name=name, exact=True)).to_be_disabled()
        await keyboard_nav(page, '#nav-library', '#workspace-title')
        for selector in ('#project', '#role', '#review'):
            await expect(page.locator(selector)).to_be_disabled()
        assert all(q['project_id'] is None and q['document_role'] is None
                   and q['review_status'] is None for q in capture.searches)
        await keyboard_nav(page, '#nav-conversations', '#conversations-heading')
        await expect(page.locator('#conversation-content .conversation-view')).to_have_count(0)
        await expect(page.locator('#conversation-content')).to_contain_text('실제 대화를 가상 대화로 대체하지 않습니다.')
        capture.assert_known_api()
    finally:
        await page.close()


async def check_conversation_demo_component_and_escaping(browser, base, _readonly):
    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
    try:
        capture = Capture(page)
        await ready(page, base, capture)
        before = len(capture.api)
        before_network = len(capture.network)
        await keyboard_nav(page, '#nav-conversations', '#conversations-heading')
        await expect(page.locator('.conversation-view .demo-label')).to_contain_text('합성 대화 시연')
        await expect(page.locator('.conversation-view .conversation-topic')).to_have_text('기억을 보관하는 도시의 풍경')
        await expect(page.locator('.conversation-summary')).to_be_visible()
        await expect(page.locator('.conversation-user')).to_contain_text('사용자 의견')
        await expect(page.locator('.conversation-ai')).to_contain_text('AI 의견')
        await expect(page.locator('.conversation-outcome')).to_contain_text('아직 확정된 결론이 없습니다.')
        choose = page.locator('.conversation-choices button').nth(1)
        await choose.focus()
        await choose.press('Enter')
        await expect(page.locator('.conversation-topic')).to_be_focused()
        await expect(page.locator('.conversation-outcome')).to_contain_text('실제 확정 아님')
        originals = page.locator('.conversation-originals')
        assert not await originals.evaluate('node => node.open')
        await originals.locator('summary').focus()
        await originals.locator('summary').press('Enter')
        await expect(originals.locator('.conversation-message')).to_have_count(2)
        await expect(originals.locator('.conversation-message').first).to_be_visible()
        await no_overflow(page)
        await expect(page.locator('#mode')).to_contain_text('합성')
        ARTIFACTS.mkdir(exist_ok=True)
        await page.screenshot(path=str(ARTIFACTS / 'studio-discussion.png'), full_page=True)
        await settle_ui(page)
        assert len(capture.api) == before, 'Conversation demo must not fetch a made-up conversation API.'
        assert len(capture.network) == before_network, 'Conversation demo must not issue fetch/XHR requests.'
        literal = '합성 <svg onload="window.studioEscaped=true"> & <b>표시</b>'
        model = {'topic': literal, 'discussion': literal, 'userOpinion': literal,
                 'aiOpinion': literal, 'conclusion': literal, 'status': literal,
                 'messages': [{'speaker': 'user', 'text': literal},
                              {'speaker': 'assistant', 'text': literal}],
                 'metadata': {'fictional_markup': literal}}
        await page.evaluate("""async model => {
            const {createConversationView} = await import('/app.mjs');
            const view = createConversationView(model);
            view.id = 'escaped-conversation';
            document.querySelector('#conversation-content').replaceChildren(view);
        }""", model)
        custom = page.locator('#escaped-conversation')
        for selector in ('.conversation-topic', '.conversation-summary', '.conversation-user p', '.conversation-ai p'):
            await expect(custom.locator(selector)).to_have_text(literal)
        await expect(custom.locator('svg,img,script,b')).to_have_count(0)
        await custom.locator('.conversation-originals summary').focus()
        await custom.locator('.conversation-originals summary').press('Enter')
        await expect(custom.locator('.conversation-message p').first).to_have_text(literal)
        assert await page.evaluate('window.studioEscaped === undefined')
        await settle_ui(page)
        assert len(capture.api) == before
        assert len(capture.network) == before_network
        capture.assert_known_api()
    finally:
        await page.close()


async def check_desktop_mobile_layout_and_screenshots(browser, base, _readonly):
    for width, height, name in ((1440, 1000, 'studio-desktop.png'), (390, 844, 'studio-mobile.png')):
        page = await browser.new_page(viewport={'width': width, 'height': height})
        try:
            capture = Capture(page)
            await ready(page, base, capture)
            await no_overflow(page)
            await keyboard_nav(page, '#nav-library', '#workspace-title')
            await no_overflow(page)
            await keyboard_nav(page, '#nav-conversations', '#conversations-heading')
            await no_overflow(page)
            await keyboard_nav(page, '#nav-home', '#home-heading')
            await expect(page.locator('#mode')).to_contain_text('합성')
            await no_overflow(page)
            ARTIFACTS.mkdir(exist_ok=True)
            await page.screenshot(path=str(ARTIFACTS / name), full_page=True)
            capture.assert_known_api()
        finally:
            await page.close()


async def main():
    servers = [make_source_fixture_server(), make_server(0, mode='readonly')]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    base, readonly = [f'http://127.0.0.1:{server.server_port}' for server in servers]
    checks = [check_honest_home_scopes, check_project_query_source_return,
              check_scoped_resume_after_changed_query, check_cancelled_debounce_search_and_resume,
              check_readonly_and_unsupported_boundaries,
              check_conversation_demo_component_and_escaping,
              check_desktop_mobile_layout_and_screenshots]
    results, browser_errors = [], []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path='/usr/bin/chromium',
                                               headless=True, args=['--no-sandbox'])
            browser.on('page', lambda page: page.on('pageerror', lambda exc: browser_errors.append(type(exc).__name__)))
            for check in checks:
                try:
                    await check(browser, base, readonly)
                    results.append({'check': check.__name__, 'status': 'passed'})
                except Exception:
                    results.append({'check': check.__name__, 'status': 'failed'})
                    print(json.dumps({'checks': results}, ensure_ascii=False, indent=2), flush=True)
                    raise
            assert not browser_errors, browser_errors
            print(json.dumps({'passed': len(results), 'failed': 0, 'browser_errors': len(browser_errors),
                              'browser': browser.version, 'checks': results,
                              'synthetic_screenshots': ['artifacts/studio-desktop.png', 'artifacts/studio-mobile.png',
                                                        'artifacts/studio-discussion.png', 'artifacts/studio-detail.png']},
                             ensure_ascii=False, indent=2))
            await browser.close()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
            if hasattr(server.provider, 'close'):
                server.provider.close()


if __name__ == '__main__':
    asyncio.run(main())
