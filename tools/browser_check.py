"""Real Chromium UI and HTTP checks; fictional inputs only. No external URLs."""
import asyncio
import json
from pathlib import Path
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.server import make_server
from app.provider import request
from playwright.async_api import async_playwright, expect

ROOT=Path(__file__).resolve().parents[1]

async def run():
    servers=[make_server(0),make_server(0,mode='readonly')]
    for server in servers:threading.Thread(target=server.serve_forever,daemon=True).start()
    checks=[]
    try:
      async with async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        page=await browser.new_page(viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        base=f'http://127.0.0.1:{servers[0].server_port}'
        await page.goto(base)
        await expect(page.locator('.record')).to_have_count(3)
        await expect(page.locator('#dataset')).to_contain_text('SYNTHETIC')
        await expect(page.locator('#components')).to_contain_text('가상 읽기 구성요소')
        checks.append('bootstrap projects, components, dataset and paged list')
        # Inspect actual API boundary: only UI assets and read contract exposed.
        response=await page.request.get(base+'/HANDOFF_MANIFEST.json');assert response.status==404
        response=await page.request.post(base+'/api/write',data={});assert response.status==405
        response=await page.request.post(base+'/api/read',data={'request':request('capabilities')},headers={'Origin':'https://untrusted.invalid'});assert response.status==403
        cap=(await (await page.request.post(base+'/api/read',data={'request':request('capabilities')})).json())
        scope=dict(dataset_id=cap['snapshot']['dataset_id'],snapshot_id=cap['snapshot']['snapshot_id'])
        response=await page.request.post(base+'/api/read',data={'request':request('search',**scope)})
        listing=await response.json();assert all(r['raw_text'] is None for r in listing['result']['records'])
        checks.append('HTTP read-only, origin guard, static allowlist and no list bodies')
        await page.locator('#next').click();await expect(page.locator('#page-number')).to_have_text('2 페이지')
        await page.get_by_role('button',name='가상 출처 누락',exact=True).click()
        await expect(page.locator('#detail')).to_contain_text('누락 확인')
        await expect(page.locator('#detail')).to_contain_text('원문 미확인')
        await page.get_by_role('button',name='출처 위치 확인',exact=True).click()
        await expect(page.locator('.source')).to_contain_text('좌표를 적용하지 않습니다')
        await page.get_by_role('button',name='← 검색 결과로',exact=True).click()
        await expect(page.locator('#page-number')).to_have_text('2 페이지')
        await expect(page.get_by_role('button',name='가상 출처 누락',exact=True)).to_be_focused()
        checks.append('next page, missing source, unavailable body, return page and focus')
        await page.get_by_role('button',name='가상 이력',exact=True).click()
        await expect(page.locator('#detail')).to_contain_text('synthetic-event-002')
        await expect(page.locator('#detail')).to_contain_text('정정 대상 synthetic-event-001')
        await page.get_by_role('button',name='관련 자료: 가상 관측',exact=True).click()
        await expect(page.locator('#detail h2')).to_have_text('가상 관측')
        await expect(page.locator('.source')).to_have_count(2)
        await page.get_by_role('button',name='출처 위치 확인',exact=True).first.click()
        await expect(page.locator('mark')).to_have_text('🧭 동쪽')
        await expect(page.locator('.body-text')).to_contain_text('실제 대화나 문서를 사용하지 않고')
        checks.append('immutable event chain, related record navigation, multiple sources, Korean body and UTF-16 highlight')
        await page.locator('#simulation-note').fill('가상의 근거를 추가 확인하는 연습')
        await page.get_by_role('button',name='합성 세션에 기록',exact=True).click()
        await expect(page.locator('.simulation ol')).to_contain_text('synthetic-session-1')
        await expect(page.locator('.simulation')).to_contain_text('실제 승인·동기화 아님')
        # No operational state is promoted by the separate session simulation.
        await expect(page.locator('#detail .states')).to_contain_text('미승인')
        await page.get_by_role('button',name='← 검색 결과로',exact=True).click()
        await page.get_by_role('button',name='이전 페이지',exact=True).click()
        await expect(page.locator('#page-number')).to_have_text('1 페이지')
        await page.get_by_role('button',name='가상 관측',exact=True).click()
        await expect(page.locator('.simulation ol')).to_contain_text('synthetic-session-1')
        await page.get_by_role('button',name='이 탭의 합성 연습 기록 지우기',exact=True).click()
        await expect(page.locator('.simulation ol li')).to_have_count(0)
        await page.get_by_role('button',name='← 검색 결과로',exact=True).click()
        checks.append('session-only simulation persistence, immutable original adoption, explicit cleanup')
        await page.get_by_role('button',name='가상 미분류',exact=True).click()
        await expect(page.locator('#detail')).to_contain_text('미분류')
        await page.get_by_role('button',name='← 검색 결과로',exact=True).click()
        await page.locator('#project').select_option('synthetic-project-cloud')
        await page.locator('#review').select_option('HELD')
        await expect(page.locator('.record')).to_have_count(2)
        await page.locator('#sort').select_option('id_desc')
        await expect(page.locator('.record-title').first).to_have_text('가상 충돌 보류')
        await page.get_by_role('button',name='필터 초기화',exact=True).click()
        await expect(page.locator('.record')).to_have_count(3)
        await expect(page.locator('#project')).to_have_value('')
        await page.locator('#search').fill('전혀-없는-가상-문구')
        await expect(page.locator('#results')).to_contain_text('조건에 맞는 기록이 없습니다')
        await page.locator('#search').fill('🧭 동쪽')
        await expect(page.locator('.record')).to_have_count(1)
        await expect(page.locator('.record-title')).to_have_text('가상 관측')
        checks.append('unassigned state, combined filters, descending order, reset, empty result and body search')
        await page.locator('summary').click()
        await page.locator('#scenario').select_option('stale')
        await expect(page.locator('#notice')).to_contain_text('오래된 버전')
        await expect(page.locator('.record')).to_have_count(0)
        await page.locator('#scenario').select_option('connection')
        await expect(page.locator('#notice')).to_contain_text('CONNECTION_UNAVAILABLE')
        await page.locator('#scenario').select_option('normal')
        await expect(page.locator('.record')).to_have_count(1)
        checks.append('stale snapshot and connection error are distinct from empty results')
        async def delayed(route):
            payload=route.request.post_data_json
            if payload.get('request',{}).get('query')=='older-query':
                await asyncio.sleep(.6)
            try:await route.continue_()
            except Exception:pass  # The superseded request may already be aborted.
        await page.route('**/api/read',delayed)
        await page.locator('#search').fill('older-query')
        await page.wait_for_timeout(240)
        await page.locator('#search').fill('가상 관측')
        await expect(page.locator('.record-title')).to_have_text('가상 관측')
        await page.wait_for_timeout(700)
        await expect(page.locator('.record-title')).to_have_text('가상 관측')
        await page.unroute('**/api/read',delayed)
        checks.append('rapid search changes reject an older delayed request')
        await page.get_by_role('button',name='필터 초기화',exact=True).click()
        await expect(page.locator('.record')).to_have_count(3)
        # Keyboard: Enter submits search and focus indicators remain native.
        await page.locator('#search').focus();await page.keyboard.type('가상 관측');await page.keyboard.press('Enter')
        await expect(page.locator('.record')).to_have_count(1)
        await page.locator('#search').press('Tab');await expect(page.get_by_role('button',name='검색',exact=True)).to_be_focused()
        await page.get_by_role('button',name='필터 초기화',exact=True).click()
        await expect(page.locator('.record')).to_have_count(3)
        artifacts=ROOT/'artifacts';artifacts.mkdir(exist_ok=True)
        await page.screenshot(path=str(artifacts/'reader-desktop.png'),full_page=True)
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.get_by_role('button',name='가상 관측',exact=True).click()
        await expect(page.locator('#detail h2')).to_have_text('가상 관측')
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.screenshot(path=str(artifacts/'reader-mobile.png'),full_page=True)
        checks.append('keyboard submit and tab navigation; desktop and 390px layout without horizontal overflow')
        readonly=f'http://127.0.0.1:{servers[1].server_port}'
        await page.goto(readonly)
        await expect(page.locator('#notice')).to_contain_text('CONNECTION_UNAVAILABLE')
        await expect(page.locator('#mode')).to_contain_text('미연결')
        await expect(page.locator('.record')).to_have_count(0)
        await expect(page.locator('.simulation')).to_have_count(0)
        assert not errors,errors
        checks.append('unconfigured read-only adapter never falls back or enables simulation; no browser errors')
        await browser.close()
        (artifacts/'browser-results.json').write_text(json.dumps({'passed':checks,'failed':[]},ensure_ascii=False,indent=2))
        print(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False,indent=2))
    finally:
        for server in servers:
            server.shutdown();server.server_close()
            if hasattr(server.provider,'close'):server.provider.close()

if __name__=='__main__':asyncio.run(run())
