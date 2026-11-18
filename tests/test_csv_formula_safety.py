"""Exercise downloadable CSV and preview/apply import through the real API."""
import csv
import io
import json
import unittest
from contextlib import closing
from pathlib import Path

import database
import test_review_regressions as fixtures


class CsvFormulaSafetyTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp

    def seed(self, title, description, tags):
        with closing(database.get_conn()) as conn, conn:
            conn.execute(
                "UPDATE video_queue SET source_url=?,title=?,description=?,tags=? WHERE id=901",
                ('https://youtu.be/dQw4w9WgXcQ', title, description, tags),
            )

    def export(self):
        response = self.client.get('/api/queue/export-csv?project=review', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.content.startswith(b'\xef\xbb\xbf'))
        text = response.content.decode('utf-8-sig')
        return text, list(csv.DictReader(io.StringIO(text, newline='')))

    def test_formula_prefixes_are_text_in_every_exported_string_column(self):
        payloads = ['=1+1', '+SUM(1,2)', '-1+2', '@SUM(1)', '\t=1+1', '\r=1+1', '\n=1+1',
                    '  =1+1', '\ufeff=1+1', '\tplain', "'literal", 'Tiếng Việt, "giữ nguyên"\nDòng 2']
        fields = ['platform', 'title', 'description', 'tags', 'duration', 'source_url',
                  'render_mode', 'upload_to', 'status', 'published_url', 'visibility', 'schedule_time']
        for payload in payloads:
            with self.subTest(payload=repr(payload)):
                with closing(database.get_conn()) as conn, conn:
                    conn.execute('UPDATE video_queue SET ' + ','.join(f'{field}=?' for field in fields) + ' WHERE id=901',
                                 [payload] * len(fields))
                _, rows = self.export()
                self.assertEqual(len(rows), 1)
                for field in fields:
                    value = next(value for key, value in rows[0].items() if key.lower() == field)
                    expected = payload if payload.startswith('Tiếng') else "'" + payload
                    self.assertEqual(value, expected)

    def test_export_preview_apply_round_trip_preserves_formula_and_literal_apostrophes(self):
        cases = [('=1+1', '@SUM(1,2)\nTiếng Việt, "trích dẫn"', '+tag'),
                 ("'Tiêu đề", "'=literal\nDòng 2", "''tag"),
                 ('Tiêu đề thường', 'Nhiều dòng\r\nDòng hai', 'tag, Việt')]
        for index, values in enumerate(cases):
            with self.subTest(index=index):
                self.seed(*values)
                text, _ = self.export()
                payload = {'project_name':f'csv-roundtrip-{index}', 'csv_text':text}
                preview = self.client.post('/api/queue/import-csv', headers=self.headers, json={**payload, 'preview':True})
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()['valid_count'], 1, preview.text)
                with closing(database.get_conn()) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM video_queue WHERE project_name=?', (payload['project_name'],)).fetchone()[0], 0)
                applied = self.client.post('/api/queue/import-csv', headers=self.headers, json=payload)
                self.assertEqual(applied.status_code, 200, applied.text)
                self.assertEqual(applied.json()['added'], 1, applied.text)
                with closing(database.get_conn()) as conn:
                    row = conn.execute('SELECT title,description,tags FROM video_queue WHERE project_name=?', (payload['project_name'],)).fetchone()
                    self.assertEqual(tuple(row), values)

    def test_unmarked_external_csv_keeps_literal_apostrophe(self):
        for marker in ('', ',__DHB_CSV_Encoding'):
            text = "Source_URL,Title,Description,Tags" + marker + "\nhttps://youtu.be/dQw4w9WgXcQ,'=literal,'description,''tags" + (',unknown-version' if marker else '') + '\n'
            response = self.client.post('/api/queue/import-csv', headers=self.headers,
                json={'project_name':f'external-{bool(marker)}', 'csv_text':text})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['added'], 1, response.text)
            with closing(database.get_conn()) as conn:
                row = conn.execute('SELECT title,description,tags FROM video_queue WHERE project_name=?', (f'external-{bool(marker)}',)).fetchone()
                self.assertEqual(tuple(row), ("'=literal", "'description", "''tags"))

    def test_logs_and_table_exports_escape_after_secret_redaction(self):
        secret = 'fixture-private-token-8675309'
        database.save_settings({'gemini_api_key':secret})
        self.addCleanup(database._invalidate_secret_cache)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO system_logs(module,status,channel_name,video_title,message) VALUES(?,?,?,?,?)",
                         ('CSVFixture', 'INFO', '+channel', '-title', '=1+1 ' + secret))
        response = self.client.get('/api/logs/export?module=CSVFixture', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(secret, response.text)
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'), newline='')))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['channel_name'], "'+channel")
        self.assertEqual(rows[0]['video_title'], "'-title")
        self.assertTrue(rows[0]['message'].startswith("'=1+1 "))
        self.assertEqual(rows[0]['__DHB_CSV_Encoding'], 'apostrophe-v1')

        self.seed('=1+1', "'description", 'tag')
        exported = Path(database.export_all_tables_csv(str(self.root/'export'))['path'])
        manifest = json.loads((exported/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['cell_encoding'], 'apostrophe-v1')
        with (exported/'video_queue.csv').open(encoding='utf-8-sig', newline='') as handle:
            row = next(row for row in csv.DictReader(handle) if row['id'] == '901')
        self.assertEqual(row['title'], "'=1+1")
        self.assertEqual(row['description'], "''description")
        self.assertNotIn(secret, (exported/'system_logs.csv').read_text(encoding='utf-8-sig'))
        with (exported/'app_settings.csv').open(encoding='utf-8-sig', newline='') as handle:
            row = next(row for row in csv.DictReader(handle) if row['key'] == 'gemini_api_key')
        self.assertEqual(row['value'], '')
