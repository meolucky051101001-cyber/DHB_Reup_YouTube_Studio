"""One bounded metadata extraction; the parent owns timeout and process-tree cleanup."""
import json
import sys
from pathlib import Path

import yt_dlp

MAX_SCAN_ITEMS = 1000


def extract_entries(url, limit):
    options = {
        'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist',
        'playlistend': limit, 'ignoreerrors': False, 'skip_download': True,
        'socket_timeout': 15, 'retries': 1, 'extractor_retries': 1,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise RuntimeError('No metadata returned')
        entries = info.get('entries') if 'entries' in info else [info]
        result = []
        # Lazy playlist iteration also stays inside the killable worker.
        from itertools import islice
        for entry in islice(entries or [], limit):
            if not isinstance(entry, dict):
                continue
            item = {key: entry.get(key) for key in ('view_count','like_count','comment_count','duration')}
            for key, size in (('id',512),('webpage_url',8192),('url',8192),('title',1000),('description',5000)):
                value = str(entry.get(key) or '')
                if key in ('id', 'webpage_url', 'url') and len(value) > size:
                    raise ValueError('Source identifier exceeds size limit')
                item[key] = value[:size]
            tags = entry.get('tags')
            item['tags'] = [str(tag)[:100] for tag in tags[:15]] if isinstance(tags,list) else []
            result.append(item)
        return result


def main():
    input_path = Path(sys.argv[1])
    result_path = input_path.with_name('result.json')
    try:
        payload = json.loads(input_path.read_text(encoding='utf-8'))
        limit = int(payload['limit'])
        if not 1 <= limit <= MAX_SCAN_ITEMS:
            raise ValueError('Invalid scan limit')
        result = {'ok':True,'entries':extract_entries(str(payload['url']),limit)}
        code = 0
    except Exception as exc:
        # Extractor exceptions can contain signed URLs, cookies or response bodies.
        result = {'ok':False,'error_type':type(exc).__name__}
        code = 2
    result_path.write_text(json.dumps(result,ensure_ascii=True),encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
