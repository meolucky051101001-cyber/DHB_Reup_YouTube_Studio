"""Opt-in real FFmpeg release matrix; no network and no runtime DB writes."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import render_engine as render


@unittest.skipUnless(os.environ.get('DHB_MEDIA_TESTS') == '1', 'Run in the media CI job')
class RealMediaMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='dhb_media_review_')
        cls.root = Path(cls.temp.name)
        cls.results = []
        cls.patches = [mock.patch.object(render,'APP_OUTPUT_DIR',cls.root),
                       mock.patch.object(render,'DHB_RENDER_DIR',cls.root),
                       mock.patch('database.register_managed_artifact')]
        for patch in cls.patches:
            patch.start()
        for folder in ('_Backgroud Music','_Intro','_Outro'):
            (cls.root/folder).mkdir()
        for audio in (False,True):
            cls.ffmpeg(['-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=4'] +
                       (['-f','lavfi','-i','sine=frequency=440:duration=4'] if audio else []) +
                       ['-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p'] +
                       (['-c:a','aac'] if audio else []) + [str(cls.root/('audio.mp4' if audio else 'silent.mp4'))])
        cls.ffmpeg(['-f','lavfi','-i','sine=frequency=880:duration=1',str(cls.root/'_Backgroud Music/tone.wav')])
        cls.ffmpeg(['-f','lavfi','-i','testsrc2=size=180x320:rate=30:duration=1','-c:v','libx264','-preset','ultrafast',str(cls.root/'_Intro/intro.mp4')])
        cls.ffmpeg(['-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=1','-c:v','libx264','-preset','ultrafast',str(cls.root/'_Outro/outro.mp4')])

    @classmethod
    def tearDownClass(cls):
        report=os.environ.get('DHB_MEDIA_REPORT')
        if report:
            Path(report).write_text(json.dumps(cls.results,indent=2),encoding='utf-8')
        for patch in reversed(cls.patches):
            patch.stop()
        cls.temp.cleanup()

    @staticmethod
    def ffmpeg(args):
        return subprocess.run([render.FFMPEG_EXE,'-hide_banner','-loglevel','error','-y',*args],
                              capture_output=True,check=True,timeout=120).stdout

    def run_case(self,name,mode,speed,audio=False,music=False,expected=None,extra=None,source=None,cut_start='00:00:00',cut_end='00:00:00'):
        cfg={'speed':speed,'resolution':'original','preset':'ultrafast','framerate':30,'bitrate':'800k',
             'shorts_visual_pop':False,'enforce_shorts_duration':False}
        if music:
            cfg['bg_music_name']='tone.wav'
        cfg.update(extra or {})
        source=source or self.root/('audio.mp4' if audio else 'silent.mp4')
        output=render.render_video(str(source),mode,cfg,cut_start=cut_start,cut_end=cut_end)
        probe=render.probe_video_info(output)
        expected=4/speed if expected is None else expected
        tolerance=max(.15,min(1.5,expected*.04))
        self.assertAlmostEqual(probe['seconds'],expected,delta=tolerance)
        self.assertTrue(probe['has_audio'])
        if mode=='square_1_1':
            self.assertEqual((probe['width'],probe['height']),(1080,1080))
        if expected>=1 and not extra:
            frames=[]
            for t in (expected-.65,expected-.2):
                frames.append(self.ffmpeg(['-ss',str(t),'-i',output,'-frames:v','1','-vf','crop=iw/2:ih/2,scale=32:32','-pix_fmt','rgb24','-f','rawvideo','-']))
            self.assertEqual(len(frames[0]),3072)
            self.assertGreater(sum(abs(a-b) for a,b in zip(*frames))/len(frames[0]),.5,'Foreground froze before the end')
        if music:
            pcm=self.ffmpeg(['-i',output,'-t','0.25','-vn','-f','s16le','-'])
            import array
            samples=array.array('h',pcm)
            self.assertGreater(max(abs(v) for v in samples),100,'Selected music is missing')
        self.results.append({'name':name,'duration':probe['seconds'],'expected':expected,'width':probe['width'],'height':probe['height'],'audio':probe['has_audio']})
        Path(output).unlink()

    def test_core_18_cases(self):
        for mode in ('shorts_9_16','square_1_1'):
            for speed in (.5,1,2):
                for audio,music in ((True,False),(False,False),(False,True)):
                    name=f'{mode}-{speed}-audio{audio}-music{music}'
                    with self.subTest(name=name):
                        self.run_case(name,mode,speed,audio,music)

    def test_full_frame_controls_3_cases(self):
        for speed in (.5,1,2):
            with self.subTest(speed=speed):
                self.run_case(f'full-{speed}','full_16_9',speed,True)

    def test_edge_cut(self):
        self.run_case('cut','square_1_1',2,expected=1,cut_start='00:00:01',cut_end='00:00:01')

    def test_edge_short_audio(self):
        source=self.root/'short-audio.mp4'
        self.ffmpeg(['-i',str(self.root/'silent.mp4'),'-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','copy','-c:a','aac',str(source)])
        self.run_case('short-audio','shorts_9_16',.5,True,source=source)

    def test_edge_intro_outro(self):
        cases = (
            ('intro-outro','shorts_9_16',2,False,False,4,{'intro_enabled':True,'intro_name':'intro.mp4','outro_enabled':True,'outro_name':'outro.mp4'}),
            ('outro-music','shorts_9_16',2,False,True,3,{'outro_enabled':True,'outro_name':'outro.mp4'}),
            ('intro-full-audio','full_16_9',1,True,False,5,{'intro_enabled':True,'intro_name':'intro.mp4'}),
        )
        for name,mode,speed,audio,music,expected,extra in cases:
            with self.subTest(name=name),                     mock.patch.object(render,'_run_ffmpeg_with_progress',wraps=render._run_ffmpeg_with_progress) as run:
                self.run_case(name,mode,speed,audio,music,expected=expected,extra=extra)
                self.assertEqual(run.call_count,1,'intro/outro must be encoded in the main pass')

    def test_edge_autofit(self):
        self.run_case('autofit','shorts_9_16',1,expected=4/1.34,
                      extra={'enforce_shorts_duration':True,'max_shorts_duration':3,'shorts_fit_mode':'speedup'})

    def test_edge_stop_mid_encode(self):
        stop=threading.Event()
        before=set(self.root.glob('yt_*.mp4'))
        timer=threading.Timer(.3,stop.set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                render.render_video(str(self.root/'silent.mp4'),'square_1_1',{'speed':.5,'preset':'slow'},stop_event=stop,owner='review-media')
        finally:
            timer.cancel()
        self.assertEqual(set(self.root.glob('yt_*.mp4')),before)
        self.assertFalse(render._ACTIVE_FFMPEG_PROCS)
        self.results.append({'name':'stop-mid-encode','passed':True})

    def test_edge_invalid_input(self):
        bad=self.root/'bad.mp4'
        bad.write_bytes(b'not a video')
        with self.assertRaises(Exception):
            render.render_video(str(bad),'shorts_9_16')
        self.results.append({'name':'invalid-input','passed':True})

    def test_representative_1080p(self):
        self.run_case('1080p','shorts_9_16',2,extra={'resolution':'1080p'})
