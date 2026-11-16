"""Boot a disposable clean install with fresh data and probe its local HTTP API."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from config import FFMPEG_EXE
from scanner_downloader import _terminate_download_process
from runtime_identity import capture_runtime, source_fingerprint
from audit_upload_capabilities import environment_manifest


def smoke(python):
    source=Path(__file__).resolve().parent
    source_identity=capture_runtime(source)
    expected_build=environment_manifest(source)['build']
    with tempfile.TemporaryDirectory(prefix='dhb_clean_start_') as folder:
        root=Path(folder)
        for path in source.glob('*.py'):
            shutil.copy2(path,root/path.name)
        shutil.copytree(source/'static',root/'static')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        env=os.environ.copy()
        env.pop('DHB_DISABLE_BACKGROUND_TASKS',None)
        env.pop('DHB_FFMPEG',None)
        env.pop('DHB_FFPROBE',None)
        env['DHB_ROOT']=str(root/'absent-legacy-tool')
        ffmpeg=shutil.which(FFMPEG_EXE) or FFMPEG_EXE
        env['PATH']=str(Path(ffmpeg).parent)+os.pathsep+env.get('PATH','')
        with (root/'server.log').open('w',encoding='utf-8') as log:
            proc=subprocess.Popen([str(python),'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],
                                  cwd=root,env=env,stdout=log,stderr=log,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                                  start_new_session=os.name!='nt')
            try:
                base=f'http://127.0.0.1:{port}'
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    if proc.poll() is not None:
                        raise RuntimeError('Clean server exited before health was ready.')
                    try:
                        response=requests.get(base+'/health/dhb',timeout=1)
                        if response.ok:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.2)
                else:
                    raise RuntimeError('Clean server startup timed out.')
                token=requests.get(base+'/api/auth/token',timeout=5).json()['token']
                headers={'X-DHB-Token':token,'Origin':base}
                health=response.json()
                diag=requests.get(base+'/api/diagnostics',headers=headers,timeout=30).json()
                overview=requests.get(base+'/api/overview',headers=headers,timeout=5).json()
                assert health['source_root']==str(root)
                assert not diag['runtime']['restart_required']
                assert diag['runtime']['revision'] is None  # deliberately copied without .git
                assert diag['version']==expected_build
                assert diag['runtime']['startup_fingerprint']==source_fingerprint(root)==source_identity['startup_fingerprint']
                assert overview['total_videos']==0 and not overview['pipeline_running'] and overview['monitors_running']==0
                assert all(tool['available'] for tool in diag['tools'][:2]),diag['tools'][:2]
                return {'startup':'ok','fresh_database':True,'auto_run':False,'correct_source_root':True,
                        'version':diag['version'],'fingerprint':diag['runtime']['startup_fingerprint'],
                        'source_revision':source_identity['revision'],'runtime_revision':None,
                        'ffmpeg_path_fallback':True,'schemas':diag['database']['schemas']}
            finally:
                # Windows venv python.exe may launch the actual server as a child.
                # Stop the entire owned tree before removing its DB and lock file.
                _terminate_download_process(proc)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    args.output.write_text(json.dumps(smoke(args.python.resolve()),indent=2),encoding='utf-8')
