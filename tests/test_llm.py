import json
import tempfile
import unittest
from pathlib import Path

from _util import profile

from jobhunt import llm as llm_mod
from jobhunt.llm import ClaudeLLM, CodexLLM, FakeLLM, GeminiLLM, LLMError

CODEX_STUB = '''#!/usr/bin/env python3
"""Stand-in for `codex exec`: no network, no model, no codex."""
import json, pathlib, sys, time

argv = sys.argv[1:]
out = pathlib.Path(argv[argv.index("-o") + 1])
mode = pathlib.Path(__file__).with_suffix(".mode").read_text().strip()
if mode == "ok":
    out.write_text(json.dumps({"jobs": []}))
elif mode == "garbage":
    out.write_text("not json at all")
elif mode == "schema":
    out.write_text(json.dumps({"jobs": [{"id": 1}]}))
elif mode == "nothing":
    pass
elif mode == "exit":
    print("codex exploded", file=sys.stderr)
    sys.exit(3)
elif mode == "slow":
    time.sleep(5)
    out.write_text(json.dumps({"jobs": []}))
'''

CLAUDE_STUB = '''#!/usr/bin/env python3
"""Stand-in for `claude -p`: no network, no model."""
import json, pathlib, sys

mode = pathlib.Path(__file__).with_suffix(".mode").read_text().strip()
if mode == "ok":
    print(json.dumps({"type": "result", "is_error": False, "structured_output": {"jobs": []}}))
elif mode == "string_result":
    print(json.dumps({"type": "result", "is_error": False, "result": "```json\\n{\\"jobs\\": []}\\n```"}))
elif mode == "error":
    print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
'''

GEMINI_STUB = '''#!/usr/bin/env python3
"""Stand-in for `gemini -p --output-format json`: no network, no model."""
import json, pathlib, sys

count_file = pathlib.Path(__file__).with_suffix(".count")
count = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(count))

mode = pathlib.Path(__file__).with_suffix(".mode").read_text().strip()
if mode == "ok":
    print(json.dumps({"response": "{\\"jobs\\": []}"}))
elif mode == "retry_then_ok":
    if count == 1:
        print(json.dumps({"response": "{\\"jobs\\": [{\\"id\\": 1}]}"}))
    else:
        print(json.dumps({"response": "{\\"jobs\\": []}"}))
elif mode == "always_bad":
    print(json.dumps({"response": "{\\"wrong\\": 1}"}))
'''


