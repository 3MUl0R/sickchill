"""
Tests for the Claude Code CLI AI provider (ClaudeCLIClient).

The CLI is never actually invoked: subprocess.Popen / subprocess.run are mocked so
these tests run anywhere. They cover envelope parsing, error mapping, timeout/kill,
environment isolation, hot-path-safe auth detection, provider selection, cache-key
separation, and cost/model-id resolution.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from sickchill.oldbeard.ai import cli_client
from sickchill.oldbeard.ai.anthropic_client import AnthropicClient
from sickchill.oldbeard.ai.base_client import (
    AIConfigurationError,
    AIError,
    AIRateLimitError,
    AIResponseError,
)
from sickchill.oldbeard.ai.cli_client import ClaudeCLIClient

CLI = "sickchill.oldbeard.ai.cli_client"


def _envelope(result="{}", is_error=False, **extra):
    env = {"type": "result", "is_error": is_error, "result": result}
    env.update(extra)
    return json.dumps(env).encode("utf-8")


class FakePopen:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0, timeout=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._timeout = timeout
        self.pid = 4321
        self.communicate_calls = 0

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if self._timeout and self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return self._stdout, self._stderr

    def kill(self):
        pass


def _client(**kw):
    c = ClaudeCLIClient(**kw)
    # Pretend the binary resolved so we exercise the request path without PATH lookups.
    c._binary = "claude"
    c._binary_resolved = True
    return c


class TestCliMakeRequest(unittest.TestCase):
    def setUp(self):
        cli_client._reset_auth_cache()

    def _run_with_popen(self, popen, prompt="do a thing", system_prompt=None):
        with mock.patch(f"{CLI}.subprocess.Popen", return_value=popen) as m:
            result = _client()._make_request(prompt, 512, system_prompt)
        return result, m

    def test_success_returns_parsed_dict_and_usage(self):
        popen = FakePopen(
            stdout=_envelope(
                result='{"selected_index": 1, "confidence": 0.9}',
                usage={"input_tokens": 12, "output_tokens": 5},
                modelUsage={"claude-sonnet-4-6": {"inputTokens": 12, "outputTokens": 5}},
            )
        )
        (parsed, usage), _ = self._run_with_popen(popen)
        self.assertEqual(parsed["selected_index"], 1)
        self.assertEqual(usage.input_tokens, 12)
        self.assertEqual(usage.output_tokens, 5)
        self.assertEqual(usage.model, "claude-sonnet-4-6")

    def test_fenced_json_result_is_parsed(self):
        popen = FakePopen(stdout=_envelope(result='```json\n{"ok": true}\n```'))
        (parsed, _usage), _ = self._run_with_popen(popen)
        self.assertEqual(parsed, {"ok": True})

    def test_prompt_sent_via_stdin_and_safe_argv(self):
        popen = FakePopen(stdout=_envelope(result="{}"))
        _result, m = self._run_with_popen(popen, prompt="PROMPT-BODY")
        args, kwargs = m.call_args
        argv = args[0]
        # Prompt must go on stdin (encoded), never in argv, and no shell.
        self.assertNotIn("PROMPT-BODY", argv)
        self.assertFalse(kwargs.get("shell", False))
        self.assertIn("-p", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        # --tools "" (disable tools) present as a flag+value pair
        self.assertIn("--tools", argv)
        self.assertIn("--system-prompt", argv)
        # neutral cwd (system temp), not the repo
        self.assertEqual(kwargs.get("cwd"), cli_client.ClaudeCLIClient._neutral_cwd())

    def test_caller_system_prompt_is_combined_with_json_instruction(self):
        popen = FakePopen(stdout=_envelope(result="{}"))
        _result, m = self._run_with_popen(popen, system_prompt="BE CAREFUL")
        argv = m.call_args[0][0]
        sys_value = argv[argv.index("--system-prompt") + 1]
        self.assertIn("BE CAREFUL", sys_value)
        self.assertIn("JSON", sys_value)

    def test_envelope_error_auth_maps_to_configuration_error(self):
        popen = FakePopen(stdout=_envelope(result="Invalid API key", is_error=True))
        with self.assertRaises(AIConfigurationError):
            self._run_with_popen(popen)

    def test_envelope_error_rate_limit(self):
        popen = FakePopen(stdout=_envelope(result="rate limit exceeded", is_error=True))
        with self.assertRaises(AIRateLimitError):
            self._run_with_popen(popen)

    def test_envelope_error_transient(self):
        popen = FakePopen(stdout=_envelope(result="connection timeout", is_error=True))
        with self.assertRaises(AIError) as ctx:
            self._run_with_popen(popen)
        # transient -> base AIError, not a non-retryable subclass
        self.assertNotIsInstance(ctx.exception, (AIConfigurationError, AIRateLimitError, AIResponseError))

    def test_missing_result_text_raises_response_error(self):
        popen = FakePopen(stdout=_envelope(result=""))
        with self.assertRaises(AIResponseError):
            self._run_with_popen(popen)

    def test_nonzero_exit_with_stderr_auth_maps(self):
        popen = FakePopen(stdout=b"", stderr=b"You are not logged in", returncode=1)
        with self.assertRaises(AIConfigurationError):
            self._run_with_popen(popen)

    def test_unparseable_output_is_retryable(self):
        popen = FakePopen(stdout=b"garbage not json", stderr=b"", returncode=1)
        with self.assertRaises(AIError) as ctx:
            self._run_with_popen(popen)
        self.assertNotIsInstance(ctx.exception, (AIConfigurationError, AIRateLimitError, AIResponseError))

    def test_timeout_kills_tree_and_raises_transient(self):
        popen = FakePopen(timeout=True)
        with mock.patch.object(ClaudeCLIClient, "_kill_tree") as kill:
            with mock.patch(f"{CLI}.subprocess.Popen", return_value=popen):
                with self.assertRaises(AIError) as ctx:
                    _client()._make_request("p", 512, None)
        kill.assert_called_once()
        self.assertNotIsInstance(ctx.exception, (AIConfigurationError, AIRateLimitError, AIResponseError))

    def test_nonzero_exit_with_parseable_json_is_mapped_not_success(self):
        # returncode != 0 but stdout is valid JSON without is_error -> must map as failure
        # (using stderr), never be returned as a success envelope.
        popen = FakePopen(stdout=_envelope(result="{}", is_error=False), returncode=2, stderr=b"network unavailable")
        with self.assertRaises(AIError) as ctx:
            self._run_with_popen(popen)
        self.assertNotIsInstance(ctx.exception, (AIConfigurationError, AIRateLimitError, AIResponseError))

    def test_nonzero_exit_with_error_envelope_is_honored(self):
        # returncode != 0 with an is_error envelope -> the envelope's error wins (auth here).
        popen = FakePopen(stdout=_envelope(result="not logged in", is_error=True), returncode=1)
        with self.assertRaises(AIConfigurationError):
            self._run_with_popen(popen)


class TestChildEnvIsolation(unittest.TestCase):
    def test_strips_api_and_provider_vars_keeps_profile(self):
        fake_env = {
            "ANTHROPIC_API_KEY": "sk-secret",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "http://x",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "HOME": "/home/u",
            "USERPROFILE": "C:/Users/u",
            "APPDATA": "C:/Users/u/AppData",
            "PATH": "/usr/bin",
        }
        with mock.patch(f"{CLI}.os.environ", fake_env):
            env = ClaudeCLIClient._child_env()
        for stripped in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            self.assertNotIn(stripped, env)
        for kept in ("HOME", "USERPROFILE", "APPDATA", "PATH"):
            self.assertIn(kept, env)
        # Original env not mutated
        self.assertIn("ANTHROPIC_API_KEY", fake_env)


class TestAuthDetection(unittest.TestCase):
    def setUp(self):
        cli_client._reset_auth_cache()

    def tearDown(self):
        cli_client._reset_auth_cache()

    def test_binary_missing_returns_false_without_probe(self):
        c = ClaudeCLIClient()
        with mock.patch.object(ClaudeCLIClient, "_resolve_binary", return_value=None):
            with mock.patch.object(ClaudeCLIClient, "_probe_auth") as probe:
                self.assertFalse(c.is_configured())
                probe.assert_not_called()

    def test_cold_start_returns_false_then_warms_in_background(self):
        c = _client()

        # Run the "background" thread synchronously for determinism.
        def run_now(target=None, args=(), name=None, daemon=None):
            t = mock.Mock()
            t.start = lambda: target(*args)
            return t

        with mock.patch.object(ClaudeCLIClient, "_probe_auth", return_value=(True, {"email": "a@b.c"})) as probe:
            with mock.patch(f"{CLI}.threading.Thread", side_effect=run_now):
                first = c.is_configured()  # cold: no value yet -> False, schedules refresh (runs now)
                second = c.is_configured()  # now fresh -> True
        self.assertFalse(first)
        self.assertTrue(second)
        probe.assert_called_once()

    def test_never_probes_synchronously_on_hot_path(self):
        c = _client()
        with mock.patch.object(ClaudeCLIClient, "_probe_auth", return_value=(True, {})) as probe:
            with mock.patch(f"{CLI}.threading.Thread") as thread:
                c.is_configured()
                # is_configured must NOT call _probe_auth directly; it delegates to a thread.
                probe.assert_not_called()
                thread.assert_called_once()

    def test_fresh_cache_returns_without_scheduling(self):
        c = _client()
        with cli_client._auth_lock:
            cli_client._auth_cache.update(checked_at=10_000_000_000.0, last_attempt=10_000_000_000.0, logged_in=True, has_value=True, info={})
        with mock.patch(f"{CLI}.time.time", return_value=10_000_000_001.0):
            with mock.patch(f"{CLI}.threading.Thread") as thread:
                self.assertTrue(c.is_configured())
                thread.assert_not_called()

    def test_refresh_coalesced_when_in_flight(self):
        c = _client()
        cli_client._auth_refresh_in_flight = True  # pretend a refresh is already running
        try:
            with mock.patch(f"{CLI}.threading.Thread") as thread:
                c.is_configured()  # stale/cold but a refresh is in flight -> no new thread
                thread.assert_not_called()
        finally:
            cli_client._auth_refresh_in_flight = False

    def test_reset_during_inflight_probe_discards_stale_result(self):
        # A background refresh started before reset_client() must not repopulate the
        # cache for the now-stale generation.
        c = _client()
        old_gen = cli_client._auth_generation
        cli_client._reset_auth_cache()  # bumps the generation
        with mock.patch.object(ClaudeCLIClient, "_probe_auth", return_value=(True, {"x": 1})):
            c._background_refresh(old_gen)  # stale write attempt
        self.assertFalse(cli_client._auth_cache["has_value"])

    def test_current_generation_probe_updates_cache(self):
        c = _client()
        cli_client._reset_auth_cache()
        gen = cli_client._auth_generation
        with mock.patch.object(ClaudeCLIClient, "_probe_auth", return_value=(True, {"email": "a@b.c"})):
            c._background_refresh(gen)
        self.assertTrue(cli_client._auth_cache["has_value"])
        self.assertTrue(cli_client._auth_cache["logged_in"])

    def test_probe_failure_keeps_last_known(self):
        c = _client()
        with cli_client._auth_lock:
            cli_client._auth_cache.update(checked_at=1.0, last_attempt=1.0, logged_in=True, has_value=True, info={})

        def run_now(target=None, args=(), name=None, daemon=None):
            t = mock.Mock()
            t.start = lambda: target(*args)
            return t

        with mock.patch.object(ClaudeCLIClient, "_probe_auth", side_effect=RuntimeError("boom")):
            with mock.patch(f"{CLI}.threading.Thread", side_effect=run_now):
                with mock.patch(f"{CLI}.time.time", return_value=10_000.0):
                    result = c.is_configured()  # stale -> returns last-known True, refresh fails
        self.assertTrue(result)
        self.assertTrue(cli_client._auth_cache["logged_in"])  # not flipped to False


class TestProbeAuth(unittest.TestCase):
    def test_probe_parses_logged_in(self):
        c = _client()
        with mock.patch.object(ClaudeCLIClient, "_spawn", return_value=(0, '{"loggedIn": true, "email": "a@b.c", "subscriptionType": "max"}', "")):
            logged_in, info = c._probe_auth()
        self.assertTrue(logged_in)
        self.assertEqual(info["email"], "a@b.c")

    def test_probe_handles_not_logged_in(self):
        c = _client()
        with mock.patch.object(ClaudeCLIClient, "_spawn", return_value=(0, '{"loggedIn": false}', "")):
            logged_in, _info = c._probe_auth()
        self.assertFalse(logged_in)

    def test_probe_timeout_is_not_logged_in(self):
        # _spawn raises a transient AIError on timeout (after tree-kill); probe degrades gracefully.
        c = _client()
        with mock.patch.object(ClaudeCLIClient, "_spawn", side_effect=AIError("Transient error: timed out")):
            logged_in, info = c._probe_auth()
        self.assertFalse(logged_in)
        self.assertIn("error", info)

    def test_probe_uses_spawn_with_tree_kill_path(self):
        # The probe must go through _spawn (which tree-kills on timeout), not a bare subprocess.run.
        c = _client()
        with mock.patch.object(ClaudeCLIClient, "_spawn", return_value=(0, '{"loggedIn": true}', "")) as spawn:
            c._probe_auth()
        spawn.assert_called_once()
        argv = spawn.call_args[0][0]
        self.assertEqual(argv[1:], ["auth", "status", "--json"])


class TestTestConnection(unittest.TestCase):
    def setUp(self):
        cli_client._reset_auth_cache()

    def test_not_logged_in_message(self):
        c = _client()
        with mock.patch.object(ClaudeCLIClient, "_probe_auth", return_value=(False, {"error": "nope"})):
            ok, msg = c.test_connection()
        self.assertFalse(ok)
        self.assertIn("not logged in", msg.lower())

    def test_logged_in_round_trip_success(self):
        c = _client()
        with mock.patch.object(ClaudeCLIClient, "_probe_auth", return_value=(True, {"email": "a@b.c", "subscriptionType": "max", "authMethod": "claude.ai"})):
            with mock.patch.object(ClaudeCLIClient, "_run_cli", return_value={"is_error": False, "result": '{"ok": true}'}):
                ok, msg = c.test_connection()
        self.assertTrue(ok)
        self.assertIn("a@b.c", msg)

    def test_binary_missing_message(self):
        c = ClaudeCLIClient()
        with mock.patch.object(ClaudeCLIClient, "_resolve_binary", return_value=None):
            ok, msg = c.test_connection()
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())

    def test_connection_does_not_repopulate_cache_after_reset(self):
        # If reset_client() bumps the generation while the UI test is probing, the
        # test's result must not overwrite the hot-path cache.
        c = _client()
        cli_client._reset_auth_cache()

        def probe_then_reset(_self):
            cli_client._reset_auth_cache()  # generation changes mid-probe
            return True, {"email": "a@b.c", "subscriptionType": "max", "authMethod": "claude.ai"}

        with mock.patch.object(ClaudeCLIClient, "_probe_auth", autospec=True, side_effect=probe_then_reset):
            with mock.patch.object(ClaudeCLIClient, "_run_cli", return_value={"is_error": False, "result": "{}"}):
                ok, _msg = c.test_connection()
        self.assertTrue(ok)  # the test itself still reports success to the user
        self.assertFalse(cli_client._auth_cache["has_value"])  # but it did not repopulate the reset cache


class TestModelAndUsageResolution(unittest.TestCase):
    def test_primary_model_id_alias_match(self):
        c = _client(model="sonnet")
        env = {"modelUsage": {"claude-haiku-4-5-20251001": {"outputTokens": 14}, "claude-sonnet-4-6": {"outputTokens": 8}}}
        self.assertEqual(c._primary_model_id(env), "claude-sonnet-4-6")

    def test_primary_model_id_exact_full_id(self):
        c = _client(model="claude-sonnet-4-6")
        env = {"modelUsage": {"claude-sonnet-4-6": {"outputTokens": 8}}}
        self.assertEqual(c._primary_model_id(env), "claude-sonnet-4-6")

    def test_primary_model_id_falls_back_to_self_model(self):
        c = _client(model="sonnet")
        self.assertEqual(c._primary_model_id({}), "sonnet")

    def test_extract_usage_none_when_no_tokens(self):
        c = _client()
        self.assertIsNone(c._extract_usage({"usage": {"input_tokens": 0, "output_tokens": 0}}))

    def test_cost_pricing_alias_falls_back_to_default(self):
        # An alias ("sonnet") is not a full model id, so pricing falls back to
        # DEFAULT_PRICING (no crash); the CLI's reported full sonnet id is priced.
        from sickchill.oldbeard.ai.cost_tracker import DEFAULT_PRICING, MODEL_PRICING

        self.assertIs(MODEL_PRICING.get("sonnet", DEFAULT_PRICING), DEFAULT_PRICING)
        self.assertIn("claude-sonnet-4-6", MODEL_PRICING)


class TestCacheKeySeparation(unittest.TestCase):
    def test_provider_changes_cache_hash(self):
        api = AnthropicClient(api_key="k", model="claude-sonnet-4-20250514")
        cli = _client(model="claude-sonnet-4-20250514")  # same model string on purpose
        h_api = api._generate_cache_hash("same prompt", None, None, 512)
        h_cli = cli._generate_cache_hash("same prompt", None, None, 512)
        self.assertNotEqual(h_api, h_cli)

    def test_same_provider_same_inputs_stable(self):
        cli = _client(model="sonnet")
        h1 = cli._generate_cache_hash("p", None, "sys", 256)
        h2 = cli._generate_cache_hash("p", None, "sys", 256)
        self.assertEqual(h1, h2)

    def test_system_prompt_changes_hash(self):
        cli = _client(model="sonnet")
        self.assertNotEqual(
            cli._generate_cache_hash("p", None, "sys-a", 256),
            cli._generate_cache_hash("p", None, "sys-b", 256),
        )


class TestProviderSelection(unittest.TestCase):
    def setUp(self):
        from sickchill.oldbeard import ai

        ai.reset_client()

    def tearDown(self):
        from sickchill.oldbeard import ai

        ai.reset_client()

    def _settings(self, **over):
        defaults = dict(
            AI_ENABLED=True,
            AI_PROVIDER="api",
            ANTHROPIC_API_KEY="sk-test",
            ANTHROPIC_MODEL="claude-sonnet-4-20250514",
            AI_REQUEST_TIMEOUT=30,
            AI_CLI_MODEL="sonnet",
            AI_CLI_PATH="",
        )
        defaults.update(over)
        return defaults

    def test_get_client_api_provider(self):
        from sickchill.oldbeard import ai

        with mock.patch.multiple("sickchill.settings", **self._settings(AI_PROVIDER="api")):
            client = ai.get_client()
        self.assertIsInstance(client, AnthropicClient)

    def test_get_client_cli_provider(self):
        from sickchill.oldbeard import ai

        with mock.patch.multiple("sickchill.settings", **self._settings(AI_PROVIDER="cli")):
            client = ai.get_client()
        self.assertIsInstance(client, ClaudeCLIClient)

    def test_get_client_none_when_disabled(self):
        from sickchill.oldbeard import ai

        with mock.patch.multiple("sickchill.settings", **self._settings(AI_ENABLED=False)):
            self.assertIsNone(ai.get_client())

    def test_get_client_cli_without_api_key(self):
        from sickchill.oldbeard import ai

        # CLI provider does not need an API key.
        with mock.patch.multiple("sickchill.settings", **self._settings(AI_PROVIDER="cli", ANTHROPIC_API_KEY=None)):
            self.assertIsInstance(ai.get_client(), ClaudeCLIClient)


if __name__ == "__main__":
    unittest.main()
