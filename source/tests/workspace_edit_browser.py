"""Synthetic Chromium/local runtime/publication/reconnect; no Dropbox network."""
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.provider import ReadOnlyAdapter
from app.publisher_workspace_server import load_verified_workspace
from app.server import make_server
from app.workspace_publisher import WorkspacePublisher
from app.workspace_runtime import LocalWorkspaceRuntime
from app.workspace_write import OriginalWorkspaceEventCodec
from tests.test_publisher_pipeline import FakeTransport, initial_objects
from tests.test_publisher_workspace import make_fixture
from tests.test_workspace_end_to_end import _ReaderTransport, _Session
from playwright.async_api import async_playwright, expect


def serve(provider, publisher=None):
    server = make_server(0, mode='readonly', provider=provider, publisher=publisher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def close_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def run():
    with tempfile.TemporaryDirectory(prefix='fictional-browser-') as temp:
        root = Path(temp)
        path = root / 'fictional.sqlite'
        conn = make_fixture(path)
        conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('fictional-project','fictional project')")
        conn.execute("INSERT INTO work_topics(topic_id,name) VALUES ('fictional-topic','fictional topic')")
        conn.commit()
        transport = FakeTransport(initial_objects(conn))
        runtime = LocalWorkspaceRuntime(path, root, codec=OriginalWorkspaceEventCodec())
        publisher = WorkspacePublisher(runtime, 'fictional-key', 'http://127.0.0.1:43155/callback', 'http://127.0.0.1:43155/callback')
        server, thread = serve(runtime, publisher)
        try:
            with patch('app.workspace_publisher.run_publisher_flow', side_effect=lambda *_: _Session()), \
                 patch('app.workspace_publisher.PublisherTransport', return_value=transport):
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
                    try:
                        page = await browser.new_page(viewport={'width':1440,'height':1000})
                        errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        await page.goto(f'http://127.0.0.1:{server.server_port}/#library')
                        await expect(page.get_by_role('button',name='fictional title',exact=True)).to_be_visible()
                        await page.get_by_role('button',name='fictional title',exact=True).click()
                        editor=page.locator('.workspace-editor')
                        await expect(editor).to_be_visible()
                        await editor.get_by_text('내용 수정',exact=True).click()
                        await editor.get_by_label('본문',exact=True).fill('fictional user edit 🧭')
                        await editor.get_by_role('button',name='로컬에 저장',exact=True).click()
                        await expect(page.locator('#workspace-local-state')).to_have_text('로컬 저장 완료',timeout=15000)
                        await expect(page.locator('#detail .body-text:not(.source-body)')).to_have_text('fictional user edit 🧭')
                        await expect(page.locator('.workspace-detail')).to_contain_text('자료 버전 이력 · 2건')
                        await expect(page.locator('#workspace-dropbox-state')).to_have_text('로컬 변경 · Dropbox 반영 전',timeout=15000)
                        await page.get_by_role('button',name='← 검색 결과로',exact=True).click()
                        await page.get_by_role('button',name='새 자료 만들기',exact=True).click()
                        creator=page.locator('#workspace-create-form')
                        await creator.get_by_label('제목',exact=True).fill('fictional new target')
                        await creator.get_by_label('본문',exact=True).fill('fictional new content')
                        await creator.get_by_role('button',name='새 자료 로컬 저장',exact=True).click()
                        await expect(page.locator('#detail h2')).to_have_text('fictional new target',timeout=15000)
                        await page.get_by_role('button',name='이 자료 보관',exact=True).click()
                        await expect(page.get_by_role('button',name='보관·병합 상태 복원',exact=True)).to_be_visible(timeout=15000)
                        await page.get_by_role('button',name='보관·병합 상태 복원',exact=True).click()
                        await expect(page.get_by_role('button',name='이 자료 보관',exact=True)).to_be_visible(timeout=15000)
                        target_id=conn.execute("SELECT record_id FROM work_records WHERE title='fictional new target'").fetchone()[0]
                        await page.get_by_role('button',name='← 검색 결과로',exact=True).click()
                        await page.get_by_role('button',name='fictional title',exact=True).click()
                        editor=page.locator('.workspace-editor')
                        await editor.get_by_text('다른 자료로 병합',exact=True).click()
                        await editor.get_by_label('대상 자료 ID',exact=True).fill(target_id)
                        await editor.get_by_role('button',name='대상 확인',exact=True).click()
                        await expect(editor).to_contain_text('대상: fictional new target · 버전 3')
                        await editor.get_by_role('button',name='확인한 대상으로 병합',exact=True).click()
                        await expect(page.get_by_role('button',name='보관·병합 상태 복원',exact=True)).to_be_visible(timeout=15000)
                        await expect(page.locator('.workspace-detail')).to_contain_text(target_id)
                        await page.get_by_role('button',name='보관·병합 상태 복원',exact=True).click()
                        await expect(page.get_by_role('button',name='이 자료 보관',exact=True)).to_be_visible(timeout=15000)
                        editor=page.locator('.workspace-editor')
                        await editor.get_by_text('프로젝트 연결 변경',exact=True).click()
                        await expect(editor.get_by_label('연결할 프로젝트')).to_contain_text('fictional project')
                        await editor.get_by_label('연결할 프로젝트').select_option('fictional-project')
                        await editor.get_by_role('button',name='선택한 프로젝트 연결',exact=True).click()
                        await expect(page.locator('.workspace-detail')).to_contain_text('프로젝트 관계 · 1건',timeout=15000)
                        editor=page.locator('.workspace-editor')
                        await editor.get_by_text('프로젝트 연결 변경',exact=True).click()
                        await editor.get_by_role('button',name='연결 보관',exact=True).click()
                        await expect(page.locator('.workspace-detail')).to_contain_text('PROJECT_ARCHIVE',timeout=15000)
                        editor=page.locator('.workspace-editor')
                        await editor.get_by_text('프로젝트 연결 변경',exact=True).click()
                        await editor.get_by_role('button',name='연결 복원',exact=True).click()
                        await expect(page.locator('.workspace-detail')).to_contain_text('PROJECT_RESTORE',timeout=15000)
                        editor=page.locator('.workspace-editor')
                        await editor.get_by_text('주제 연결 변경',exact=True).click()
                        await expect(editor.get_by_label('연결할 주제')).to_contain_text('fictional topic')
                        await editor.get_by_label('연결할 주제').select_option('fictional-topic')
                        await editor.get_by_role('button',name='선택한 주제 연결',exact=True).click()
                        await expect(page.locator('.workspace-detail')).to_contain_text('주제 관계 · 1건',timeout=15000)
                        editor=page.locator('.workspace-editor')
                        await editor.get_by_text('태그 변경',exact=True).click()
                        await editor.get_by_label('태그 이름',exact=True).fill('fictional tag')
                        await editor.get_by_role('button',name='태그 추가',exact=True).click()
                        await expect(page.locator('.workspace-detail')).to_contain_text('태그 · 1건',timeout=15000)
                        editor=page.locator('.workspace-editor')
                        await editor.get_by_text('과거 버전으로 복원',exact=True).click()
                        page.once('dialog',lambda dialog:asyncio.create_task(dialog.accept()))
                        await editor.get_by_role('button',name='버전 2 상태 복원',exact=True).click()
                        await expect(page.locator('.workspace-detail')).to_contain_text('자료 버전 이력 · 5건',timeout=15000)
                        publish=page.get_by_role('button',name='Dropbox에 반영',exact=True)
                        await expect(publish).to_be_enabled(timeout=15000)
                        await publish.click()
                        await expect(page.locator('#workspace-dropbox-state')).to_have_text('Dropbox 마지막 발행 확인됨',timeout=30000)
                        assert sum(kind=='cas' for kind,_ in transport.writes)==1
                        assert not errors,errors
                        await page.close()
                        close_server(server,thread)
                        server=None
                        publisher.close()
                        runtime.close()
                        provider=load_verified_workspace(_ReaderTransport(transport))
                        reader,reader_thread=serve(ReadOnlyAdapter(provider.read))
                        try:
                            page=await browser.new_page(viewport={'width':1440,'height':1000})
                            await page.goto(f'http://127.0.0.1:{reader.server_port}/#library')
                            await page.get_by_role('button',name='fictional title',exact=True).click()
                            await expect(page.locator('#detail .body-text:not(.source-body)')).to_have_text('fictional user edit 🧭')
                            await expect(page.locator('.workspace-detail')).to_contain_text('자료 버전 이력 · 5건')
                            await page.get_by_role('button',name='← 검색 결과로',exact=True).click()
                            await page.get_by_role('button',name='fictional new target',exact=True).click()
                            await expect(page.locator('#detail .body-text:not(.source-body)')).to_have_text('fictional new content')
                            await expect(page.locator('.workspace-detail')).to_contain_text('자료 버전 이력 · 3건')
                            await expect(page.get_by_role('button',name='새 자료 만들기',exact=True)).to_be_hidden()
                            print('Fictional edit, publish and verified remote reconnect Chromium flow: PASS')
                        finally:
                            close_server(reader,reader_thread)
                            provider.close()
                    finally:
                        await browser.close()
        finally:
            if server is not None:
                close_server(server,thread)
            publisher.close()
            runtime.close()
            conn.close()


if __name__=='__main__':
    asyncio.run(run())
