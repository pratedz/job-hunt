"""The local board server: static files from ``board/`` plus the JSON API.

Binds 127.0.0.1 only (WORKSPLIT constraint 7), serves the UI's plain HTML/CSS/JS
with no build step, and routes ``/api/*`` to :mod:`jobhunt.board_api`.

Every POST must carry ``X-Requested-With: jobhunt`` - a cheap guard against a
random page in the same browser posting to the local server. Errors are JSON
``{"ok": false, "error": "..."}`` with a real status code.

The LLM and the network fetcher are injected, so tests drive the whole API
(fake codex, fake transport) without touching either.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import board_api, llm as llm_mod, logos as logos_mod, store, wiki_setup
from .board_api import ApiError
from .board_daily import DailyControl
from .tracker import Tracker
from .vault import Vault

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}
POST_HEADER = "X-Requested-With"
POST_HEADER_VALUE = "jobhunt"


class _PgPool:
    """One SQLite connection per thread; WAL keeps readers out of each other's way."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.local = threading.local()
        self._lock = threading.Lock()
        self._all: list = []

    def conn(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = store.connect(self.db_path)
            self.local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def close(self):
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all.clear()


class BoardApp:
    """Everything a request needs, with no HTTP in it."""

    def __init__(self, cfg, db_path, vault_path=None, llm_factory=None, fetcher_factory=None,
                 static_dir=None, llm_probe_runner=None):
        self.cfg = cfg
        self.pool = _PgPool(db_path)
        self._pinned_vault_path = Path(vault_path) if vault_path is not None else None
        resolved_vault = self._pinned_vault_path or cfg.vault_path
        records_sub = cfg.get("vault.records_dir", "61 Application Records")
        (Path(resolved_vault) / records_sub).mkdir(parents=True, exist_ok=True)
        self.vault = Vault(resolved_vault, records_sub)
        self.llm_factory = llm_factory
        self.fetcher_factory = fetcher_factory
        self.llm_probe_runner = llm_probe_runner
        self.static_dir = Path(static_dir) if static_dir else cfg.root / "board"
        self._llm = None
        self._cover_llm = None
        self._onboarding_llm = None
        self._fetcher = None
        self.daily = DailyControl(cfg, db_path, resolved_vault)

    def _apply_cfg(self, new_cfg) -> None:
        self.cfg = new_cfg
        self._llm = None
        self._cover_llm = None
        self._onboarding_llm = None
        self._fetcher = None
        resolved_vault = self._pinned_vault_path or new_cfg.vault_path
        records_sub = new_cfg.get("vault.records_dir", "61 Application Records")
        (Path(resolved_vault) / records_sub).mkdir(parents=True, exist_ok=True)
        self.vault = Vault(resolved_vault, records_sub)
        self.daily = DailyControl(new_cfg, self.pool.db_path, resolved_vault)

    # -- injected services -------------------------------------------------- #
    @property
    def llm(self):
        if self._llm is None:
            if self.llm_factory is not None:
                self._llm = self.llm_factory()
            else:
                self._llm = llm_mod.build(self.cfg.profile, self.cfg.schema_path)
        return self._llm

    @property
    def cover_llm(self):
        """Cover emails use the same selected OAuth provider with `medium` reasoning effort."""
        if self._cover_llm is None:
            if self.llm_factory is not None:
                self._cover_llm = self.llm_factory()
            else:
                self._cover_llm = llm_mod.build(self.cfg.profile, self.cfg.cover_schema_path,
                                                effort="medium")
        return self._cover_llm

    @property
    def onboarding_llm(self):
        """Career Wiki onboarding uses the selected OAuth provider with `medium` reasoning effort."""
        if self._onboarding_llm is None:
            if self.llm_factory is not None:
                self._onboarding_llm = self.llm_factory()
            else:
                self._onboarding_llm = llm_mod.build(
                    self.cfg.profile, self.cfg.onboarding_schema_path, effort="medium"
                )
        return self._onboarding_llm

    @property
    def fetcher(self):
        if self._fetcher is None:
            if self.fetcher_factory is not None:
                self._fetcher = self.fetcher_factory()
            else:
                from .http import PoliteFetcher
                self._fetcher = PoliteFetcher(self.cfg.profile)
        return self._fetcher

    def tracker(self) -> Tracker:
        return Tracker(self.vault, today=board_api.today_bangkok)

    # -- routing ------------------------------------------------------------ #
    def handle(self, method: str, path: str, query: dict, body: dict | None):
        conn = self.pool.conn()
        parts = [part for part in path.split("/") if part]

        if method == "GET":
            if path == "/api/daily":
                return self.daily.status()
            if path == "/api/stats":
                return board_api.stats(conn, self.tracker(),
                                       region=(query.get("region") or ["all"])[0],
                                       tab=(query.get("tab") or ["all"])[0])
            if path == "/api/jobs":
                return board_api.list_jobs(
                    conn, self.tracker(),
                    tab=(query.get("tab") or ["all"])[0],
                    q=(query.get("q") or [""])[0],
                    limit=_int(query, "limit", 100),
                    offset=_int(query, "offset", 0),
                    region=(query.get("region") or ["all"])[0],
                    companies=logos_mod.logo_sources(self.cfg),
                )
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cover-email":
                return board_api.cover_email_for(conn, self.tracker(), _int_path(parts[2]),
                                                 self.cfg, self.cover_llm, regenerate=False)
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                return board_api.get_job(conn, self.tracker(), _int_path(parts[2]),
                                         companies=logos_mod.logo_sources(self.cfg))
            if path == "/api/applications":
                return board_api.applications(self.tracker(),
                                              (query.get("status") or [None])[0], conn=conn,
                                              companies=logos_mod.logo_sources(self.cfg))
            if path == "/api/site-changes":
                return board_api.site_changes(conn)
            if path == "/api/suggestions":
                return board_api.suggestions(conn, self.tracker())
            if path == "/api/lessons":
                return board_api.lessons(self.tracker())
            if path == "/api/wiki":
                return wiki_setup.get_wiki_state(self.cfg, runner=self.llm_probe_runner)
            raise ApiError(404, f"No route for GET {path}")

        if method == "POST":
            if path == "/api/daily":
                return self.daily.start()
            if path == "/api/wiki/provider":
                payload = body or {}
                new_cfg, res = wiki_setup.save_provider(
                    self.cfg,
                    str(payload.get("provider") or ""),
                    str(payload.get("model") or ""),
                    runner=self.llm_probe_runner,
                )
                self._apply_cfg(new_cfg)
                return res
            if path == "/api/wiki/extract-resume":
                return wiki_setup.extract_uploaded_resume(body or {})
            if path == "/api/wiki/generate":
                return wiki_setup.generate_wiki(self.cfg, self.onboarding_llm, body or {})
            if path == "/api/wiki/save":
                new_cfg, res = wiki_setup.save_wiki(
                    self.cfg, body or {}, conn=conn, runner=self.llm_probe_runner
                )
                self._apply_cfg(new_cfg)
                return res
            if len(parts) == 5 and parts[:2] == ["api", "jobs"] and parts[3:] == ["cover-email", "reveal"]:
                return board_api.reveal_draft(conn, self.tracker(), _int_path(parts[2]), self.cfg)
            if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                job_id = _int_path(parts[2])
                action = parts[3]
                if action in ("skip", "unskip"):
                    return board_api.skip(conn, self.tracker(), job_id, action)
                if action == "apply":
                    # M10: no model call on Apply; a cover email is on demand only
                    return board_api.apply_for_job(conn, self.tracker(), job_id,
                                                   companies=logos_mod.logo_sources(self.cfg))
                if action == "score":     # M12 "Score anyway"
                    return board_api.score_now(self.cfg, conn, self.tracker(), job_id,
                                               self.llm, self.fetcher)
                if action == "cover-email":
                    return board_api.cover_email_for(conn, self.tracker(), job_id, self.cfg,
                                                     self.cover_llm, regenerate=True)
                raise ApiError(404, f"No route for POST {path}")
            if len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] in ("confirm", "dismiss"):
                suggestion_id = _int_path(parts[2])
                if parts[3] == "confirm":
                    return board_api.confirm_suggestion(conn, self.tracker(), suggestion_id,
                                                        body or {})
                return board_api.dismiss_suggestion(conn, suggestion_id)
            if path == "/api/applications/status":
                return board_api.change_status(self.tracker(), body or {})
            if path == "/api/applications/submitted":
                return board_api.mark_submitted(self.tracker(), (body or {}).get("record_file"))
            if path == "/api/paste":
                return board_api.paste(self.cfg, conn, self.tracker(), body or {},
                                       self.llm, self.fetcher)
            if len(parts) == 4 and parts[:2] == ["api", "site-changes"] and parts[3] == "seen":
                return board_api.mark_site_change_seen(conn, _int_path(parts[2]))
            raise ApiError(404, f"No route for POST {path}")

        raise ApiError(405, f"{method} is not supported")


