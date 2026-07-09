#!/usr/bin/env python3
"""
Tiny webhook listener for the shiftarr container.
Sonarr -> Settings -> Connect -> Webhook -> URL http://shiftarr:8000/  (On Import / On Upgrade).
On an import event it extracts + syncs the episode's embedded subtitle. No docker socket needed.
"""
import json, os, subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))

def sync(path):
    subprocess.run(["python", "/app/shiftarr.py", path])

def episode_path(data):
    ef = data.get("episodeFile") or {}
    if ef.get("path"):
        return ef["path"]
    series = (data.get("series") or {}).get("path")
    if series and ef.get("relativePath"):
        return os.path.join(series, ef["relativePath"])
    return None

class Handler(BaseHTTPRequestHandler):
    def _reply(self, code=200, msg=b"ok\n"):
        self.send_response(code); self.send_header("Content-Type","text/plain")
        self.end_headers(); self.wfile.write(msg)
    def do_GET(self):
        self._reply(200, b"shiftarr webhook up\n")
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        self._reply(200)                       # ack immediately; work in background
        try: data = json.loads(raw or b"{}")
        except Exception: data = {}
        et = data.get("eventType")
        if et in (None, "Test"):
            print("ping/test received", flush=True); return
        if et not in ("Download", "Rename", "Upgrade"):
            print(f"ignoring eventType={et}", flush=True); return
        path = episode_path(data)
        if not path:
            print(f"eventType={et} but no episode path in payload", flush=True); return
        print(f"eventType={et} -> sync {path}", flush=True)
        threading.Thread(target=sync, args=(path,), daemon=True).start()
    def log_message(self, *a): pass

if __name__ == "__main__":
    print(f"shiftarr webhook listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
