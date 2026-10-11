"""A local HTTP server standing in for Ashby and the model endpoint in tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    """Serves `routes[path]` = (status, body). A callable body gets the parsed JSON request and returns
    (status, body). A bytes body is sent truncated: its Content-Length promises more than arrives.
    Every request is recorded in `requests` as (method, path, json_body)."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._answer(None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self._answer(json.loads(self.rfile.read(length) or b"null"))

            def _answer(self, payload):
                server.requests.append((self.command, self.path, payload))
                status, body = server.routes.get(self.path, (404, {"error": "not found"}))
                if callable(body):
                    status, body = body(payload)
                truncated = isinstance(body, bytes)
                raw = body if truncated else body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                for name, value in getattr(server, "extra_headers", {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw) + (100 if truncated else 0)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ashby_job(job_id, title, location="London", description=None, **extra):
    return {"id": job_id, "title": title, "location": location, "isListed": True,
            "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}",
            "descriptionPlain": description if description is not None else f"{title} role. We sell software.",
            **extra}


def model_reply(tags: dict, cost: float = 0.0005):
    """An OpenRouter-shaped chat-completions response carrying `tags` as the message content."""
    return 200, {"choices": [{"message": {"content": json.dumps(tags)}}], "usage": {"cost": cost}}


def tags_quoting_the_posting(payload):
    """A well-behaved model: founding yes quoting the title, location_eligible yes quoting the location."""
    text = payload["messages"][0]["content"]
    title = text.split("<title>", 1)[1].split("</title>", 1)[0]
    location = text.split("<location>", 1)[1].split("</location>", 1)[0]
    return {"founding": {"value": "yes", "quote": title},
            "location_eligible": {"value": "yes", "quote": location},
            "segment": {"value": [], "quote": None},
            "language_required": {"value": "no", "quote": None},
            "tech_company": {"value": "unknown", "quote": None}}