def _int(query: dict, key: str, default: int) -> int:
    try:
        return max(0, int((query.get(key) or [default])[0]))
    except (TypeError, ValueError):
        return default


def _int_path(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise ApiError(400, f"Bad id: {text}")


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "jobhunt-board/0.1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------- #
    def log_message(self, fmt, *args):  # keep the terminal quiet
        return

    @property
    def app(self) -> BoardApp:
        return self.server.app

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        headers.update(extra or {})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._json(status, {"ok": False, "error": message})

    # -- GET ---------------------------------------------------------------- #
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    # -- POST --------------------------------------------------------------- #
    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query, keep_blank_values=True)

        if path.startswith("/logos/"):
            if method != "GET":
                return self._error(405, "Only GET is allowed for logos")
            return self._logo(path[len("/logos/"):])

        if not path.startswith("/api/"):
            if method != "GET":
                return self._error(405, "Only GET is allowed for static files")
            return self._static(path)

        if method == "POST":
            if self.headers.get(POST_HEADER) != POST_HEADER_VALUE:
                return self._error(403, f"Missing {POST_HEADER}: {POST_HEADER_VALUE}")

        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > 14 * 1024 * 1024:
                return self._error(413, "Request payload exceeds 14 MB limit")
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._error(400, "The request body is not valid JSON")
                if not isinstance(body, dict):
                    return self._error(400, "The request body must be a JSON object")

        try:
            result = self.app.handle(method, path, query, body)
        except ApiError as exc:
            return self._error(exc.status, exc.message)
        except Exception as exc:  # never leak a traceback into the UI
            return self._error(500, f"{type(exc).__name__}: {exc}")
        self._json(200, result)

    # -- logos -------------------------------------------------------------- #
    def _logo(self, name: str):
        try:
            path = logos_mod.stored_path(self.app.cfg, name)
        except ValueError:
            # only names the backend writes are served; anything else does not exist
            return self._error(404, "No such logo")
        if not path.is_file():
            return self._error(404, f"No logo {name}")
        kind = path.suffix.lstrip(".").lower()
        content_type = logos_mod.CONTENT_TYPES.get(kind, "application/octet-stream")
        self._send(200, path.read_bytes(), content_type,
                   extra={"Cache-Control": "max-age=604800"})

    # -- static files ------------------------------------------------------- #
    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if ".." in Path(rel).parts:
            return self._error(403, "Refusing to leave the board folder")
        target = (self.app.static_dir / rel).resolve()
        root = self.app.static_dir.resolve()
        if root not in target.parents and target != root:
            return self._error(403, "Refusing to leave the board folder")
        if not target.is_file():
            return self._error(404, f"No file {rel}")
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), content_type)


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, BoardHandler)

    def server_close(self):
        self.app.pool.close()
        super().server_close()


