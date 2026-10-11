"""One-shot build helper for slow APT HTTP/1.1 downloads on an Aliyun host.

Run on the Docker server: python3 build_aliyun.py < Dockerfile
Only the Dockerfile is read from stdin. A loopback-only temporary relay fetches
Debian repository files over verified HTTPS / HTTP2 using curl. APT retains its
normal repository signature and package hash checks. No persistent service is
installed. Requires Python 3.6+, curl with HTTP2 support, and Docker.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlsplit


class RelayServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        decoded = unquote(path)
        if (not path.startswith(("/debian/", "/debian-security/"))
                or ".." in decoded.split("/") or "\\" in decoded):
            self.send_error(404)
            return
        with tempfile.TemporaryDirectory(prefix="taffy-apt-") as temporary:
            download = os.path.join(temporary, "download")
            result = subprocess.run([
                "curl", "--http2", "--fail", "--silent", "--show-error",
                "--location", "--retry", "2", "--connect-timeout", "10",
                "--max-time", "120", "--output", download,
                "https://mirrors.aliyun.com" + path,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if result.returncode:
                self.send_error(502, "Upstream Debian download failed")
                return
            self.send_response(200)
            self.send_header("Content-Length", str(os.path.getsize(download)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                with open(download, "rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=65536)
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    dockerfile = sys.stdin.buffer.read()
    if not dockerfile:
        raise SystemExit("Provide the project's Dockerfile on stdin")
    server = RelayServer(("127.0.0.1", 0), RelayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    print("Temporary Debian HTTP2 relay listening on loopback port {}".format(port), flush=True)
    try:
        result = subprocess.run([
            "docker", "build", "--network=host",
            "--build-arg", "DEBIAN_MIRROR=127.0.0.1:{}".format(port),
            "--build-arg", "DEBIAN_MIRROR_SCHEME=http",
            "--tag", "taffy-sandbox:1", "-",
        ], input=dockerfile)
        return result.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
