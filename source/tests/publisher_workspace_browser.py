"""Real Chromium against a wholly fictional in-memory workspace snapshot."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.provider import ReadOnlyAdapter
from app.publisher_workspace import _REFERENCE
from app.publisher_workspace_reader import WorkspaceSnapshotProvider
from app.server import make_server
from playwright.async_api import async_playwright, expect


def fixture():
    db = sqlite3.connect(':memory:')
    try:
        db.executescript(_REFERENCE.read_text(encoding='utf-8'))
        db.executemany('INSERT INTO work_meta VALUES (?,?)', [('format_version','UTT-INTEGRATED-WORKSPACE/0.1'),('dataset_id','fictional-browser')])
        for n in range(1, 6):
            title = f'가상 노트 {n}'
            body = f'가상 기록 본문 {n}'
            db.execute('''INSERT INTO work_records(record_id,title,body,body_sha256,document_role,review_status,
                       adoption_status,archived_at,merged_into) VALUES (?,?,?,?,?,?,?,?,?)''',
                       (f'fictional-{n}', title, body, hashlib.sha256(body.encode()).hexdigest(),
                        'NOTE','UNREVIEWED','UNREVIEWED','가상 보관' if n in (2,4) else None,'fictional-1' if n == 4 else None))
        db.execute("INSERT INTO work_projects(project_id,name) VALUES ('project-a','가상 프로젝트')")
        db.execute("INSERT INTO work_record_projects(link_id,record_id,project_id,role,basis) VALUES ('link-a','fictional-4','project-a','PRIMARY','CANDIDATE')")
        db.execute("INSERT INTO work_topics(topic_id,name) VALUES ('topic-a','가상 주제')")
        db.execute("INSERT INTO work_record_topics(record_id,topic_id,status) VALUES ('fictional-4','topic-a','INHERITED')")
        db.executemany('INSERT INTO work_sources(evidence_id,record_id,source_id,locator,validated_in_this_workspace) VALUES (?,?,?,?,?)',[
            ('evidence-a','fictional-4','source-a','가상 위치 A',0),('evidence-b','fictional-4','source-a','가상 위치 B',1)])
        db.execute("INSERT INTO work_decisions(decision_id,record_id,decision,actor,user_authorized) VALUES ('decision-a','fictional-4','UNRESOLVED','USER',0)")
        db.execute("INSERT INTO work_events(event_id,event_kind,record_id,record_version,actor,reason,detail_json,event_hash) VALUES ('event-a','EDIT','fictional-4',1,'USER','가상 편집','{}','fictional-hash')")
        db.execute("INSERT INTO work_revisions(record_id,version,title,body,body_sha256,review_status,adoption_status,decision_status) SELECT record_id,version,title,body,body_sha256,review_status,adoption_status,decision_status FROM work_records WHERE record_id='fictional-4'")
        db.commit()
        return db.serialize()
    finally:
        db.close()


async def run():
    provider = WorkspaceSnapshotProvider(fixture(), lifecycle_enabled=True)
    server = make_server(0, mode='readonly', provider=ReadOnlyAdapter(provider.read))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
            try:
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{server.server_port}/#library')
                await expect(page.locator('.record')).to_have_count(3)
                await expect(page.locator('.record').nth(1).locator('.workspace-lifecycle')).to_contain_text('보관: 가상 보관')
                await expect(page.locator('#project')).to_be_disabled()
                await expect(page.locator('#role')).to_be_disabled()
                await expect(page.locator('#review')).to_be_disabled()
                await page.locator('#search').fill('가상')
                await page.locator('#filters button[type=submit]').click()
                await expect(page.locator('.record')).to_have_count(3)
                await page.locator('#next').click()
                await expect(page.locator('#page-number')).to_have_text('2 페이지')
                await expect(page.locator('.record').first.locator('.workspace-lifecycle')).to_contain_text('병합 대상: fictional-1')
                await page.get_by_role('button', name='가상 노트 4', exact=True).click()
                await expect(page.locator('#detail h2')).to_have_text('가상 노트 4')
                await expect(page.locator('.body-text:not(.source-body)')).to_have_text('가상 기록 본문 4')
                await expect(page.locator('.workspace-detail')).to_contain_text('프로젝트 관계 · 1건')
                await page.locator('.workspace-detail summary').filter(has_text='프로젝트 관계').click()
                await expect(page.locator('.workspace-detail')).to_contain_text('가상 프로젝트')
                await page.locator('.workspace-detail summary').filter(has_text='출처 근거').click()
                await expect(page.locator('.workspace-detail')).to_contain_text('가상 위치 A')
                await expect(page.locator('.workspace-detail')).to_contain_text('가상 위치 B')
                await page.locator('.workspace-detail summary').filter(has_text='사건 이력').click()
                await expect(page.locator('.workspace-detail')).to_contain_text('가상 편집')
                await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
                await expect(page.locator('#page-number')).to_have_text('2 페이지')
                await expect(page.locator('#search')).to_have_value('가상')
                catalog = page.locator('#workspace-catalog')
                await catalog.locator(':scope > summary').click()
                await catalog.get_by_text('프로젝트', exact=True).click()
                await catalog.get_by_text('가상 프로젝트', exact=True).click()
                await expect(catalog.get_by_role('button', name='가상 노트 4', exact=True)).to_be_visible()
                await catalog.get_by_text('주제', exact=True).click()
                await catalog.get_by_text('가상 주제', exact=True).click()
                await expect(catalog.get_by_role('button', name='가상 노트 4', exact=True)).to_have_count(2)
                waiting, release = asyncio.Event(), asyncio.Event()
                async def late(route):
                    request = json.loads(route.request.post_data)
                    if request['record_id'] == 'fictional-4':
                        reply = await route.fetch()
                        waiting.set()
                        await release.wait()
                        try:
                            await route.fulfill(response=reply)
                        except Exception:
                            pass  # Browser may cancel the old request after navigation.
                    else:
                        await route.continue_()
                await page.route('**/api/workspace/read', late)
                await page.locator('#results').get_by_role('button', name='가상 노트 4', exact=True).click()
                await asyncio.wait_for(waiting.wait(), timeout=10)
                await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
                await page.get_by_role('button', name='가상 노트 5', exact=True).click()
                await expect(page.locator('#detail h2')).to_have_text('가상 노트 5')
                release.set()
                await expect(page.locator('.workspace-detail')).to_contain_text('출처 근거 · 0건')
                await expect(page.locator('.workspace-detail')).not_to_contain_text('가상 위치 A')
                assert not errors, errors
                print('Fictional workspace Chromium UI flow: PASS')
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        provider.close()


if __name__ == '__main__':
    asyncio.run(run())
