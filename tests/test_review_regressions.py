"""HTTP and worker regressions using the production SQLite schema, never live data."""
import os
import io
from openpyxl import load_workbook
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

os.environ['DHB_DISABLE_BACKGROUND_TASKS'] = '1'
import database
import pipeline_manager as pm
from fastapi.testclient import TestClient


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for field, value in [('APP_DB_PATH', self.root / 'db.sqlite'), ('APP_DATA_DIR', self.root),
                             ('APP_TEMP_DIR', self.root), ('load_dhb_settings', lambda: {})]:
            patch = mock.patch.object(database, field, value)
            patch.start()
            self.addCleanup(patch.stop)
        database.init_db()
        import app
        self.app = app
        self.client = TestClient(app.app, base_url='http://127.0.0.1:8686', raise_server_exceptions=False)
        self.headers = {'Origin': 'http://127.0.0.1:8686', 'X-DHB-Token': app.API_TOKEN}
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'shared source fixture')
        self.manager = pm.PipelineManager()
        for field, value in [('pipeline_manager', self.manager), ('APP_TEMP_DIR', self.root)]:
            patch = mock.patch.object(app, field, value)
            patch.start()
            self.addCleanup(patch.stop)
        for field, value in [('_UPLOAD_GAP_STATE_BROKEN', False), ('_COOLDOWN_ANCHOR', None)]:
            patch = mock.patch.object(pm, field, value)
            patch.start()
            self.addCleanup(patch.stop)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO projects(name) VALUES ('review')")
            for i in (901, 902):
                conn.execute("INSERT INTO youtube_channels(id,name,channel_id,studio_category_mode) VALUES(?,?,?,'keep')",
                             (i, f'Channel {i}', 'UC' + ('a' if i == 901 else 'b') * 22))
                conn.execute("INSERT INTO video_queue(id,project_name,title,target_type,target_channel_id,source_type,platform,input_video_path) VALUES(?,?,'Title','CHANNEL',?,'File','LocalFile',?)",
                             (i, 'review' if i == 901 else 'other', i, str(self.source)))

    def row(self, queue_id=901):
        with closing(database.get_conn()) as conn:
            return dict(conn.execute('SELECT * FROM video_queue WHERE id=?', (queue_id,)).fetchone())

    def reserve_review(self, status='NeedsReview'):
        with closing(database.get_conn()) as conn, conn:
            conn.execute('UPDATE video_queue SET status=? WHERE id=901', (status,))
            conn.execute("INSERT INTO content_reservations(content_key,channel_id,queue_id,claim_token,state) VALUES('review-key',901,901,'previous','NeedsReview')")

    def test_render_cannot_change_review_or_legacy_reserved_row(self):
        for status in ('NeedsReview', 'Rendered'):
            with self.subTest(status=status):
                with closing(database.get_conn()) as conn, conn:
                    conn.execute('DELETE FROM content_reservations')
                self.reserve_review(status)
                before = self.row()
                with mock.patch.object(pm, 'render_video') as render:
                    with mock.patch.object(pm.threading, 'Thread'):
                        result = self.client.post('/api/pipeline/start', headers=self.headers,
                            json={'ids':[901,902], 'do_download':False, 'do_render':True, 'do_upload':False})
                    self.assertEqual(result.status_code, 409, result.text)
                    self.manager._process_single_item_safe(901,0,False,True,False,threading.Semaphore(0),'review-test')
                    render.assert_not_called()
                self.assertEqual(self.row(), before)

    def test_legacy_review_row_can_be_reconciled(self):
        self.reserve_review('Rendered')
        response = self.client.get('/api/queue?project=review', headers=self.headers)
        self.assertTrue(response.json()[0]['requires_review'])
        response = self.client.post('/api/queue/901/manual-reupload', headers=self.headers,
            json={'action':'confirmed_published','reason':'Verified offline fixture','published_url':'https://youtu.be/dQw4w9WgXcQ'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row()['status'], 'Completed')

    def test_shared_source_remains_after_success(self):
        options = [False,False,True]
        token = self.manager.preview_distribution([901], options)['preview_token']
        self.manager._freeze_batch_assignments([901],token,options,'shared-test')
        with mock.patch.object(pm,'upload_video_to_youtube', return_value={'url':'https://youtu.be/dQw4w9WgXcQ'}):
            self.manager._process_single_item_safe(901,0,*options,threading.Semaphore(0),'shared-test')
        self.assertEqual(self.row()['status'],'Completed')
        self.assertTrue(self.source.is_file())
        self.assertEqual(self.row()['input_video_path'],str(self.source))
        self.assertTrue(Path(self.row(902)['input_video_path']).is_file())

    def test_batch_worker_frees_prepare_slot_before_waiting_for_upload_slot(self):
        options = [False,False,True]
        token = self.manager.preview_distribution([901], options)['preview_token']
        self.manager._freeze_batch_assignments([901],token,options,'capacity-test')
        prepare, ready = threading.Semaphore(0), threading.Semaphore(1)
        capacity = pm._WorkerCapacity(prepare, ready, threading.Event())
        seen = {}
        real_reserve = pm.reserve_upload_slot
        def reserve(stop_event, queue_id=None):
            seen['prepare_free'] = prepare.acquire(timeout=0)
            if seen['prepare_free']:
                prepare.release()
            seen['ready_held'] = not ready.acquire(timeout=0)
            return real_reserve(stop_event, queue_id)
        with mock.patch.object(pm,'upload_video_to_youtube', return_value={'url':'https://youtu.be/dQw4w9WgXcQ'}),                 mock.patch.object(pm,'reserve_upload_slot', side_effect=reserve):
            self.manager._process_single_item_safe(901,0,*options,capacity,'capacity-test')
        self.assertEqual(self.row()['status'],'Completed')
        self.assertEqual(seen, {'prepare_free': True, 'ready_held': True})
        self.assertTrue(ready.acquire(timeout=0))

    def test_repair_is_readonly_by_default_and_idempotent(self):
        from repair_review_state import repair
        self.reserve_review('Rendered')
        result = repair(database.APP_DB_PATH)
        self.assertFalse(result['changes'][0]['applied'])
        self.assertEqual(self.row()['status'], 'Rendered')
        result = repair(database.APP_DB_PATH, apply=True)
        self.assertTrue(result['changes'][0]['applied'])
        self.assertTrue(Path(result['backup']).exists())
        self.assertEqual(repair(database.APP_DB_PATH, apply=True)['changes'], [])
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT state FROM content_reservations').fetchone()[0], 'NeedsReview')


    def test_bulk_groups_persist_canonical_json(self):
        with closing(database.get_conn()) as conn, conn:
            for group in ('New-A','New-B'):
                conn.execute('INSERT INTO channel_groups(name) VALUES (?)',(group,))
        response = self.client.post('/api/queue/bulk-update',headers=self.headers,
            json={'ids':[901],'updates':{'target_group_names':['New-A','New-B']}})
        self.assertEqual(response.status_code,200,response.text)
        from group_distribution import target_group_names
        self.assertEqual(target_group_names(self.row()),['New-A','New-B'])
        self.assertIsNone(self.row()['target_channel_id'])

    def test_excel_export_nonempty_keeps_text(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET title='=1+1' WHERE id=901")
        response=self.client.get('/api/queue/export-excel?project=review',headers=self.headers)
        self.assertEqual(response.status_code,200,response.text if response.status_code!=200 else '')
        workbook=load_workbook(io.BytesIO(response.content))
        cells=[cell for row in workbook['Danh sách'] for cell in row if cell.value=='=1+1']
        self.assertEqual(len(cells),1)
        self.assertEqual(cells[0].data_type,'s')
        workbook.close()

    def test_group_alias_conflicts_and_batch_rollback(self):
        with closing(database.get_conn()) as conn, conn:
            for name in ('Nhóm A', 'Nhóm B'):
                conn.execute('INSERT INTO channel_groups(name) VALUES(?)', (name,))
        before = self.row()
        invalid = [
            {'target_group_names':['Nhóm A'], 'target_group_names_json':'["Nhóm B"]'},
            {'target_group_names':['Nhóm A'], 'target_channel_id':901},
            {'target_group_names':['Missing']},
        ]
        for updates in invalid:
            response = self.client.post('/api/queue/bulk-update',headers=self.headers,json={'ids':[901,902],'updates':updates})
            self.assertEqual(response.status_code,400,response.text)
            self.assertEqual(self.row(),before)
        response = self.client.post('/api/queue/bulk-update',headers=self.headers,
                                   json={'ids':[901,99999],'updates':{'description':'Must rollback'}})
        self.assertEqual(response.status_code,404,response.text)
        self.assertEqual(self.row(),before)

    def test_group_workbook_preview_apply_roundtrip(self):
        import json
        from datetime import datetime, timedelta
        with closing(database.get_conn()) as conn, conn:
            for name, channel in [('Nhóm A',901),('Nhóm B',902)]:
                conn.execute('INSERT INTO channel_groups(name) VALUES(?)',(name,))
                conn.execute('INSERT INTO channel_group_memberships(group_name,channel_id) VALUES(?,?)',(name,channel))
        updates = {'target_group_names':['Nhóm A','Nhóm B','Nhóm A'], 'title':'=Tiêu đề Việt',
                   'description':'Nội dung & Unicode', 'visibility':'schedule',
                   'schedule_time':(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),
                   'made_for_kids':1,'altered_content':1}
        response=self.client.post('/api/queue/bulk-update',headers=self.headers,json={'ids':[901],'updates':updates})
        self.assertEqual(response.status_code,200,response.text)
        before=self.row()
        self.assertEqual(json.loads(before['target_group_names_json']),['Nhóm A','Nhóm B'])
        preview=self.manager.preview_distribution([901],[False,False,True])
        self.assertFalse(preview['items'][0]['blocked'],preview)
        self.assertIn(preview['items'][0]['channel_id'],[901,902])
        response=self.client.get('/api/queue/export-excel?project=review&ids=901,902',headers=self.headers)
        self.assertEqual(response.status_code,200)
        workbook=load_workbook(io.BytesIO(response.content))
        self.assertEqual(workbook['Danh sách'].max_row,2)
        self.assertTrue(any(cell.value=='Nhóm A | Nhóm B' for cell in workbook['Danh sách'][2]))
        workbook.close()
        for preview_only in ('true','false'):
            imported=self.client.post('/api/queue/import-excel',headers=self.headers,
                data={'project_name':'review','ids':'901','preview_only':preview_only},
                files={'file':('roundtrip.xlsx',response.content,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
            self.assertEqual(imported.status_code,200,imported.text)
        after=self.row()
        for key in ('title','description','target_group_names_json','schedule_time','visibility','made_for_kids','altered_content'):
            self.assertEqual(after[key],before[key],key)


if __name__ == '__main__':
    unittest.main()
