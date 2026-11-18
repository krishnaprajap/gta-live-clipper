"""One-time helper: obtain a YouTube refresh token (youtube.upload scope)
using the OAuth loopback flow. Run on a machine with a browser:

    YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... python scripts/youtube_auth.py

Create the OAuth client in Google Cloud Console as type "Desktop app".
Put the printed refresh token into .env as YOUTUBE_REFRESH_TOKEN. It is
printed once to your terminal only; it is never written to disk here."""
import http.server
import os
import secrets
import urllib.parse

import httpx

PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/"
cid, csec = os.environ["YOUTUBE_CLIENT_ID"], os.environ["YOUTUBE_CLIENT_SECRET"]
state = secrets.token_urlsafe(16)
url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "access_type": "offline",
    "prompt": "consent", "state": state, "scope": "https://www.googleapis.com/auth/youtube.upload"})
result = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] == state and "code" in q:
            result["code"] = q["code"][0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Done - you can close this tab.")

    def log_message(self, *a):
        pass


print("Open this URL and approve access:\n", url)
srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
while "code" not in result:
    srv.handle_request()
r = httpx.post("https://oauth2.googleapis.com/token", data={
    "code": result["code"], "client_id": cid, "client_secret": csec, "redirect_uri": REDIRECT,
    "grant_type": "authorization_code"})
r.raise_for_status()
print("\nYOUTUBE_REFRESH_TOKEN=" + r.json()["refresh_token"])
