"""Multi-provider OAuth CLI runtime: choose ONE subscription (`codex`, `claude`, or `gemini`).

Zero API keys and zero local proxy servers are required: each provider runs through
the user's official CLI authenticated via browser OAuth:
  - ``codex``  -> OpenAI Codex CLI (`codex login`)
  - ``claude`` -> Anthropic Claude Code CLI (`claude auth login`)
  - ``gemini`` -> Google Gemini CLI (`gemini` -> Login with Google)

Every call runs in an isolated temporary directory with ``stdin=DEVNULL`` and no
agentic write/network tools enabled, because job descriptions are untrusted text.
Every reply is validated against the caller's JSON Schema before anything is stored.

``FakeLLM`` is what unit tests use; no unit test ever starts a live CLI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import validate as schema_check

PROVIDERS = ("codex", "claude", "gemini")

PROVIDER_SPECS = {
    "codex": {
        "id": "codex",
        "label": "Codex (ChatGPT OAuth)",
        "subscription": "ChatGPT Plus, Pro, or Team",
        "binary": "codex",
        "path_key": "codex_path",
        "install_cmd": "npm install -g @openai/codex",
        "login_cmd": "codex login",
    },
    "claude": {
        "id": "claude",
        "label": "Claude (Claude OAuth)",
        "subscription": "Claude Pro, Max, or Team",
        "binary": "claude",
        "path_key": "claude_path",
        "install_cmd": "npm install -g @anthropic-ai/claude-code",
        "login_cmd": "claude auth login",
    },
    "gemini": {
        "id": "gemini",
        "label": "Gemini (Google OAuth)",
        "subscription": "Google Account / Google AI Pro",
        "binary": "gemini",
        "path_key": "gemini_path",
        "install_cmd": "npm install -g @google/gemini-cli",
        "login_cmd": "gemini",
    },
}


class LLMError(RuntimeError):
    """An LLM CLI call that produced no usable schema-valid answer."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # 'spawn' | 'timeout' | 'exit' | 'io' | 'schema'


class FakeLLM:
    """Canned replies for tests. ``replies`` is a list (consumed per call) or a callable."""

    def __init__(self, replies):
        self._replies = list(replies) if isinstance(replies, list) else [replies]
        self.calls: list[dict] = []

    def run(self, prompt: str, job_ids: list[str] | None = None) -> dict:
        return self._run(prompt, list(job_ids or []))

    def run_batch(self, prompt: str, job_ids: list[str]) -> dict:
        return self._run(prompt, list(job_ids))

    def _run(self, prompt: str, job_ids: list[str]) -> dict:
        self.calls.append({"prompt": prompt, "job_ids": list(job_ids)})
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(prompt, job_ids)
        return reply


def _extra_bin_dirs() -> list[str]:
    home = Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    return [str(p) for p in candidates if p.is_dir()]


def resolve_binary(configured: str | None, default_name: str) -> str | None:
    """Return an executable path if present on disk or PATH, else None."""
    target = (configured or "").strip() or default_name
    expanded = Path(target).expanduser()
    if "/" in target or "\\" in target:
        return str(expanded) if (expanded.is_file() and os.access(expanded, os.X_OK)) else None
    path_env = os.environ.get("PATH", "")
    extra = os.pathsep.join(_extra_bin_dirs())
    search_path = f"{path_env}{os.pathsep}{extra}" if path_env else extra
    return shutil.which(target, path=search_path)


def _extract_json_object(raw: str) -> dict:
    """Parse a JSON object from raw model text, stripping optional markdown fences."""
    text = (raw or "").strip()
    if not text:
        raise json.JSONDecodeError("Empty response", "", 0)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    elif not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("Expected a JSON object", text, 0)
    return parsed


