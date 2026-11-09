"""The only way the app touches the network (WORKSPLIT constraint 2).

The transport is **`/usr/bin/curl`**: Python's urllib gets HTTP 403 from JobsDB
whatever headers are sent (TLS fingerprint at their CDN), while curl gets 200 with
an honest User-Agent. The politeness rules sit above the transport and are
unchanged:

* a browser-free, honest User-Agent for every host;
* **no cookies at all** - no cookie jar, no `-b`, no `-c`, never a `Cookie` header;
* a 20 s timeout;
* at least ``[limits] min_seconds_between_requests`` between requests to the same
  host, shared by search pages and JD fetches;
* at most ``max_requests_per_host_per_run`` per host, overridden by
  ``[limits.per_host]`` (LinkedIn needs 150 because search and JD fetches share it);
* HTTP 429, 999, or a login wall / authwall / captcha stops that host for the run,
  and later calls to it fail fast without a request.

Transport, clock and sleep are injected, so unit tests never open a socket (one
integration test does use a throwaway 127.0.0.1 server to exercise real curl).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "job-hunt/1.0 (local job search tool)"
CURL = "/usr/bin/curl"
TIMEOUT = 20.0

WALL_MARKERS = (
    # Only phrases a wall actually shows. A normal page may embed a reCAPTCHA script,
    # so the bare word "captcha" is deliberately not here.
    "authwall",
    "security verification",
    "unusual activity",
    "are you a robot",
    "verify you are a human",
    "request blocked",
    "too many requests",
)
WALL_PATHS = ("/authwall", "/checkpoint", "/uas/login", "/login")

# curl exit codes worth naming (https://curl.se/docs/manpage.html#EXIT)
EXIT_KINDS = {
    6: "resolve",
    7: "connect",
    28: "timeout",
    35: "tls",
    47: "redirect",
    52: "empty",
    55: "send",
    56: "recv",
    60: "tls",
}


class HostStopped(RuntimeError):
    """That host is out for the rest of this run."""

    def __init__(self, host: str, reason: str):
        super().__init__(f"{host} stopped for this run: {reason}")
        self.host = host
        self.reason = reason


class FetchError(RuntimeError):
    """One request failed at the transport level: DNS, connection, TLS, timeout."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # 'resolve' | 'connect' | 'timeout' | 'tls' | 'redirect' | ...


@dataclass
class Response:
    requested_url: str
    url: str
    status: int
    text: str
    headers: dict = field(default_factory=dict)
    raw: bytes = b""          # the body as received, for images

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _parse_header_file(path: Path) -> dict:
    """The headers of the last response block (curl writes one block per redirect)."""
    blocks: list[dict] = []
    current: dict | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        if line.startswith("HTTP/"):
            current = {}
            blocks.append(current)
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key.strip().casefold()] = value.strip()
    return blocks[-1] if blocks else {}


class CurlTransport:
    """Real transport: one `/usr/bin/curl` process per request, no cookie stores."""

    def __init__(self, timeout: float = TIMEOUT, curl_path: str = CURL, runner=None):
        self.timeout = timeout
        self.curl_path = curl_path
        self.runner = runner or subprocess.run

    def argv(self, url: str, headers: dict, method: str, headers_file: Path,
             body_file: Path, data_file: Path | None, timeout: float | None = None) -> list[str]:
        argv = [
            self.curl_path,
            "-q",                       # first, so ~/.curlrc is ignored
            "-sS",
            "--max-time", str(int(timeout if timeout is not None else self.timeout)),
            "--compressed",
        ]
        user_agent = None
        for key, value in headers.items():
            if key.casefold() == "user-agent":
                user_agent = value
            else:
                argv += ["-H", f"{key}: {value}"]
        argv += ["-A", user_agent or USER_AGENT]
        if method != "GET":
            argv += ["-X", method.upper()]
        if data_file is not None:
            argv += ["--data-binary", f"@{data_file}"]
        argv += [
            "-D", str(headers_file),
            "-o", str(body_file),
            "-w", "%{http_code} %{url_effective}",
            "-L",
            "--max-redirs", "5",
            url,
        ]
        return argv

    def __call__(self, url: str, headers: dict, method: str = "GET",
                 data: bytes | None = None,
                 timeout: float | None = None) -> tuple[int, str, dict, bytes]:
        with tempfile.TemporaryDirectory(prefix="jobhunt-curl-") as workdir:
            work = Path(workdir)
            headers_file = work / "headers.txt"
            body_file = work / "body.bin"
            data_file = None
            if data is not None:
                data_file = work / "request.bin"
                data_file.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))

            argv = self.argv(url, headers, method, headers_file, body_file, data_file, timeout)
            try:
                limit = (timeout if timeout is not None else self.timeout) + 15
                completed = self.runner(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=limit)
            except subprocess.TimeoutExpired as exc:
                raise FetchError("timeout", f"curl gave up after {limit:.0f}s") from exc
            except OSError as exc:
                raise FetchError("spawn", f"could not start curl: {exc}") from exc

            if completed.returncode != 0:
                kind = EXIT_KINDS.get(completed.returncode, "curl")
                detail = (completed.stderr or "").strip()[-200:]
                raise FetchError(kind, f"curl exited {completed.returncode}: {detail}")

            status_text, _, final_url = (completed.stdout or "").strip().partition(" ")
            try:
                status = int(status_text)
            except ValueError:
                raise FetchError("curl", f"curl reported no HTTP status: {completed.stdout!r}")
            if not final_url:
                final_url = url
            body = body_file.read_bytes() if body_file.exists() else b""
            return status, final_url, _parse_header_file(headers_file), body


