#!/usr/bin/env python3
"""Threaded HTTP proxy with configurable Responses API body replacements.

HTTPS upstream certificate verification is disabled to match the reference proxy.
Send SIGHUP, or run this script with reload <PID>, to reload plugin configuration.
"""

import argparse
import http.client
import http.server
import os
import signal
import ssl
import sys
import urllib.parse
from pathlib import Path

from plugin.engine import ReplacementEngine


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def hop_by_hop_headers(headers):
    excluded = set(HOP_BY_HOP_HEADERS)
    # Connection may nominate additional headers that belong to this hop only.
    for key, value in headers:
        if key.lower() == "connection":
            excluded.update(token.strip().lower() for token in value.split(","))
    return excluded


def header_host(parsed):
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port is not None and parsed.port != default_port:
        return f"{host}:{parsed.port}"
    return host


def read_chunked_body(rfile):
    body = bytearray()
    while True:
        line = rfile.readline()
        if not line:
            raise ValueError("incomplete chunked request body")
        size = int(line.split(b";", 1)[0].strip(), 16)
        if size < 0:
            raise ValueError("invalid chunk size")
        if size == 0:
            # Upstream framing uses Content-Length, so consume chunk trailers here.
            while True:
                trailer = rfile.readline()
                if trailer in (b"\r\n", b"\n"):
                    return bytes(body)
                if not trailer:
                    raise ValueError("incomplete chunked request trailers")

        chunk = rfile.read(size)
        if len(chunk) != size or rfile.read(2) != b"\r\n":
            raise ValueError("incomplete chunked request body")
        body.extend(chunk)


class ProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, engine):
        self.engine = engine
        self.reload_requested = False
        super().__init__(address, handler)

    def request_reload(self, signum, frame):
        # Signal handlers must not import plugins, perform I/O, or acquire locks.
        self.reload_requested = True

    def service_actions(self):
        if not self.reload_requested:
            return
        # Clear before loading so a signal received during reload stays pending.
        self.reload_requested = False
        try:
            self.engine.reload()
        except Exception as exc:
            print(f"Config reload failed: {exc}", file=sys.stderr, flush=True)


def make_handler(upstream, engine, timeout, chunk_size):
    parsed = urllib.parse.urlparse(upstream)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("--upstream must be an http:// or https:// URL with a hostname")
    upstream_host = header_host(parsed)
    upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def read_request_body(self):
            encoding = self.headers.get("Transfer-Encoding", "").lower()
            if "chunked" in encoding:
                return read_chunked_body(self.rfile)
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("invalid Content-Length")
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                raise ValueError("incomplete request body")
            return body

        def build_upstream_headers(self, body):
            incoming = list(self.headers.items())
            excluded = hop_by_hop_headers(incoming) | {"host", "content-length"}
            headers = [(key, value) for key, value in incoming if key.lower() not in excluded]
            headers.append(("Host", upstream_host))
            if body or "Content-Length" in self.headers or self.command in ("POST", "PUT", "PATCH"):
                headers.append(("Content-Length", str(len(body))))
            return headers

        def open_upstream_connection(self):
            if parsed.scheme == "https":
                return http.client.HTTPSConnection(
                    parsed.hostname,
                    upstream_port,
                    timeout=timeout,
                    context=ssl._create_unverified_context(),
                )
            return http.client.HTTPConnection(parsed.hostname, upstream_port, timeout=timeout)

        def proxy_request(self):
            connection = None
            response_started = False
            try:
                # Capture before reading the body; reload cannot change this request.
                chain = engine.snapshot()
                body = self.read_request_body()
                if self.command == "POST" and urllib.parse.urlsplit(self.path).path.endswith("/responses"):
                    body = engine.rewrite(body, chain)
                headers = self.build_upstream_headers(body)
                connection = self.open_upstream_connection()

                # Preserve duplicate headers and avoid adding Accept-Encoding.
                connection.putrequest(
                    self.command, self.path, skip_host=True, skip_accept_encoding=True
                )
                for key, value in headers:
                    connection.putheader(key, value)
                connection.endheaders(body if body else None)
                response = connection.getresponse()
                response_headers = response.getheaders()
                excluded = hop_by_hop_headers(response_headers)
                is_sse = "text/event-stream" in response.getheader("Content-Type", "").lower()

                # After headers begin, failures must close the stream, not append 502.
                response_started = True
                self.send_response_only(response.status, response.reason)
                for key, value in response_headers:
                    if key.lower() not in excluded:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command == "HEAD":
                    return

                while True:
                    data = response.readline() if is_sse else response.read(chunk_size)
                    if not data:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
            except Exception:
                if not response_started and not self.wfile.closed:
                    try:
                        self.send_error(502, "Bad Gateway")
                    except OSError:
                        pass
            finally:
                self.close_connection = True
                if connection is not None:
                    connection.close()

        do_GET = proxy_request
        do_POST = proxy_request
        do_PUT = proxy_request
        do_PATCH = proxy_request
        do_DELETE = proxy_request
        do_HEAD = proxy_request
        do_OPTIONS = proxy_request

    return ProxyHandler


def positive_pid(value):
    try:
        pid = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("PID must be a positive integer")
    if pid <= 0:
        raise argparse.ArgumentTypeError("PID must be a positive integer")
    return pid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    reload_parser = subparsers.add_parser("reload", help="Send SIGHUP to a running proxy")
    reload_parser.add_argument("pid", type=positive_pid, metavar="PID")
    parser.add_argument("-b", "--listen-host", default="127.0.0.1", help="Listen interface (default: 127.0.0.1)")
    parser.add_argument("-p", "--listen-port", type=int, default=8000, help="Listen port (default: 8000)")
    parser.add_argument("-u", "--upstream", help="Upstream origin, http://host or https://host")
    parser.add_argument("-t", "--timeout", type=float, default=None, help="Upstream socket timeout in seconds (default: None)")
    parser.add_argument("-s", "--chunk-size", type=int, default=8192, help="Read buffer size in bytes (default: 8192)")
    args = parser.parse_args()

    if args.command == "reload":
        try:
            os.kill(args.pid, signal.SIGHUP)
        except (OSError, OverflowError) as exc:
            print(f"Could not signal PID {args.pid}: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.upstream:
        parser.error("--upstream is required")
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be positive")

    server = None
    try:
        engine = ReplacementEngine(Path(__file__).resolve().with_name("config.json"))
        handler = make_handler(args.upstream, engine, args.timeout, args.chunk_size)
        server = ProxyServer((args.listen_host, args.listen_port), handler, engine)
        signal.signal(signal.SIGHUP, server.request_reload)
        print(f"PID: {os.getpid()}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Proxy error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if server is not None:
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
