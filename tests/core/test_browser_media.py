"""Local browser fixture for player state that is absent from page HTML."""
from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fetcher_core.download import _resolve_pornhub_browser_urls
from fetcher_core.models import Video


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
