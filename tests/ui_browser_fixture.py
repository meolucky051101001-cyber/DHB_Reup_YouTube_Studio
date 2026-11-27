"""Isolated full-page UI fixture. No runtime, provider or external connection."""
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8686'


class UIPage:
    def __init__(self, browser, *, viewport=None):
        self.context = browser.new_context(viewport=viewport or {'width': 1440, 'height': 900}, service_workers='block')
        self.page = self.context.new_page()
        self.requests = []
        self.external = []
        self.errors = []
        self.responses = {'/api/auth/token': {'token': 'offline-ui-token'}}
        self.handler = None
        self.context.route('**/*', self.dispatch)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(BASE + '/', wait_until='load')
        self.page.wait_for_function('window.StudioUI && document.body.classList.contains("studio-ui")')
        self.page.evaluate("""() => {
            document.getElementById('sel-project').innerHTML = '<option value="fixture">Dự án kiểm thử</option>';
            gChannels = [{id:1,name:'Kênh mẫu',channel_id:'UCaaaaaaaaaaaaaaaaaaaaaa',profile_name:'Profile mẫu',group_name:'Mặc định'}];
            gQueueItems = []; gSelectedIds.clear();
        }""")

    def dispatch(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if f'{parsed.scheme}://{parsed.netloc}' != BASE:
            self.external.append(request.url)
            route.abort()
            return
        if parsed.path == '/':
            route.fulfill(content_type='text/html', body=(ROOT / 'static/index.html').read_text(encoding='utf-8'))
            return
        if parsed.path.startswith('/static/'):
            path = ROOT / parsed.path.lstrip('/')
            if not path.is_file() or not path.resolve().is_relative_to(ROOT / 'static'):
                route.fulfill(status=404, body='missing fixture asset')
                return
            content = path.read_text(encoding='utf-8')
            if path.name == 'app.js':
                content = content.replace("window.addEventListener('DOMContentLoaded', () => queueMicrotask(initApp));", '')
            route.fulfill(content_type='text/css' if path.suffix == '.css' else 'text/javascript', body=content)
            return
        self.requests.append({'method': request.method, 'path': parsed.path, 'query': parsed.query, 'body': request.post_data})
        if self.handler and self.handler(route, parsed):
            return
        response = self.responses.get(parsed.path, {})
        route.fulfill(status=200, content_type='application/json', body=json.dumps(response))

    def seed_queue(self, items):
        self.responses['/api/queue'] = items
        self.page.evaluate('items => {gQueueItems=items; gSelectedIds.clear(); StudioQueue.render(items);}', items)

    def open_more_actions(self):
        if self.page.locator('#queue-more-actions').is_hidden():
            self.page.locator('#queue-more-toggle').click()

    def close(self):
        self.context.close()
