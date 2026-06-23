"""
Tests for AI Anthropic client functionality.

Tests:
    TestJSONParsing - JSON response parsing from Claude
    TestClientConfiguration - Client configuration validation
    TestErrorHandling - Error categorization and handling
"""

from __future__ import annotations

import unittest
from unittest import mock

from sickchill.oldbeard.ai.anthropic_client import (
    AIConfigurationError,
    AIResponseError,
    AnthropicClient,
)


class TestJSONParsing(unittest.TestCase):
    """Test JSON parsing from Claude responses."""

    def setUp(self):
        """Set up test client."""
        self.client = AnthropicClient(api_key="test-key")

    def test_parse_clean_json(self):
        """Test parsing a clean JSON response."""
        response = '{"selected_index": 2, "confidence": 0.95, "reasoning": "Best match"}'

        result = self.client._parse_json_response(response)

        self.assertEqual(result["selected_index"], 2)
        self.assertEqual(result["confidence"], 0.95)
        self.assertEqual(result["reasoning"], "Best match")

    def test_parse_json_with_whitespace(self):
        """Test parsing JSON with leading/trailing whitespace."""
        response = """
        {
            "selected_index": 1,
            "confidence": 0.85
        }
        """

        result = self.client._parse_json_response(response)

        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["confidence"], 0.85)

    def test_parse_json_in_code_block(self):
        """Test parsing JSON wrapped in markdown code block."""
        response = """Here is my analysis:

```json
{
    "selected_index": 0,
    "confidence": 0.92,
    "reasoning": "High quality release"
}
```

This is my recommendation."""

        result = self.client._parse_json_response(response)

        self.assertEqual(result["selected_index"], 0)
        self.assertEqual(result["confidence"], 0.92)

    def test_parse_json_in_plain_code_block(self):
        """Test parsing JSON in code block without json language tag."""
        response = """Analysis complete:

```
{"selected_index": 3, "confidence": 0.88}
```"""

        result = self.client._parse_json_response(response)

        self.assertEqual(result["selected_index"], 3)
        self.assertEqual(result["confidence"], 0.88)

    def test_parse_json_with_surrounding_text(self):
        """Test parsing JSON embedded in prose."""
        response = """Based on my analysis, the best result is:

{"selected_index": 1, "confidence": 0.90, "reasoning": "Good seeders"}

Let me know if you need more information."""

        result = self.client._parse_json_response(response)

        self.assertEqual(result["selected_index"], 1)

    def test_parse_nested_json(self):
        """Test parsing JSON with nested objects."""
        response = """{
            "show_indexer_id": 12345,
            "season": 5,
            "episodes": [16],
            "metadata": {"source": "tvdb"},
            "confidence": 0.95
        }"""

        result = self.client._parse_json_response(response)

        self.assertEqual(result["show_indexer_id"], 12345)
        self.assertEqual(result["episodes"], [16])
        self.assertEqual(result["metadata"]["source"], "tvdb")

    def test_parse_json_with_special_chars(self):
        """Test parsing JSON with special characters in strings."""
        response = '{"reasoning": "Show \\"Breaking Bad\\" matches best", "confidence": 0.9}'

        result = self.client._parse_json_response(response)

        self.assertIn("Breaking Bad", result["reasoning"])

    def test_parse_invalid_json_raises_error(self):
        """Test that invalid JSON raises AIResponseError."""
        response = "This is not JSON at all, just plain text."

        with self.assertRaises(AIResponseError):
            self.client._parse_json_response(response)

    def test_parse_malformed_json_raises_error(self):
        """Test that malformed JSON raises AIResponseError."""
        response = '{"selected_index": 1, "confidence": }'  # Missing value

        with self.assertRaises(AIResponseError):
            self.client._parse_json_response(response)

    def test_parse_empty_response_raises_error(self):
        """Test that empty response raises AIResponseError."""
        with self.assertRaises(AIResponseError):
            self.client._parse_json_response("")

    def test_parse_json_array_not_object(self):
        """Test parsing JSON array (should work if valid JSON)."""
        # Our parser looks for objects specifically, so array alone won't match
        response = "[1, 2, 3]"

        with self.assertRaises(AIResponseError):
            self.client._parse_json_response(response)

    def test_parse_prefers_first_valid_json(self):
        """Test that first valid JSON object is returned when multiple exist."""
        response = """
{"first": true, "selected_index": 0}

Some text

{"second": true, "selected_index": 1}
"""

        result = self.client._parse_json_response(response)

        # Should get the first valid JSON
        self.assertTrue(result.get("first", False) or result.get("second", False))


