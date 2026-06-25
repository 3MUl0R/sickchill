"""
Claude Code CLI client for SickChill AI features.

Uses a locally installed, logged-in ``claude`` CLI (Claude Code) as the AI
backend instead of an Anthropic API key. This lets a server whose host is
signed in to an Anthropic subscription run the AI features with no API key.

- Detection / auth uses the cheap, non-billed ``claude auth status --json`` probe.
- Inference uses headless ``claude -p --output-format json`` with the prompt on
  stdin and the model's textual result handed to the shared JSON parser.

All calls are stateless one-shots (a fresh conversation per request), matching
the existing prompt-hash response cache. The child process is isolated: it runs
in a neutral working directory with tools/MCP disabled and a sanitized
environment so it always uses the logged-in session (never an inherited API key).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Tuple

from sickchill.oldbeard.ai.base_client import (
    AIConfigurationError,
    AIError,
    AIRateLimitError,
    AIResponseError,
    APIUsage,
    BaseAIClient,
)

logger = logging.getLogger(__name__)

# System prompt used when the caller does not supply one. It fully replaces
# Claude Code's default agent prompt (avoiding coding-agent framing) and keeps
# the model in pure JSON-API mode.
_JSON_SYSTEM_PROMPT = (
    "You are a backend JSON API for an automation system. Respond with ONLY a single valid JSON object. Do not include explanations, prose, or markdown fences."
)

# Environment variables that would push the CLI off the logged-in subscription
# (OAuth) onto an API key or a third-party provider. Stripped from the child env
# so the CLI provider always uses the interactive login, never an inherited key.
_STRIPPED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

# Shared auth-status cache (survives client resets so the hot path stays cheap).
AUTH_CACHE_TTL = 300.0  # seconds a successful probe stays "fresh"
AUTH_RETRY_MIN_INTERVAL = 30.0  # min seconds between background refresh attempts

_auth_lock = threading.Lock()
_auth_cache: Dict[str, Any] = {
    "checked_at": 0.0,  # when we last got a successful/known result
    "last_attempt": 0.0,  # when we last started a probe (success or fail)
    "logged_in": False,
    "has_value": False,  # have we ever obtained a real result?
    "info": {},
}
_auth_refresh_in_flight = False
# Bumped on every reset so an in-flight background probe started before the reset
# (e.g. for the old provider/path) cannot overwrite the cache afterwards.
_auth_generation = 0


def _reset_auth_cache() -> None:
    """Clear the shared auth cache (used by reset_client / tests)."""
    global _auth_refresh_in_flight, _auth_generation
    with _auth_lock:
        _auth_generation += 1
        _auth_cache.update(checked_at=0.0, last_attempt=0.0, logged_in=False, has_value=False, info={})
        _auth_refresh_in_flight = False


class ClaudeCLIClient(BaseAIClient):
    """AI provider backed by the local, logged-in Claude Code CLI."""

    PROVIDER = "cli"

    # Aliases offered in the UI dropdown; the CLI also accepts full model ids.
    SUPPORTED_MODELS = {
        "sonnet": "Claude Sonnet (recommended)",
        "opus": "Claude Opus (most capable)",
        "haiku": "Claude Haiku (faster, cheaper)",
    }

    DEFAULT_MODEL = "sonnet"
    DEFAULT_TIMEOUT = 60

    # Reasoning effort levels accepted by `claude --effort`. Higher = more internal thinking
    # (slower, more tokens). "low" is the default for SickChill's bounded JSON tasks.
    SUPPORTED_EFFORTS = {
        "low": "Low (fastest, recommended)",
        "medium": "Medium",
        "high": "High",
        "xhigh": "Extra high",
        "max": "Maximum (slowest)",
    }
    DEFAULT_EFFORT = "low"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        cli_path: str = "",
        effort: str = DEFAULT_EFFORT,
    ):
        """
        Args:
            model: Model alias ("sonnet"/"opus"/"haiku") or a full model id.
            timeout: Per-call subprocess timeout in seconds.
            cli_path: Optional explicit path to the ``claude`` binary.
            effort: Reasoning effort passed to ``--effort`` (one of SUPPORTED_EFFORTS);
                anything unrecognized falls back to DEFAULT_EFFORT.
        """
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout if timeout and timeout > 0 else self.DEFAULT_TIMEOUT
        self.cli_path = cli_path or ""
        self.effort = effort if effort in self.SUPPORTED_EFFORTS else self.DEFAULT_EFFORT
        self._binary: Optional[str] = None
        self._binary_resolved = False

    # ------------------------------------------------------------------ #
    # Binary / environment helpers
    # ------------------------------------------------------------------ #
    def _resolve_binary(self) -> Optional[str]:
        """Locate the claude binary (explicit path, then PATH). Cached per instance."""
        if self._binary_resolved:
            return self._binary

        resolved: Optional[str] = None
        if self.cli_path:
            if os.path.isfile(self.cli_path):
                resolved = self.cli_path
            else:
                resolved = shutil.which(self.cli_path)
        if not resolved:
            resolved = shutil.which("claude")
        if not resolved and sys.platform == "win32":
            for name in ("claude.exe", "claude.cmd", "claude.bat"):
                resolved = shutil.which(name)
                if resolved:
                    break

        self._binary = resolved
        self._binary_resolved = True
        return resolved

    @staticmethod
    def _child_env() -> Dict[str, str]:
        """A copy of the process env with API/provider-switch vars stripped.

        Profile/keychain vars (HOME, USERPROFILE, APPDATA, PATH, ...) are kept so
        the CLI can read the logged-in OAuth session.
        """
        env = os.environ.copy()
        for var in _STRIPPED_ENV_VARS:
            env.pop(var, None)
        return env

    @staticmethod
    def _neutral_cwd() -> str:
        """A neutral working directory so the CLI never adopts the SickChill repo."""
        return tempfile.gettempdir()

    @staticmethod
    def _creationflags() -> int:
        if sys.platform == "win32":
            return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return 0

    @staticmethod
    def _kill_tree(proc: "subprocess.Popen") -> None:
        """Kill the process and any children (handles .cmd/node shims)."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Auth detection (hot-path safe: never blocks once warmed)
    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        """Cheap, non-blocking readiness check (runs on every search/postprocess).

        Never spawns a subprocess synchronously: returns a cached/last-known value
        and triggers a single coalesced background refresh when stale.
        """
        if not self._resolve_binary():
            return False

        now = time.time()
        start_refresh = False
        generation = 0
        with _auth_lock:
            fresh = _auth_cache["has_value"] and (now - _auth_cache["checked_at"]) < AUTH_CACHE_TTL
            if fresh:
                return bool(_auth_cache["logged_in"])

            last_known = bool(_auth_cache["logged_in"]) if _auth_cache["has_value"] else False
            recently_attempted = (now - _auth_cache["last_attempt"]) < AUTH_RETRY_MIN_INTERVAL
            global _auth_refresh_in_flight
            if not _auth_refresh_in_flight and not recently_attempted:
                _auth_refresh_in_flight = True
                _auth_cache["last_attempt"] = now
                generation = _auth_generation
                start_refresh = True

        if start_refresh:
            threading.Thread(target=self._background_refresh, args=(generation,), name="ai-cli-auth", daemon=True).start()
        return last_known

    def _background_refresh(self, generation: int) -> None:
        """Run the auth probe off the hot path and update the shared cache.

        Writes are dropped if the cache generation changed while the probe ran
        (i.e. a reset_client / provider change happened), so a stale probe can
        never repopulate the cache for a configuration that no longer applies.
        """
        global _auth_refresh_in_flight
        try:
            logged_in, info = self._probe_auth()
            with _auth_lock:
                if generation == _auth_generation:  # never let a probe error flip a good state to bad
                    _auth_cache.update(checked_at=time.time(), logged_in=logged_in, has_value=True, info=info)
        except Exception as e:  # never let a probe error flip a good state to bad
            logger.debug(f"Claude CLI auth refresh failed: {e}")
        finally:
            with _auth_lock:
                if generation == _auth_generation:
                    _auth_refresh_in_flight = False

    def _probe_auth(self) -> Tuple[bool, Dict[str, Any]]:
        """Synchronously run ``claude auth status --json``. Returns (logged_in, info)."""
        binary = self._resolve_binary()
        if not binary:
            return False, {"error": "claude CLI not found on PATH"}
        try:
            returncode, out, err = self._spawn([binary, "auth", "status", "--json"])
        except AIError as e:
            return False, {"error": str(e)}

        try:
            info = json.loads(out)
        except json.JSONDecodeError:
            return False, {"error": "could not parse auth status output", "raw": (err or out or f"exit {returncode}")[:300]}
        if not isinstance(info, dict):
            return False, {"error": "unexpected auth status payload"}
        return bool(info.get("loggedIn")), info

    # ------------------------------------------------------------------ #
    # Connectivity test (UI button) — always a fresh synchronous probe
    # ------------------------------------------------------------------ #
    def test_connection(self) -> Tuple[bool, str]:
        """Force a fresh auth probe (+ a tiny round-trip) and report status."""
        binary = self._resolve_binary()
        if not binary:
            return False, "Claude Code CLI not found. Install it or set an explicit path in the field above."

        # Capture the cache generation before probing so a reset_client() during this
        # ad-hoc test cannot have its result overwrite the hot-path cache afterwards.
        with _auth_lock:
            generation = _auth_generation

        logged_in, info = self._probe_auth()
        now = time.time()
        with _auth_lock:
            if generation == _auth_generation:
                _auth_cache.update(checked_at=now, last_attempt=now, logged_in=logged_in, has_value=True, info=info)

        if not logged_in:
            detail = info.get("error", "")
            suffix = f" ({detail})" if detail else ""
            return False, f"Found CLI at {binary} but it is not logged in. Run 'claude login' on the server{suffix}."

        # A tiny real round-trip ("the couple of tests"): confirms end-to-end usability.
        try:
            envelope = self._run_cli(
                self._build_argv(binary, _JSON_SYSTEM_PROMPT),
                'Return ONLY this JSON object: {"ok": true}',
            )
            if envelope.get("is_error"):
                return False, f"Logged in but a test call failed: {self._envelope_error_text(envelope)}"
        except AIError as e:
            return False, f"Logged in as {info.get('email', '?')} but a test call failed: {e}"

        email = info.get("email", "unknown")
        sub = info.get("subscriptionType", "unknown")
        method = info.get("authMethod", "unknown")
        return True, f"Ready. Logged in as {email} ({sub} via {method}). Model: {self.model}."

    # ------------------------------------------------------------------ #
    # Inference
    # ------------------------------------------------------------------ #
    def _build_argv(self, binary: str, system_prompt: str) -> list:
        return [
            binary,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--system-prompt",
            system_prompt,
        ]

    def _make_request(
        self,
        prompt: str,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> Tuple[Dict[str, Any], Optional[APIUsage]]:
        """Run one headless CLI call and return (parsed JSON response, APIUsage)."""
        binary = self._resolve_binary()
        if not binary:
            raise AIConfigurationError("Claude Code CLI not found on PATH")

        # Compose the effective system prompt: always enforce JSON-only output,
        # appending any caller-supplied guidance. (max_tokens is not a CLI flag in
        # headless mode; our prompts already request short JSON.)
        if system_prompt:
            effective_system = f"{system_prompt}\n\n{_JSON_SYSTEM_PROMPT}"
        else:
            effective_system = _JSON_SYSTEM_PROMPT

        envelope = self._run_cli(self._build_argv(binary, effective_system), prompt)

        if envelope.get("is_error"):
            self._raise_for_text(self._envelope_error_text(envelope))

        result_text = envelope.get("result")
        if not result_text or not isinstance(result_text, str):
            raise AIResponseError("Claude CLI returned no result text")

        parsed = self._parse_json_response(result_text)
        return parsed, self._extract_usage(envelope)

    def _spawn(self, argv: list, input_bytes: bytes = b"") -> Tuple[int, str, str]:
        """Run argv with full isolation, a timeout, and process-tree kill on hang.

        Returns (returncode, stdout, stderr) with UTF-8 decoding. Raises
        AIConfigurationError if the process cannot be launched and a transient
        AIError if it times out (after killing the whole process tree).
        """
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._neutral_cwd(),
                env=self._child_env(),
                creationflags=self._creationflags(),
                start_new_session=(sys.platform != "win32"),
            )
        except (OSError, ValueError) as e:
            raise AIConfigurationError(f"Failed to launch Claude CLI: {e}")

        try:
            stdout_b, stderr_b = proc.communicate(input=input_bytes, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            try:
                proc.communicate(timeout=10)  # reap the killed process
            except Exception:
                pass
            raise AIError(f"Transient error: Claude CLI timed out after {self.timeout}s")

        stdout = stdout_b.decode("utf-8", "replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", "replace") if stderr_b else ""
        return proc.returncode, stdout, stderr

    def _run_cli(self, argv: list, prompt: str) -> Dict[str, Any]:
        """Spawn the CLI, feed the prompt on stdin, and return the parsed envelope."""
        returncode, stdout, stderr = self._spawn(argv, prompt.encode("utf-8"))

        envelope: Optional[Any] = None
        if stdout.strip():
            try:
                envelope = json.loads(stdout)
            except json.JSONDecodeError:
                envelope = None

        if isinstance(envelope, dict):
            # A nonzero exit that did not set is_error is a real failure that happened
            # to emit JSON: trust the exit code and map it rather than reporting success.
            if returncode != 0 and not envelope.get("is_error"):
                self._raise_for_text(stderr or json.dumps(envelope)[:300] or f"Claude CLI exited with code {returncode}")
            return envelope

        # No parseable envelope: map nonzero exit / stderr to an error class.
        self._raise_for_text(stderr or stdout or f"Claude CLI exited with code {returncode}")

    # ------------------------------------------------------------------ #
    # Error mapping & usage extraction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _envelope_error_text(envelope: Dict[str, Any]) -> str:
        for key in ("result", "error", "subtype"):
            val = envelope.get(key)
            if val:
                return str(val)
        return "unknown CLI error"

    def _raise_for_text(self, text: str) -> None:
        """Map CLI failure text to the appropriate AIError subclass and raise."""
        lowered = (text or "").lower()
        if any(k in lowered for k in ("not logged in", "log in", "login", "unauthenticated", "authentication", "credit balance", "invalid api key")):
            raise AIConfigurationError(f"Claude CLI auth error: {text}")
        if "rate" in lowered and "limit" in lowered:
            raise AIRateLimitError(f"Claude CLI rate limited: {text}")
        if self._is_transient_error(Exception(lowered)):
            raise AIError(f"Transient error: {text}")
        # Unknown failure: treat as retryable (transient) so a one-off hiccup self-heals.
        raise AIError(f"Claude CLI request failed: {text}")

    def _extract_usage(self, envelope: Dict[str, Any]) -> Optional[APIUsage]:
        """Build APIUsage from the envelope (top-level usage = primary model turn)."""
        usage = envelope.get("usage") or {}
        try:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            return None
        if not (input_tokens or output_tokens):
            return None
        return APIUsage(input_tokens=input_tokens, output_tokens=output_tokens, model=self._primary_model_id(envelope))

    def _primary_model_id(self, envelope: Dict[str, Any]) -> str:
        """Resolve the full model id of the main turn for accurate cost labeling."""
        model_usage = envelope.get("modelUsage") or {}
        if not isinstance(model_usage, dict) or not model_usage:
            return self.model

        if self.model in model_usage:
            return self.model

        alias = (self.model or "").lower()
        if alias:
            for key in model_usage:
                if alias in key.lower():
                    return key

        def _total(key: str) -> int:
            u = model_usage.get(key) or {}
            return int(
                (u.get("inputTokens") or 0) + (u.get("outputTokens") or 0) + (u.get("cacheReadInputTokens") or 0) + (u.get("cacheCreationInputTokens") or 0)
            )

        return max(model_usage, key=_total)
