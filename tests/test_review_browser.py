"""Real DOM regressions. CI's browser job sets DHB_BROWSER_TESTS=1."""
import os
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class ProfileDomTests(unittest.TestCase):
    def test_profile_handlers_treat_names_as_data_after_rerender(self):
        from playwright.sync_api import sync_playwright
        source = (Path(__file__).resolve().parents[1] / 'static/app.js').read_text(encoding='utf-8')
        names = ["&#39;);globalThis.reviewMarker=1;//", 'Việt & <tag> " \' \\ &#34;']
        actions = ['profileOpenBrowser', 'profileCheckLogin', 'profileRefreshChannels', 'profileEditProxy', 'profileDelete']
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<table><tbody id="profiles-tbody"></tbody></table>')
            page.add_script_tag(content=source)
            page.evaluate("""names => {
                globalThis.reviewMarker = 0; globalThis.calls = [];
                profileOpenBrowser = name => calls.push(['profileOpenBrowser', name]);
                profileCheckLogin = name => calls.push(['profileCheckLogin', name]);
                profileRefreshChannels = name => calls.push(['profileRefreshChannels', name]);
                profileEditProxy = name => calls.push(['profileEditProxy', name]);
                profileDelete = name => calls.push(['profileDelete', name]);
                gProfiles = names.map(name => ({ name, source: 'Local' }));
                renderProfilesTable(); renderProfilesTable(); renderProfilesTable();
            }""", names)
            for index, name in enumerate(names):
                self.assertEqual(page.locator('tbody tr').nth(index).locator('td b').first.inner_text(), name)
                for action in actions:
                    page.locator(f'[data-profile-index="{index}"][data-profile-action="{action}"]').click()
            self.assertEqual(page.evaluate('reviewMarker'), 0)
            self.assertEqual(page.evaluate('calls'), [[action,name] for name in names for action in actions])
            self.assertEqual(page.locator('tbody [onclick]').count(), 0)
            browser.close()