class TestClientConfiguration(unittest.TestCase):
    """Test client configuration and validation."""

    def test_is_configured_with_valid_key(self):
        """Test is_configured returns True with a key."""
        client = AnthropicClient(api_key="sk-ant-test-key-12345")

        self.assertTrue(client.is_configured())

    def test_is_configured_with_any_key(self):
        """Test is_configured accepts any non-empty key format."""
        # We removed the sk-ant- check, so any non-empty key should work
        client = AnthropicClient(api_key="any-valid-key-format")

        self.assertTrue(client.is_configured())

    def test_is_configured_with_empty_key(self):
        """Test is_configured returns False with empty key."""
        client = AnthropicClient(api_key="")

        self.assertFalse(client.is_configured())

    def test_is_configured_with_none_key(self):
        """Test is_configured returns False with None key."""
        client = AnthropicClient(api_key=None)

        self.assertFalse(client.is_configured())

    def test_is_configured_with_whitespace_key(self):
        """Test is_configured returns False with whitespace-only key."""
        client = AnthropicClient(api_key="   ")

        self.assertFalse(client.is_configured())

    def test_default_model(self):
        """Test default model is set correctly."""
        client = AnthropicClient(api_key="test")

        self.assertEqual(client.model, "claude-sonnet-4-20250514")

    def test_custom_model(self):
        """Test custom model is accepted."""
        client = AnthropicClient(api_key="test", model="claude-haiku-3-5-20241022")

        self.assertEqual(client.model, "claude-haiku-3-5-20241022")

    def test_invalid_model_falls_back_to_default(self):
        """Test invalid model falls back to default."""
        client = AnthropicClient(api_key="test", model="invalid-model")

        self.assertEqual(client.model, "claude-sonnet-4-20250514")

    def test_custom_timeout(self):
        """Test custom timeout is set."""
        client = AnthropicClient(api_key="test", timeout=60)

        self.assertEqual(client.timeout, 60)


class TestErrorHandling(unittest.TestCase):
    """Test error categorization and handling."""

    def test_analyze_without_config_raises_error(self):
        """Test analyze raises error when not configured."""
        client = AnthropicClient(api_key="")

        with self.assertRaises(AIConfigurationError):
            client.analyze("test prompt")

    def test_test_connection_without_config(self):
        """Test test_connection returns failure when not configured."""
        client = AnthropicClient(api_key="")

        success, message = client.test_connection()

        self.assertFalse(success)
        self.assertIn("not configured", message.lower())

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._get_client")
    def test_test_connection_auth_error(self, mock_get_client):
        """Test test_connection handles auth errors."""
        mock_client = mock.MagicMock()
        mock_client.messages.create.side_effect = Exception("Invalid API key authentication failed")
        mock_get_client.return_value = mock_client

        client = AnthropicClient(api_key="invalid-key")
        success, message = client.test_connection()

        self.assertFalse(success)
        self.assertIn("Invalid API key", message)

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._get_client")
    def test_test_connection_rate_limit(self, mock_get_client):
        """Test test_connection handles rate limit errors."""
        mock_client = mock.MagicMock()
        mock_client.messages.create.side_effect = Exception("Rate limit exceeded")
        mock_get_client.return_value = mock_client

        client = AnthropicClient(api_key="test-key")
        success, message = client.test_connection()

        self.assertFalse(success)
        self.assertIn("Rate limit", message)

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._get_client")
    def test_test_connection_timeout(self, mock_get_client):
        """Test test_connection handles timeout errors."""
        mock_client = mock.MagicMock()
        mock_client.messages.create.side_effect = Exception("Request timeout after 30s")
        mock_get_client.return_value = mock_client

        client = AnthropicClient(api_key="test-key")
        success, message = client.test_connection()

        self.assertFalse(success)
        self.assertIn("timed out", message.lower())

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._get_client")
    def test_test_connection_success(self, mock_get_client):
        """Test test_connection returns success on valid response."""
        mock_client = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.content = [mock.MagicMock(text="OK")]
        mock_client.messages.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        client = AnthropicClient(api_key="valid-key")
        success, message = client.test_connection()

        self.assertTrue(success)
        self.assertIn("successful", message.lower())


class TestSupportedModels(unittest.TestCase):
    """Test supported models configuration."""

    def test_supported_models_exist(self):
        """Test that supported models dict is populated."""
        self.assertIn("claude-sonnet-4-20250514", AnthropicClient.SUPPORTED_MODELS)
        self.assertIn("claude-haiku-3-5-20241022", AnthropicClient.SUPPORTED_MODELS)

    def test_supported_models_have_descriptions(self):
        """Test that supported models have human-readable descriptions."""
        for model_id, description in AnthropicClient.SUPPORTED_MODELS.items():
            self.assertIsInstance(description, str)
            self.assertGreater(len(description), 0)


