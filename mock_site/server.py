"""Local mock ATS pages for tests. Serves static html; POST /submit* returns a confirmation page."""
import http.server, threading, pathlib, urllib.parse

ROOT = pathlib.Path(__file__).parent
SUBMITS = []


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "ignore")
        SUBMITS.append((self.path, body))
        if self.path.startswith("/submit_silent"):
            html = "<html><body><p>Processing.</p></body></html>"
        elif self.path.startswith("/submit_invalid"):
            html = open(ROOT / "invalid.html").read()
        else:
            html = "<html><body><h1>Thank you for applying!</h1><p>Your application has been received.</p></body></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(html.encode())


def start(port=0):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    s, u = start(8765); print(u); threading.Event().wait()