class CodexArgv(unittest.TestCase):
    """Codex OAuth CLI invocation: ignores personal proxy config and runs read-only."""

    def setUp(self):
        self.llm = CodexLLM(profile(**{"llm.codex_path": "/usr/local/bin/codex"}),
                            "schemas/score_batch.schema.json")
        self.argv = self.llm.argv(Path("/tmp/schema.json"), Path("/tmp/out.json"), "PROMPT")

    def test_program_subcommand_and_ignore_user_config(self):
        self.assertEqual(self.argv[:2], ["/usr/local/bin/codex", "exec"])
        self.assertIn("--ignore-user-config", self.argv)

    def test_optional_model_omitted_by_default_and_included_when_set(self):
        self.assertNotIn("-m", self.argv)
        with_model = CodexLLM(profile(**{"llm.model": "gpt-5.4"}), "schemas/score_batch.schema.json")
        argv2 = with_model.argv(Path("/tmp/schema.json"), Path("/tmp/out.json"), "PROMPT")
        self.assertEqual(argv2[argv2.index("-m") + 1], "gpt-5.4")

    def test_sandbox_and_isolation_flags(self):
        self.assertEqual(self.argv[self.argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", self.argv)
        self.assertIn("--ephemeral", self.argv)
        self.assertEqual(self.argv[self.argv.index("--disable") + 1], "hooks")
        self.assertIn("mcp_servers={}", self.argv)
        self.assertIn('web_search="disabled"', self.argv)
        self.assertIn('model_reasoning_effort="low"', self.argv)

    def test_schema_output_and_prompt_position(self):
        self.assertEqual(self.argv[self.argv.index("--output-schema") + 1], "/tmp/schema.json")
        self.assertEqual(self.argv[self.argv.index("-o") + 1], "/tmp/out.json")
        self.assertEqual(self.argv[-1], "PROMPT")

    def test_no_dangerous_flags(self):
        for flag in ("--dangerously-bypass-approvals-and-sandbox", "--add-dir", "--worktree"):
            self.assertNotIn(flag, self.argv)


class MultiProviderBuildAndStubs(unittest.TestCase):
    """Exercises Codex, Claude, and Gemini OAuth CLI runners against local stubs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write_stub(self, name: str, code: str, mode: str = "ok") -> Path:
        script = self.dir / name
        script.write_text(code, encoding="utf-8")
        script.chmod(0o755)
        script.with_suffix(".mode").write_text(mode, encoding="utf-8")
        return script

    def test_build_selects_requested_provider(self):
        self.assertIsInstance(llm_mod.build(profile(**{"llm.provider": "codex"}), "schemas/score_batch.schema.json"), CodexLLM)
        self.assertIsInstance(llm_mod.build(profile(**{"llm.provider": "claude"}), "schemas/score_batch.schema.json"), ClaudeLLM)
        self.assertIsInstance(llm_mod.build(profile(**{"llm.provider": "gemini"}), "schemas/score_batch.schema.json"), GeminiLLM)

    def test_claude_uses_safe_mode_to_keep_oauth_and_parses_structured_output(self):
        script = self._write_stub("fake-claude", CLAUDE_STUB, "ok")
        llm = ClaudeLLM(profile(**{"llm.provider": "claude", "llm.claude_path": str(script)}),
                        "schemas/score_batch.schema.json")
        argv = llm.argv("PROMPT")
        self.assertIn("--safe-mode", argv)
        self.assertNotIn("--bare", argv)
        self.assertIn("--json-schema", argv)
        self.assertEqual(llm.run_batch("prompt", ["1"]), {"jobs": []})

        script.with_suffix(".mode").write_text("string_result", encoding="utf-8")
        self.assertEqual(llm.run("prompt"), {"jobs": []})

        script.with_suffix(".mode").write_text("error", encoding="utf-8")
        with self.assertRaises(LLMError) as ctx:
            llm.run("prompt")
        self.assertEqual(ctx.exception.kind, "exit")

    def test_gemini_parses_json_envelope_and_retries_once_on_schema_mismatch(self):
        script = self._write_stub("fake-gemini", GEMINI_STUB, "retry_then_ok")
        llm = GeminiLLM(profile(**{"llm.provider": "gemini", "llm.gemini_path": str(script)}),
                        "schemas/score_batch.schema.json")
        self.assertEqual(llm.run("prompt"), {"jobs": []})
        self.assertEqual(int(script.with_suffix(".count").read_text()), 2)

        script.with_suffix(".mode").write_text("always_bad", encoding="utf-8")
        with self.assertRaises(LLMError) as ctx:
            llm.run("prompt")
        self.assertEqual(ctx.exception.kind, "schema")

    def test_detect_providers_reports_all_three_subscriptions(self):
        class Done:
            returncode = 0
            stdout = '{"loggedIn": true, "authMethod": "oauth"}'
            stderr = ""

        providers = llm_mod.detect_providers(profile(**{"llm.provider": "claude"}),
                                             runner=lambda *a, **k: Done())
        self.assertEqual([p["id"] for p in providers], ["codex", "claude", "gemini"])
        selected = [p["id"] for p in providers if p["selected"]]
        self.assertEqual(selected, ["claude"])


class RunBatchAgainstCodexStub(unittest.TestCase):
    """Exercises Codex subprocess error handling without ever starting codex."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.script = self.dir / "fake-codex"
        self.script.write_text(CODEX_STUB, encoding="utf-8")
        self.script.chmod(0o755)
        self.mode("ok")

    def mode(self, name):
        (self.dir / "fake-codex.mode").write_text(name, encoding="utf-8")

    def llm(self, timeout=10):
        return CodexLLM(profile(**{"llm.codex_path": str(self.script),
                                   "llm.timeout_seconds": timeout}),
                        "schemas/score_batch.schema.json")

    def test_a_good_reply_is_parsed(self):
        self.assertEqual(self.llm().run_batch("prompt", ["1"]), {"jobs": []})

    def test_non_zero_exit_is_an_error_with_the_stderr_tail(self):
        self.mode("exit")
        with self.assertRaises(LLMError) as ctx:
            self.llm().run_batch("prompt", ["1"])
        self.assertEqual(ctx.exception.kind, "exit")
        self.assertIn("codex exploded", str(ctx.exception))

    def test_missing_output_file_is_an_error(self):
        self.mode("nothing")
        with self.assertRaises(LLMError) as ctx:
            self.llm().run_batch("prompt", ["1"])
        self.assertEqual(ctx.exception.kind, "io")

    def test_invalid_json_is_an_error(self):
        self.mode("garbage")
        with self.assertRaises(LLMError) as ctx:
            self.llm().run_batch("prompt", ["1"])
        self.assertEqual(ctx.exception.kind, "schema")

    def test_schema_mismatch_is_an_error(self):
        self.mode("schema")
        with self.assertRaises(LLMError) as ctx:
            self.llm().run_batch("prompt", ["1"])
        self.assertEqual(ctx.exception.kind, "schema")

    def test_timeout_is_an_error(self):
        self.mode("slow")
        with self.assertRaises(LLMError) as ctx:
            self.llm(timeout=0.3).run_batch("prompt", ["1"])
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_a_missing_codex_binary_is_an_error(self):
        llm = CodexLLM(profile(**{"llm.codex_path": str(self.dir / "nope")}),
                       "schemas/score_batch.schema.json")
        with self.assertRaises(LLMError) as ctx:
            llm.run_batch("prompt", ["1"])
        self.assertEqual(ctx.exception.kind, "spawn")


class FakeLLMBehaviour(unittest.TestCase):
    def test_replies_are_consumed_then_reused(self):
        llm = FakeLLM([{"jobs": []}, {"jobs": []}])
        llm.run_batch("p", ["1"])
        llm.run_batch("p", ["2"])
        llm.run_batch("p", ["3"])
        self.assertEqual([call["job_ids"] for call in llm.calls], [["1"], ["2"], ["3"]])

    def test_an_exception_reply_is_raised(self):
        with self.assertRaises(LLMError):
            FakeLLM([LLMError("exit", "boom")]).run_batch("p", ["1"])


if __name__ == "__main__":
    unittest.main()