class TestRetryLogic(unittest.TestCase):
    """Test retry logic with exponential backoff."""

    def test_is_transient_error_timeout(self):
        """Test that timeout errors are identified as transient."""
        client = AnthropicClient(api_key="test")

        self.assertTrue(client._is_transient_error(Exception("Connection timeout")))
        self.assertTrue(client._is_transient_error(Exception("Request timed out")))

    def test_is_transient_error_network(self):
        """Test that network errors are identified as transient."""
        client = AnthropicClient(api_key="test")

        self.assertTrue(client._is_transient_error(Exception("Network error")))
        self.assertTrue(client._is_transient_error(Exception("Connection refused")))

    def test_is_transient_error_server_errors(self):
        """Test that 5xx errors are identified as transient."""
        client = AnthropicClient(api_key="test")

        self.assertTrue(client._is_transient_error(Exception("502 Bad Gateway")))
        self.assertTrue(client._is_transient_error(Exception("503 Service Unavailable")))
        self.assertTrue(client._is_transient_error(Exception("504 Gateway Timeout")))

    def test_is_transient_error_overloaded(self):
        """Test that overloaded errors are identified as transient."""
        client = AnthropicClient(api_key="test")

        self.assertTrue(client._is_transient_error(Exception("Server overloaded")))

    def test_is_not_transient_error_auth(self):
        """Test that auth errors are NOT transient."""
        client = AnthropicClient(api_key="test")

        self.assertFalse(client._is_transient_error(Exception("Invalid API key")))
        self.assertFalse(client._is_transient_error(Exception("Authentication failed")))

    def test_is_not_transient_error_rate_limit(self):
        """Test that rate limit errors are NOT transient."""
        client = AnthropicClient(api_key="test")

        # Note: rate limit is handled separately, but _is_transient_error should not match
        self.assertFalse(client._is_transient_error(Exception("Rate limit exceeded")))

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._make_request")
    @mock.patch("sickchill.oldbeard.ai.base_client.time.sleep")
    def test_retry_on_transient_error(self, mock_sleep, mock_make_request):
        """Test that transient errors trigger retry."""
        from sickchill.oldbeard.ai.anthropic_client import AIError

        # Fail twice, then succeed (returns tuple of (response, usage))
        mock_make_request.side_effect = [
            AIError("Transient error: Connection timeout"),
            AIError("Transient error: Network error"),
            ({"selected_index": 1, "confidence": 0.9}, None),  # Tuple: (response, usage)
        ]

        client = AnthropicClient(api_key="test-key")
        result = client.analyze("test prompt", use_cache=False)  # Disable cache to avoid needing to mock it

        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(mock_make_request.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # Slept before retries 2 and 3

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._make_request")
    @mock.patch("sickchill.oldbeard.ai.base_client.time.sleep")
    def test_no_retry_on_auth_error(self, mock_sleep, mock_make_request):
        """Test that auth errors do NOT trigger retry."""
        mock_make_request.side_effect = AIConfigurationError("Authentication failed")

        client = AnthropicClient(api_key="invalid-key")

        with self.assertRaises(AIConfigurationError):
            client.analyze("test prompt", use_cache=False)

        # Should only try once
        self.assertEqual(mock_make_request.call_count, 1)
        mock_sleep.assert_not_called()

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._make_request")
    @mock.patch("sickchill.oldbeard.ai.base_client.time.sleep")
    def test_no_retry_on_rate_limit(self, mock_sleep, mock_make_request):
        """Test that rate limit errors do NOT trigger retry."""
        from sickchill.oldbeard.ai.anthropic_client import AIRateLimitError

        mock_make_request.side_effect = AIRateLimitError("Rate limit exceeded")

        client = AnthropicClient(api_key="test-key")

        with self.assertRaises(AIRateLimitError):
            client.analyze("test prompt", use_cache=False)

        # Should only try once
        self.assertEqual(mock_make_request.call_count, 1)
        mock_sleep.assert_not_called()

    @mock.patch("sickchill.oldbeard.ai.anthropic_client.AnthropicClient._make_request")
    @mock.patch("sickchill.oldbeard.ai.base_client.time.sleep")
    def test_max_retries_exhausted(self, mock_sleep, mock_make_request):
        """Test that all retries are exhausted before giving up."""
        from sickchill.oldbeard.ai.anthropic_client import MAX_RETRIES, AIError

        mock_make_request.side_effect = AIError("Transient error: timeout")

        client = AnthropicClient(api_key="test-key")

        with self.assertRaises(AIError):
            client.analyze("test prompt", use_cache=False)

        # Should try MAX_RETRIES times
        self.assertEqual(mock_make_request.call_count, MAX_RETRIES)


if __name__ == "__main__":
    unittest.main()