def create_server(cfg, db_path=None, vault_path=None, host=None, port=None,
                  llm_factory=None, fetcher_factory=None, static_dir=None) -> BoardServer:
    db = db_path or cfg.db_path
    bind_host = host or cfg.get("board.host", "127.0.0.1")
    if bind_host not in ("127.0.0.1", "localhost"):
        # WORKSPLIT constraint 7: localhost only, whatever the config says.
        bind_host = "127.0.0.1"
    bind_port = int(port if port is not None else cfg.get("board.port", 8765))
    app = BoardApp(cfg, db, vault_path=vault_path, llm_factory=llm_factory,
                   fetcher_factory=fetcher_factory, static_dir=static_dir)
    return BoardServer((bind_host, bind_port), app)


def serve(cfg, db_path=None, vault_path=None, host=None, port=None, open_browser=True,
          llm_factory=None, fetcher_factory=None, static_dir=None) -> BoardServer:
    server = create_server(cfg, db_path=db_path, vault_path=vault_path, host=host, port=port,
                           llm_factory=llm_factory, fetcher_factory=fetcher_factory,
                           static_dir=static_dir)
    url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"job-hunt board: {url}")
    print(f"  database: {db_path or cfg.db_path}")
    print(f"  wiki:     {vault_path or cfg.vault_path}")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nboard stopped")
    finally:
        server.server_close()
    return server
