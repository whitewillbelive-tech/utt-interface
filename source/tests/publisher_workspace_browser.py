"""Real Chromium against a wholly fictional in-memory workspace snapshot."""
import asyncio
import hashlib
from pathlib import Path
import sqlite3
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.provider import ReadOnlyAdapter
from app.publisher_workspace_reader import WorkspaceSnapshotProvider
from app.server import make_server
from playwright.async_api import async_playwright, expect


def fixture():
    db = sqlite3.connect(':memory:')
    try:
        db.executescript('''
            CREATE TABLE work_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE work_records(record_id TEXT PRIMARY KEY,title TEXT,body TEXT,
              body_sha256 TEXT,summary TEXT,document_role TEXT,review_status TEXT,
              archived_at TEXT,merged_into TEXT);
            INSERT INTO work_meta VALUES ('format_version','UTT-INTEGRATED-WORKSPACE/0.1');
            INSERT INTO work_meta VALUES ('dataset_id','fictional-browser');
        ''')
        for n in range(1, 6):
            title = f'가상 노트 {n}'
            body = f'가상 기록 본문 {n}'
            db.execute('INSERT INTO work_records VALUES (?,?,?,?,?,?,?,?,?)',
                       (f'fictional-{n}', title, body, hashlib.sha256(body.encode()).hexdigest(),
                        None, 'NOTE', 'UNREVIEWED', None, None))
        return db.serialize()
    finally:
        db.close()


async def run():
    provider = WorkspaceSnapshotProvider(fixture())
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
                await expect(page.locator('#project')).to_be_disabled()
                await expect(page.locator('#role')).to_be_disabled()
                await expect(page.locator('#review')).to_be_disabled()
                await page.locator('#search').fill('가상')
                await page.locator('#filters button[type=submit]').click()
                await expect(page.locator('.record')).to_have_count(3)
                await page.locator('#next').click()
                await expect(page.locator('#page-number')).to_have_text('2 페이지')
                await page.get_by_role('button', name='가상 노트 4', exact=True).click()
                await expect(page.locator('#detail h2')).to_have_text('가상 노트 4')
                await expect(page.locator('.body-text:not(.source-body)')).to_have_text('가상 기록 본문 4')
                await page.get_by_role('button', name='← 검색 결과로', exact=True).click()
                await expect(page.locator('#page-number')).to_have_text('2 페이지')
                await expect(page.locator('#search')).to_have_value('가상')
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