@dataclass
class _HostState:
    count: int = 0
    last_at: float | None = None
    stop_reason: str | None = None


class PoliteFetcher:
    def __init__(self, profile: dict, transport=None, sleep=None, now=None, log=None):
        limits = profile.get("limits", {})
        self.spacing = float(limits.get("min_seconds_between_requests", 3))
        self.default_cap = int(limits.get("max_requests_per_host_per_run", 40))
        per_host = {k: v for k, v in (limits.get("per_host") or {}).items() if isinstance(v, int)}
        self.per_host_caps = per_host
        self.delay_per_host = {k: float(v) for k, v in (limits.get("delay_per_host") or {}).items()
                               if isinstance(v, (int, float))}
        #: Big list endpoints need longer than the default 20 s.
        self.timeout_per_host = {k: float(v) for k, v in
                                 (limits.get("timeout_per_host") or {}).items()
                                 if isinstance(v, (int, float))}
        self.transport = transport or CurlTransport()
        self.sleep = sleep or time.sleep
        self.now = now or time.monotonic
        self.log = log or (lambda event: None)
        self.hosts: dict[str, _HostState] = {}
        self.events: list[dict] = []

    # -- state helpers ------------------------------------------------------ #
    def state(self, host: str) -> _HostState:
        return self.hosts.setdefault(host, _HostState())

    def cap_for(self, host: str) -> int:
        return self.per_host_caps.get(host, self.default_cap)

    def spacing_for(self, host: str) -> float:
        return self.delay_per_host.get(host, self.spacing)

    def timeout_for(self, host: str) -> float:
        return self.timeout_per_host.get(host, float(getattr(self.transport, "timeout",
                                                              TIMEOUT)))

    def stopped_hosts(self) -> dict[str, str]:
        return {host: state.stop_reason for host, state in self.hosts.items() if state.stop_reason}

    def stats(self) -> dict[str, dict]:
        return {host: {"requests": state.count, "stopped": state.stop_reason}
                for host, state in sorted(self.hosts.items())}

    def _event(self, **event) -> None:
        self.events.append(event)
        self.log(event)

    def _attempt(self, url, headers, method, data, timeout):
        """One transport call; the timeout travels with it so curl can honour it."""
        try:
            return self.transport(url, headers, method=method, data=data, timeout=timeout)
        except TypeError:
            # a transport that does not accept a timeout (a plain test fake)
            return self.transport(url, headers, method=method, data=data)

    def stop(self, host: str, reason: str) -> None:
        state = self.state(host)
        if not state.stop_reason:
            state.stop_reason = reason
            self._event(event="host_stopped", host=host, reason=reason)

    # -- the request -------------------------------------------------------- #
    def get(self, url: str, accept: str = "text/html,application/xhtml+xml") -> Response:
        return self.request(url, method="GET", accept=accept)

    def post(self, url: str, data, accept: str = "application/json",
             content_type: str = "application/json") -> Response:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
        return self.request(url, method="POST", accept=accept, data=body,
                            content_type=content_type)

    def request(self, url: str, method: str = "GET", accept: str = "text/html",
                data: bytes | None = None, content_type: str | None = None) -> Response:
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.casefold()
        state = self.state(host)

        if state.stop_reason:
            raise HostStopped(host, state.stop_reason)
        cap = self.cap_for(host)
        if state.count >= cap:
            self.stop(host, f"request cap reached ({cap})")
            raise HostStopped(host, state.stop_reason)

        if state.last_at is not None:
            wait = self.spacing_for(host) - (self.now() - state.last_at)
            if wait > 0:
                self._event(event="wait", host=host, seconds=round(wait, 3), url=url)
                self.sleep(wait)

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
        }
        if content_type:
            headers["Content-Type"] = content_type

        state.count += 1
        state.last_at = self.now()
        timeout = self.timeout_for(host)
        try:
            status, final_url, response_headers, body = self._attempt(
                url, headers, method, data, timeout)
        except FetchError as exc:
            if exc.kind == "timeout":
                # one retry for a slow host (curl exit 28), after the usual delay
                self._event(event="retry", host=host, url=url, kind=exc.kind)
                self.sleep(self.spacing_for(host))
                state.count += 1
                state.last_at = self.now()
                try:
                    status, final_url, response_headers, body = self._attempt(
                        url, headers, method, data, timeout)
                except FetchError as second:
                    self._event(event="fetch_error", host=host, url=url, kind=second.kind)
                    raise
            else:
                self._event(event="fetch_error", host=host, url=url, kind=exc.kind)
                raise
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or "")
        self._event(event="request", host=host, url=url, status=status, bytes=len(text))

        if status in (429, 999):
            self.stop(host, f"HTTP {status}")
            raise HostStopped(host, state.stop_reason)
        if status in (401, 403) or _looks_like_wall(final_url, text):
            self.stop(host, f"login wall / captcha (HTTP {status})")
            raise HostStopped(host, state.stop_reason)
        return Response(requested_url=url, url=final_url, status=status, text=text,
                        headers=response_headers,
                        raw=body if isinstance(body, bytes) else text.encode("utf-8"))


def _looks_like_wall(url: str, text: str) -> bool:
    path = urllib.parse.urlsplit(url or "").path.casefold()
    if any(marker in path for marker in WALL_PATHS):
        return True
    lowered = (text or "").casefold()
    return any(marker in lowered for marker in WALL_MARKERS)
