"""Project mutations must preserve queue identity and publication evidence."""
import unittest
from contextlib import closing
from unittest import mock
from urllib.parse import quote

import database
import test_review_regressions as fixtures


class ProjectCrudTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp

    def request(self, method, name='', payload=None):
        return self.client.request(method, '/api/projects' + ('/' + quote(name, safe='') if name else ''),
                                   headers=self.headers, json=payload)

    def snapshot(self):
        with closing(database.get_conn()) as conn:
            return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')]
                    for t in ('projects', 'video_queue', 'upload_attempts', 'content_reservations',
                              'published_history', 'upload_approval_items', 'managed_artifacts')}

    def test_invalid_duplicate_and_missing_requests_preserve_data(self):
        for value in (None, '', ' ', [], {}, 1, True, 'x'*201, 'bad\0name', 'Default'):
            with self.subTest(value=value):
                before = self.snapshot()
                self.assertEqual(self.request('POST', payload={'name':value}).status_code, 400)
                self.assertEqual(self.snapshot(), before)
        before = self.snapshot()
        self.assertEqual(self.request('POST', payload={'name':'review'}).status_code, 409)
        self.assertEqual(self.request('DELETE', 'absent').status_code, 404)
        self.assertEqual(self.request('PUT', 'absent', {'name':'new'}).status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_delete_nonempty_project_never_cascades(self):
        for status in ('Pending', 'Completed', 'Error', 'NeedsReview', 'Uploading'):
            with self.subTest(status=status):
                with closing(database.get_conn()) as conn, conn:
                    conn.execute('UPDATE video_queue SET status=? WHERE id=901', (status,))
                before = self.snapshot()
                self.assertEqual(self.request('DELETE', 'review').status_code, 409)
                self.assertEqual(self.snapshot(), before)

    def test_rename_preserves_queue_ids_files_and_evidence(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO upload_attempts(queue_id,attempt_no,channel_id,outcome) VALUES(901,1,901,'Completed')")
        before = self.snapshot()
        name = 'Dự án Việt / phiên 2'
        response = self.request('PUT', 'review', {'name':name})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.snapshot()
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT project_name FROM video_queue WHERE id=901').fetchone()[0], name)
        self.assertEqual(before['upload_attempts'], after['upload_attempts'])
        self.assertEqual([r[0] for r in before['projects']], [r[0] for r in after['projects']])
        self.assertTrue(self.source.is_file())
        self.assertEqual(self.request('DELETE', name).status_code, 409)

    def test_rename_blocked_for_claim_reservation_and_colliding_queue(self):
        for field, value in [('claim_token','worker'), ('status','NeedsReview')]:
            with closing(database.get_conn()) as conn, conn:
                conn.execute(f'UPDATE video_queue SET {field}=? WHERE id=901', (value,))
            before = self.snapshot()
            self.assertEqual(self.request('PUT', 'review', {'name':'changed'}).status_code, 409)
            self.assertEqual(self.snapshot(), before)
            with closing(database.get_conn()) as conn, conn:
                conn.execute("UPDATE video_queue SET claim_token='',status='Pending' WHERE id=901")
        self.assertEqual(self.request('PUT', 'review', {'name':'other'}).status_code, 409)
        fixtures.ReviewRegressions.reserve_review(self, 'Rendered')
        before = self.snapshot()
        self.assertEqual(self.request('PUT', 'review', {'name':'changed'}).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_empty_delete_last_project_and_monitor_ownership(self):
        response = self.request('POST', payload={'name':'temporary'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.request('DELETE', 'temporary').status_code, 200)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO projects(name) VALUES('Monitor - fixture')")
            conn.execute("INSERT INTO monitors(name,source_urls) VALUES('fixture','https://example.test')")
        for method in ('PUT','DELETE'):
            self.assertEqual(self.request(method, 'Monitor - fixture', {'name':'manual'}).status_code, 409)
        with closing(database.get_conn()) as conn, conn:
            conn.execute('DELETE FROM video_queue')
            conn.execute("DELETE FROM projects WHERE name != 'review'")
        self.assertEqual(self.request('DELETE', 'review').status_code, 409)

    def test_failed_queue_update_rolls_back_project_rename(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("CREATE TRIGGER reject_rename BEFORE UPDATE OF project_name ON video_queue "
                         "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        before = self.snapshot()
        self.assertEqual(self.request('PUT', 'review', {'name':'changed'}).status_code, 500)
        self.assertEqual(self.snapshot(), before)
        with closing(database.get_conn()) as conn, conn:
            conn.execute('DROP TRIGGER reject_rename')
        self.assertEqual(self.request('PUT', 'review', {'name':'changed'}).status_code, 200)

    def test_local_import_follows_project_id_across_rename_and_rejects_deleted_id(self):
        for delete in (False, True):
            with self.subTest(delete=delete):
                self.assertEqual(self.request('POST', payload={'name':'importing'}).status_code, 200)
                def probe(paths):
                    response = self.request('DELETE' if delete else 'PUT', 'importing', {'name':'renamed'})
                    self.assertEqual(response.status_code, 200, response.text)
                    return {str(self.source): {'seconds':30,'height':1920,'width':1080,'duration':'00:00:30'}}
                with mock.patch.object(self.app, 'probe_video_infos', side_effect=probe):
                    response = self.client.post('/api/add-local-paths', headers=self.headers,
                        json={'project_name':'importing','paths':[str(self.source)]})
                self.assertEqual(response.status_code, 409 if delete else 200, response.text)
                with closing(database.get_conn()) as conn:
                    self.assertIsNone(conn.execute("SELECT 1 FROM projects WHERE name='importing'").fetchone())
                    self.assertIsNone(conn.execute("SELECT 1 FROM video_queue WHERE project_name='importing'").fetchone())
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM video_queue WHERE project_name='renamed'").fetchone()[0], 1)
