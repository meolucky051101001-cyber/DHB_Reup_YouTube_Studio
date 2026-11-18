"""Preset CRUD, validation and snapshot isolation from queued videos."""
import json
import unittest
from contextlib import closing

import database
import test_review_regressions as fixtures


class RenderPresetCrudTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp

    def request(self, method, suffix='', payload=None):
        return self.client.request(method, '/api/render-presets'+suffix, headers=self.headers, json=payload)

    def snapshot(self):
        with closing(database.get_conn()) as conn:
            return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')]
                    for t in ('render_presets','video_queue','monitors')}

    def create(self, **values):
        response = self.request('POST',payload={'name':'Preset Việt', 'config':{'speed':1.1}, **values})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_invalid_types_modes_and_config_never_write(self):
        cases = [{'name':[]},{'name':True},{'name':' '},{'name':'x'*201},{'note':{}},
                 {'render_mode':'invalid'},{'render_mode':[]},{'config':None},{'config':[]},
                 {'config':False},{'config':0},{'config':{'speed':True}}, {'config':{'speed':'NaN'}},
                 {'config':{'speed':3}},{'config':{'volume':{}}},{'config':{'brightness':2}},
                 {'config':{'hflip':[]}},{'config':{'crop_size':'bad'}},{'config':{'rotation_angle':45}},
                 {'config':{'framerate':29.5}},{'config':{'bg_music_name':'../music.mp3'}},
                 {'config':{'watermark_image_opacity':'Infinity'}},{'config':{'pad_color':'bad;filter'}},
                 {'config':{'unexpected':{'nested':'value'}}},{'render_mode':'custom_ffmpeg','config':{}}]
        for values in cases:
            with self.subTest(values=values):
                before = self.snapshot()
                response = self.request('POST',payload={'name':'new',**values})
                self.assertEqual(response.status_code,400,response.text)
                self.assertEqual(self.snapshot(),before)

    def test_create_update_rename_and_duplicate_are_explicit(self):
        preset = self.create(note='kept',config={'hflip':'false','crop_center':'0','speed':'1.25','framerate':60})
        self.assertIs(preset['config']['hflip'], False)
        self.assertIs(preset['config']['crop_center'], False)
        before = self.snapshot()
        self.assertEqual(self.request('POST',payload={'name':'Preset Việt'}).status_code,409)
        self.assertEqual(self.snapshot(),before)
        response = self.request('PUT',f"/{preset['id']}",{'name':'Đổi tên','expected_revision':preset['revision']})
        self.assertEqual(response.status_code,200,response.text)
        updated = response.json()
        self.assertEqual(updated['id'],preset['id'])
        self.assertEqual(updated['config'],preset['config'])
        self.assertEqual(updated['note'],'kept')
        self.assertNotEqual(updated['revision'],preset['revision'])
        self.assertEqual(self.request('PUT','/999999',{'name':'x'}).status_code,404)
        self.assertEqual(self.request('DELETE','/999999').status_code,404)

    def test_stale_update_delete_and_failed_validation_preserve_latest_version(self):
        preset = self.create()
        current = self.request('PUT',f"/{preset['id']}",{'note':'new','expected_revision':preset['revision']}).json()
        before = self.snapshot()
        for method in ('PUT','DELETE'):
            suffix = f"/{preset['id']}"
            if method == 'DELETE':
                suffix += '?expected_revision='+preset['revision']
            self.assertEqual(self.request(method,suffix,{'note':'stale','expected_revision':preset['revision']}).status_code,409)
            self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.request('PUT',f"/{preset['id']}",{'config':{'speed':5},'expected_revision':current['revision']}).status_code,400)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.request('DELETE',f"/{preset['id']}?expected_revision={current['revision']}").status_code,200)

    def test_edit_delete_library_preset_does_not_mutate_queue_or_monitor_copies(self):
        preset = self.create()
        cfg = json.dumps(preset['config'])
        with closing(database.get_conn()) as conn, conn:
            conn.execute('UPDATE video_queue SET render_config_json=? WHERE id=901',(cfg,))
            conn.execute("INSERT INTO monitors(name,source_urls,render_config_json) VALUES('copy','https://example.test',?)",(cfg,))
        before = self.snapshot()
        response = self.request('PUT',f"/{preset['id']}",{'config':{'speed':1.5},'expected_revision':preset['revision']})
        self.assertEqual(response.status_code,200,response.text)
        updated = response.json()
        self.assertEqual(self.request('DELETE',f"/{preset['id']}?expected_revision={updated['revision']}").status_code,200)
        after = self.snapshot()
        self.assertEqual(before['video_queue'],after['video_queue'])
        self.assertEqual(before['monitors'],after['monitors'])

    def test_corrupt_legacy_presets_remain_visible_without_replacing_raw_data(self):
        for raw in ('broken','[]','{"speed":NaN}','{"speed":4}'):
            with closing(database.get_conn()) as conn, conn:
                conn.execute('INSERT INTO render_presets(name,config_json) VALUES(?,?)',(raw,raw))
        before = self.snapshot()
        response = self.request('GET')
        self.assertEqual(response.status_code,200,response.text)
        rows = response.json()
        self.assertEqual(len(rows),4)
        for row in rows:
            self.assertFalse(row['valid'])
            self.assertIsNone(row['config'])
            self.assertEqual(row['config_json'],row['name'])
            self.assertTrue(row['validation_error'])
        self.assertEqual(self.snapshot(),before)

    def test_apply_uses_same_config_validation_before_bulk_writes(self):
        before = self.snapshot()
        response = self.client.post('/api/queue/bulk-update',headers=self.headers,
            json={'ids':[901,902],'updates':{'render_config_json':'{"volume":"NaN"}'}})
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.snapshot(),before)
        response = self.client.post('/api/queue/bulk-update',headers=self.headers,
            json={'ids':[901,902],'updates':{'render_config_json':'{"hflip":"false","speed":"1.2"}'}})
        self.assertEqual(response.status_code,200,response.text)
        with closing(database.get_conn()) as conn:
            values = json.loads(conn.execute('SELECT render_config_json FROM video_queue WHERE id=901').fetchone()[0])
        self.assertIs(values['hflip'],False)
        self.assertEqual(values['speed'],1.2)

    def test_concurrent_updates_only_one_revision_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        preset = self.create()
        def update(note):
            return self.request('PUT',f"/{preset['id']}",{'note':note,'expected_revision':preset['revision']})
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(update, ['one','two']))
        self.assertEqual(sorted(response.status_code for response in responses),[200,409])
        with closing(database.get_conn()) as conn:
            self.assertIn(conn.execute('SELECT note FROM render_presets WHERE id=?',(preset['id'],)).fetchone()[0],('one','two'))
