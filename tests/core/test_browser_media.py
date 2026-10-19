"""Local browser fixture for player state that is absent from page HTML."""
from __future__ import annotations

import json
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fetcher_core.download import (
    _BROWSER_MEDIA_SCRIPT,
    _resolve_browser_media_urls,
    _resolve_pornhub_browser_urls,
)
from fetcher_core.models import Video


def test_legacy_pornhub_resolver_name_still_works(monkeypatch) -> None:
    sentinel = (["https://x.test/a.mp4"], None)
    monkeypatch.setattr("fetcher_core.download._resolve_browser_media_urls", lambda v, **kwargs: sentinel)
    assert _resolve_pornhub_browser_urls(object()) == sentinel


def test_browser_fallback_resolves_arbitrary_hosts_via_page_scan(monkeypatch):
    """The generic fallback must work for any site, not just known tube sites."""
    from selenium import webdriver

    page_html = (
        "<html><body><video controls>"
        "<source src='https://cdn.arbitrary.test/hls/master.m3u8' type='application/vnd.apple.mpegurl'>"
        "</video><script>var cfg = {url: 'https://cdn.arbitrary.test/720.mp4'};</script></body></html>"
    )

    class ScriptingBrowser:
        page_source = page_html
        closed = False
        def set_page_load_timeout(self, seconds): pass
        def set_script_timeout(self, seconds): pass
        def get(self, url): pass
        def execute_async_script(self, script):
            assert script is _BROWSER_MEDIA_SCRIPT
            # Emulate the bounded page scan the injected script performs.
            urls = set(re.findall(r"https?://[^\s'\"<>]+?\.(?:m3u8|mp4)", page_html))
            return [{"videoUrl": url} for url in sorted(urls)]
        def quit(self): self.closed = True

    browser = ScriptingBrowser()
    monkeypatch.setattr(webdriver, "Chrome", lambda **kw: browser)

    item = Video("arbitrary-1", "Arbitrary host", 90, 0,
                 "https://some-arbitrary-host.test/watch/arbitrary-1", "SomeArbitraryTube")
    urls, error = _resolve_browser_media_urls(item)

    assert error is None
    # .mp4 wins the quality tiebreak; the m3u8 from <source> is also collected.
    assert urls == ["https://cdn.arbitrary.test/720.mp4"]
    assert browser.closed


@pytest.mark.skipif(not shutil.which("google-chrome"), reason="Chrome is required for the runtime-player fixture")
def test_runtime_redtube_player_resolves_quality_api_instead_of_current_preview():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass

        def do_GET(self):
            if self.path == "/api":
                payload = json.dumps([
                    {"quality": "1080", "videoUrl": "https://cdn.test/full.mp4"},
                    {"quality": "240", "videoUrl": "https://cdn.test/preview.mp4"},
                ]).encode()
                content_type = "application/json"
            else:
                payload = b'''<html><script>
                    window.page_params = {generalVideoConfig:{mainRoll:{mediaDefinition:[
                        {format:'mp4',videoUrl:'/api',remote:true}
                    ]}}};
                </script><video src=""></video></html>'''
                content_type = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        item = Video("fixture", "Runtime player", 120, 0,
                     f"http://127.0.0.1:{server.server_port}/watch", "RedTube")
        urls, error = _resolve_pornhub_browser_urls(item)
        assert error is None
        assert urls == ["https://cdn.test/full.mp4"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