class _BaseCLILLM:
    provider_id: str = "codex"

    def __init__(self, profile: dict, schema_path: str | Path, effort: str | None = None):
        llm = profile.get("llm", {})
        spec = PROVIDER_SPECS[self.provider_id]
        raw_bin = llm.get(spec["path_key"]) or llm.get("binary_path")
        self.configured_bin = (str(raw_bin).strip() if raw_bin else "") or spec["binary"]
        self.executable = resolve_binary(self.configured_bin, spec["binary"]) or self.configured_bin
        self.model = str(llm.get("model") or "").strip()
        self.effort = effort or str(llm.get("reasoning_effort") or "low").strip()
        self.timeout = float(llm.get("timeout_seconds", 300))
        self.schema_path = Path(schema_path)
        self.schema = json.loads(self.schema_path.read_text(encoding="utf-8"))

    def run(self, prompt: str, job_ids: list[str] | None = None) -> dict:
        return self._run(prompt)

    def run_batch(self, prompt: str, job_ids: list[str]) -> dict:
        return self._run(prompt)

    def _validate(self, reply: dict) -> dict:
        try:
            schema_check.validate(self.schema, reply)
        except schema_check.SchemaError as exc:
            raise LLMError("schema", f"reply does not match the schema: {exc}") from exc
        return reply

    def _exec(self, argv: list[str], workdir: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        extra = _extra_bin_dirs()
        if extra:
            env["PATH"] = env.get("PATH", "") + os.pathsep + os.pathsep.join(extra)
        try:
            return subprocess.run(
                argv,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError("timeout", f"{self.provider_id} timed out after {self.timeout:.0f}s") from exc
        except OSError as exc:
            raise LLMError("spawn", f"could not start {self.provider_id}: {exc}") from exc


class CodexLLM(_BaseCLILLM):
    """OpenAI Codex CLI (`codex exec`) via ChatGPT OAuth."""

    provider_id = "codex"

    def __init__(self, profile: dict, schema_path: str | Path, effort: str | None = None):
        super().__init__(profile, schema_path, effort=effort)
        self.codex_path = self.executable

    def argv(self, schema_file: Path, out_file: Path, prompt: str) -> list[str]:
        cmd = [
            self.codex_path,
            "exec",
            "--ignore-user-config",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--disable", "hooks",
            "-c", "mcp_servers={}",
            "-c", 'web_search="disabled"',
            "-c", f'model_reasoning_effort="{self.effort}"',
        ]
        if self.model:
            cmd.extend(["-m", self.model])
        cmd.extend(["--output-schema", str(schema_file), "-o", str(out_file), prompt])
        return cmd

    def _run(self, prompt: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="jobhunt-codex-") as workdir:
            workdir_path = Path(workdir)
            schema_file = workdir_path / "output.schema.json"
            schema_file.write_text(json.dumps(self.schema), encoding="utf-8")
            out_file = workdir_path / "answer.json"
            completed = self._exec(self.argv(schema_file, out_file, prompt), workdir)
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip()[-400:]
                raise LLMError("exit", f"codex exited {completed.returncode}: {tail}")
            if not out_file.exists():
                raise LLMError("io", "codex wrote no -o file")
            try:
                reply = json.loads(out_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LLMError("schema", f"reply is not valid JSON: {exc}") from exc
            return self._validate(reply)


class ClaudeLLM(_BaseCLILLM):
    """Anthropic Claude Code CLI (`claude -p`) via Claude OAuth."""

    provider_id = "claude"

    def __init__(self, profile: dict, schema_path: str | Path, effort: str | None = None):
        super().__init__(profile, schema_path, effort=effort)
        self.claude_path = self.executable

    def argv(self, prompt: str) -> list[str]:
        cmd = [
            self.claude_path,
            "-p",
            "--safe-mode",
            "--tools", "",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(self.schema, separators=(",", ":")),
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append(prompt)
        return cmd

    def _run(self, prompt: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="jobhunt-claude-") as workdir:
            completed = self._exec(self.argv(prompt), workdir)
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip()[-400:]
                raise LLMError("exit", f"claude exited {completed.returncode}: {tail}")
            raw = (completed.stdout or "").strip()
            if not raw:
                raise LLMError("io", "claude produced empty output")
            try:
                envelope = json.loads(raw)
                if isinstance(envelope, dict) and envelope.get("is_error"):
                    raise LLMError("exit", str(envelope.get("result") or "claude reported an error"))
                if isinstance(envelope, dict) and envelope.get("structured_output") is not None:
                    structured = envelope["structured_output"]
                    reply = (_extract_json_object(structured) if isinstance(structured, str)
                             else structured)
                elif isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
                    reply = _extract_json_object(envelope["result"])
                elif isinstance(envelope, dict):
                    reply = envelope
                else:
                    raise LLMError("schema", "claude output is not a JSON object")
            except json.JSONDecodeError as exc:
                raise LLMError("schema", f"reply is not valid JSON: {exc}") from exc
            return self._validate(reply)


class GeminiLLM(_BaseCLILLM):
    """Google Gemini CLI (`gemini -p`) via Google Account OAuth."""

    provider_id = "gemini"

    def __init__(self, profile: dict, schema_path: str | Path, effort: str | None = None):
        super().__init__(profile, schema_path, effort=effort)
        self.gemini_path = self.executable

    def _schema_prompt(self, prompt: str, validation_feedback: str | None = None) -> str:
        schema_json = json.dumps(self.schema, indent=2, ensure_ascii=False)
        parts = [
            prompt,
            "",
            "## Output Contract (Strict JSON Only)",
            "Respond ONLY with a single raw JSON object that conforms strictly to the JSON Schema below.",
            "Do not include markdown code fences (```), explanations, or extra keys.",
            "<json_schema>",
            schema_json,
            "</json_schema>",
        ]
        if validation_feedback:
            parts.extend([
                "",
                f"IMPORTANT: Your previous output failed validation ({validation_feedback}). "
                "Fix the JSON object so every required field and type matches the schema.",
            ])
        return "\n".join(parts)

    def argv(self, prompt: str, validation_feedback: str | None = None) -> list[str]:
        cmd = [
            self.gemini_path,
            "-p", self._schema_prompt(prompt, validation_feedback),
            "--output-format", "json",
        ]
        if self.model:
            cmd.extend(["-m", self.model])
        return cmd

    def _parse_gemini_stdout(self, raw: str) -> dict:
        if not raw:
            raise LLMError("io", "gemini produced empty output")
        try:
            envelope = _extract_json_object(raw)
        except json.JSONDecodeError as exc:
            raise LLMError("schema", f"reply is not valid JSON: {exc}") from exc
        if envelope.get("error"):
            err = envelope["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError("exit", f"gemini error: {msg}")
        if "response" in envelope and isinstance(envelope["response"], str):
            try:
                return _extract_json_object(envelope["response"])
            except json.JSONDecodeError as exc:
                raise LLMError("schema", f"gemini response text is not valid JSON: {exc}") from exc
        return envelope

    def _run(self, prompt: str) -> dict:
        last_err: LLMError | None = None
        feedback: str | None = None
        for _attempt in range(2):
            with tempfile.TemporaryDirectory(prefix="jobhunt-gemini-") as workdir:
                completed = self._exec(self.argv(prompt, feedback), workdir)
                if completed.returncode != 0:
                    tail = (completed.stderr or completed.stdout or "").strip()[-400:]
                    raise LLMError("exit", f"gemini exited {completed.returncode}: {tail}")
                try:
                    reply = self._parse_gemini_stdout((completed.stdout or "").strip())
                    return self._validate(reply)
                except LLMError as exc:
                    if exc.kind != "schema":
                        raise
                    last_err = exc
                    feedback = str(exc)
        assert last_err is not None
        raise last_err


def selected_provider(profile: dict | None) -> str:
    llm = (profile or {}).get("llm", {})
    provider = str(llm.get("provider") or "codex").strip().lower()
    return provider if provider in PROVIDERS else "codex"


def build(
    profile: dict,
    schema_path: str | Path,
    effort: str | None = None,
    fake: bool = False,
) -> CodexLLM | ClaudeLLM | GeminiLLM | FakeLLM:
    if fake:
        raise ValueError("FakeLLM needs canned replies; construct it directly")
    provider = selected_provider(profile)
    classes = {
        "codex": CodexLLM,
        "claude": ClaudeLLM,
        "gemini": GeminiLLM,
    }
    cls = classes[provider]
    return cls(profile, schema_path, effort=effort)


def _probe_auth(provider_id: str, exe: str, runner=None) -> tuple[bool, str]:
    """Check whether a provider CLI has an active OAuth session without reading credentials."""
    run_cmd = runner or subprocess.run
    env = dict(os.environ)
    extra = _extra_bin_dirs()
    if extra:
        env["PATH"] = env.get("PATH", "") + os.pathsep + os.pathsep.join(extra)

    if provider_id == "codex":
        try:
            proc = run_cmd(
                [exe, "login", "status"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, env=env,
            )
            combined = f"{proc.stdout or ''} {proc.stderr or ''}".strip()
            if proc.returncode == 0 and "not logged in" not in combined.casefold():
                return True, "Ready · ChatGPT OAuth active"
            return False, "Installed · Run `codex login` to sign in"
        except (OSError, subprocess.SubprocessError):
            return False, "Installed · Could not verify `codex login status`"

    if provider_id == "claude":
        try:
            proc = run_cmd(
                [exe, "auth", "status", "--json"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, env=env,
            )
            raw = (proc.stdout or "").strip()
            if raw:
                try:
                    data = json.loads(raw)
                    if bool(data.get("loggedIn")):
                        method = data.get("authMethod") or "OAuth"
                        return True, f"Ready · Claude {method} active"
                    return False, "Installed · Run `claude auth login` to sign in"
                except json.JSONDecodeError:
                    pass
            if proc.returncode == 0:
                return True, "Ready · Claude OAuth active"
            return False, "Installed · Run `claude auth login` to sign in"
        except (OSError, subprocess.SubprocessError):
            return False, "Installed · Run `claude auth login` to sign in"

    if provider_id == "gemini":
        gemini_dir = Path.home() / ".gemini"
        has_oauth = (
            (gemini_dir / "oauth_creds.json").is_file()
            or (gemini_dir / "google_accounts.json").is_file()
            or bool(os.environ.get("GEMINI_API_KEY"))
        )
        if has_oauth:
            return True, "Ready · Google OAuth active"
        return False, "Installed · Run `gemini` once and choose Login with Google"

    return False, "Unknown provider"


def detect_providers(profile: dict | None = None, runner=None) -> list[dict]:
    """Inspect installation and OAuth status for Codex, Claude, and Gemini CLIs."""
    llm_cfg = (profile or {}).get("llm", {})
    active = selected_provider(profile)
    out = []
    for pid in PROVIDERS:
        spec = PROVIDER_SPECS[pid]
        configured = llm_cfg.get(spec["path_key"]) or (
            llm_cfg.get("binary_path") if pid == active else None
        )
        exe = resolve_binary(str(configured) if configured else None, spec["binary"])
        if exe:
            authed, detail = _probe_auth(pid, exe, runner=runner)
        else:
            authed, detail = False, f"Not installed · `{spec['install_cmd']}`"
        out.append({
            "id": pid,
            "label": spec["label"],
            "subscription": spec["subscription"],
            "install_cmd": spec["install_cmd"],
            "login_cmd": spec["login_cmd"],
            "installed": bool(exe),
            "authenticated": authed,
            "selected": pid == active,
            "detail": detail,
        })
    return out


def check_provider_health(profile: dict | None, runner=None) -> tuple[bool, str]:
    """Check whether the user's single selected AI provider CLI is installed."""
    pid = selected_provider(profile)
    spec = PROVIDER_SPECS[pid]
    llm_cfg = (profile or {}).get("llm", {})
    configured = llm_cfg.get(spec["path_key"]) or llm_cfg.get("binary_path")
    exe = resolve_binary(str(configured) if configured else None, spec["binary"])
    if not exe:
        target = configured or spec["binary"]
        return False, (
            f"{spec['label']} CLI not found ({target}); install with `{spec['install_cmd']}` "
            f"and sign in with `{spec['login_cmd']}`"
        )
    return True, exe
