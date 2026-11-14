"""Offline scanner lifecycle tests, including a genuinely blocked child process."""
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import database
import scanner_downloader as scanner
import scan_worker
import test_review_regressions as fixtures


class ScanWorkerTests(unittest.TestCase):
    def test_lazy_playlist_is_bounded_and_keeps_metadata(self):
        def entries():
            yield {'id':'one','title':'Tiêu đề Việt','duration':12,'view_count':5,'tags':['tag'],'webpage_url':'https://youtu.be/dQw4w9WgXcQ'}
            raise AssertionError('Read past requested limit')
        fake=mock.MagicMock()
        fake.__enter__.return_value.extract_info.return_value={'entries':entries()}
        with mock.patch.object(scan_worker.yt_dlp,'YoutubeDL',return_value=fake) as constructor:
            result=scan_worker.extract_entries('https://www.youtube.com/@fixture',1)
        self.assertEqual(result[0]['title'],'Tiêu đề Việt')
        self.assertEqual(result[0]['tags'],['tag'])
        fake.__enter__.return_value.extract_info.assert_called_once_with('https://www.youtube.com/@fixture',download=False)
        self.assertEqual(constructor.call_args.args[0]['socket_timeout'],15)

    def test_real_blocked_worker_and_descendant_are_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            heartbeat=root/'heartbeat'
            ready=root/'ready'
            child_code=f"import time; from pathlib import Path; p=Path({str(heartbeat)!r})\nwhile True:\n p.write_text(str(time.time())); time.sleep(.05)\n"
            worker=root/'fixture_worker.py'
            worker.write_text(
                'import subprocess,sys,time\nfrom pathlib import Path\n'
                f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}])\n'
                f'while not Path({str(heartbeat)!r}).exists(): time.sleep(.01)\n'
                f'Path({str(ready)!r}).write_text("ready")\n'
                'time.sleep(60)\n',encoding='utf-8')
            real_popen=subprocess.Popen
            processes=[]
            def spawn(args,**kwargs):
                if len(args)>1 and str(args[1]).endswith('scan_worker.py'):
                    args=[args[0],str(worker),*args[2:]]
                    proc=real_popen(args,**kwargs)
                    processes.append(proc)
                    return proc
                return real_popen(args,**kwargs)
            stop=threading.Event()
            def cancel_when_ready():
                deadline=time.monotonic()+4
                while not ready.exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                stop.set()
            cancel=threading.Thread(target=cancel_when_ready)
            cancel.start()
            started=time.monotonic()
            with mock.patch.object(scanner,'APP_TEMP_DIR',root),mock.patch.object(scanner.subprocess,'Popen',side_effect=spawn):
                with self.assertRaises(InterruptedError):
                    scanner._scan_remote_source('https://example.test/blocked?token=fixture',1,stop)
            cancel.join(timeout=5)
            self.assertLess(time.monotonic()-started,8)
            self.assertTrue(ready.exists(),'The worker must be blocked before cancellation')
            self.assertIsNotNone(processes[0].poll())
            stamp=heartbeat.read_text()
            time.sleep(.25)
            self.assertEqual(heartbeat.read_text(),stamp,'Worker descendant is still running')
            self.assertEqual(list(root.glob('scan-*')),[])

    def test_deadline_crash_and_success_protocol(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            worker=root/'fixture_worker.py'
            real_popen=subprocess.Popen
            def spawn(args,**kwargs):
                if len(args)>1 and str(args[1]).endswith('scan_worker.py'):
                    args=[args[0],str(worker),*args[2:]]
                return real_popen(args,**kwargs)
            cases=[('import time; time.sleep(60)',TimeoutError),
                   ('raise SystemExit(2)',scanner.ScanSourceError),
                   ('import sys; from pathlib import Path; Path(sys.argv[1]).with_name("result.json").write_text("not json")',scanner.ScanSourceError)]
            with mock.patch.object(scanner,'APP_TEMP_DIR',root),mock.patch.object(scanner.subprocess,'Popen',side_effect=spawn),mock.patch.object(scanner,'SCAN_TIMEOUT_SEC',.5):
                for code,exception in cases:
                    with self.subTest(exception=exception.__name__):
                        worker.write_text(code,encoding='utf-8')
                        with self.assertRaises(exception):
                            scanner._scan_remote_source('https://example.test',1)
                        self.assertEqual(list(root.glob('scan-*')),[])
                worker.write_text('import sys,json; from pathlib import Path; Path(sys.argv[1]).with_name("result.json").write_text(json.dumps({"ok":True,"entries":[{"id":"result"}]}))',encoding='utf-8')
                self.assertEqual(scanner._scan_remote_source('https://example.test',1),[{'id':'result'}])


class ScanApiTests(unittest.TestCase):
    setUp=fixtures.ReviewRegressions.setUp

    def test_later_source_failure_does_not_insert_partial_results(self):
        first=[{'id':'first','title':'Video','duration':4,'webpage_url':'https://youtu.be/dQw4w9WgXcQ'}]
        payload={'project_name':'review','urls':'https://www.youtube.com/@one\nhttps://www.youtube.com/@two','target_channel_id':901,'max_items':2}
        for error,status in [(TimeoutError('Quét quá hạn'),504),(scanner.ScanSourceError('Không lấy được nguồn'),502)]:
            with self.subTest(status=status),mock.patch.object(scanner,'_scan_remote_source',side_effect=[first,error]),mock.patch.object(scanner,'add_log'):
                response=self.client.post('/api/queue/scan',headers=self.headers,json=payload)
                self.assertEqual(response.status_code,status,response.text)
                with closing(database.get_conn()) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM video_queue').fetchone()[0],2)

    def test_filters_and_url_normalization_are_preserved(self):
        entries=[{'id':'short','url':'short','title':'Clip','duration':30,'view_count':100,'like_count':20,'comment_count':None,'tags':['Việt']},
                 {'id':'long','duration':200,'view_count':100,'like_count':20}]
        with mock.patch.object(scanner,'_scan_remote_source',return_value=entries):
            result=scanner.scan_social_urls(['https://www.youtube.com/@fixture'],min_views=50,min_likes=10,min_comments=5,only_shorts=True)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['source_url'],'https://www.youtube.com/watch?v=short')
        self.assertEqual(result[0]['tags'],'Việt')

    def test_invalid_limit_is_rejected_before_extraction(self):
        with mock.patch.object(scanner,'_scan_remote_source') as scan:
            for limit in (0,-1,1001,'bad',1.5,True,None):
                response=self.client.post('/api/queue/scan',headers=self.headers,json={'urls':'https://youtu.be/dQw4w9WgXcQ','target_channel_id':901,'max_items':limit})
                self.assertEqual(response.status_code,400,response.text)
            scan.assert_not_called()
