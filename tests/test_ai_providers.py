import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import ai_providers
from ai_providers import (
    GeminiProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderError,
    clear_api_key,
    get_api_key,
    get_model,
    get_provider,
    get_provider_name,
    has_api_key,
    list_providers,
    load_config,
    save_config,
    set_api_key,
    set_model,
    set_provider_name,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("no json here")
        return self._payload


class TempDataDirTestCase(unittest.TestCase):
    """Point PyOS at a throwaway data directory for every test."""

    def setUp(self):
        self._previous = os.environ.get("PY_OS_DATA_DIR")
        self.tmpdir = tempfile.mkdtemp()
        os.environ["PY_OS_DATA_DIR"] = self.tmpdir
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._previous is None:
            os.environ.pop("PY_OS_DATA_DIR", None)
        else:
            os.environ["PY_OS_DATA_DIR"] = self._previous


class ConfigTests(TempDataDirTestCase):
    def test_missing_file_returns_defaults(self):
        config = load_config()
        self.assertEqual(config["provider"], "ollama")
        self.assertEqual(config["keys"], {})
        self.assertEqual(config["models"]["ollama"], "llama3")

    def test_round_trip(self):
        set_provider_name("gemini")
        set_api_key("gemini", "AIza-test-key")
        set_model("gemini", "gemini-2.5-flash")

        self.assertEqual(get_provider_name(), "gemini")
        self.assertEqual(get_api_key("gemini"), "AIza-test-key")
        self.assertTrue(has_api_key("gemini"))
        self.assertEqual(get_model("gemini"), "gemini-2.5-flash")

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(ai_providers.get_config_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        self.assertEqual(get_provider_name(), "ollama")
        self.assertEqual(get_api_key("gemini"), "")

    def test_unknown_provider_name_is_ignored(self):
        with open(ai_providers.get_config_path(), "w", encoding="utf-8") as handle:
            json.dump({"provider": "some-other-service"}, handle)
        self.assertEqual(get_provider_name(), "ollama")

    def test_clearing_one_key_keeps_the_other(self):
        set_api_key("gemini", "gemini-key")
        set_api_key("openrouter", "openrouter-key")
        clear_api_key("gemini")

        self.assertEqual(get_api_key("gemini"), "")
        self.assertEqual(get_api_key("openrouter"), "openrouter-key")

    def test_keys_are_stored_as_plain_readable_json(self):
        set_api_key("openrouter", "sk-or-plain")
        with open(ai_providers.get_config_path(), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["keys"]["openrouter"], "sk-or-plain")

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_saved_file_is_owner_only(self):
        set_api_key("gemini", "key")
        mode = stat.S_IMODE(os.stat(ai_providers.get_config_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_failed_save_keeps_previous_keys_and_cleans_up(self):
        set_api_key("gemini", "kept-key")
        config_path = ai_providers.get_config_path()

        with mock.patch("ai_providers.json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                set_api_key("gemini", "replacement-key")

        self.assertEqual(get_api_key("gemini"), "kept-key")
        leftovers = [n for n in os.listdir(self.tmpdir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_save_config_creates_missing_directory(self):
        nested = os.path.join(self.tmpdir, "deep", "nested", "ai_config.json")
        config = {"provider": "ollama", "keys": {}, "models": {}}
        save_config(config, nested)
        self.assertTrue(os.path.exists(nested))


class OllamaProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = OllamaProvider()

    def test_requires_no_api_key(self):
        self.assertFalse(self.provider.requires_api_key)
        self.assertEqual(self.provider.status_text(), "no API key needed")

    @mock.patch("ai_providers.requests.get")
    def test_list_models(self, get):
        get.return_value = FakeResponse(payload={"models": [{"name": "llama3"}, {"name": "mistral"}]})
        self.assertEqual(self.provider.list_models(), ["llama3", "mistral"])
        self.assertEqual(get.call_args[0][0], "http://localhost:11434/api/tags")

    @mock.patch("ai_providers.requests.get")
    def test_list_models_offline(self, get):
        get.side_effect = requests.exceptions.ConnectionError()
        with self.assertRaises(ProviderError) as ctx:
            self.provider.list_models()
        self.assertEqual(ctx.exception.kind, "offline")
        self.assertIn("Ollama is running", ctx.exception.message)

    @mock.patch("ai_providers.requests.get")
    def test_list_models_without_installed_models(self, get):
        get.return_value = FakeResponse(payload={"models": []})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.list_models()
        self.assertEqual(ctx.exception.kind, "http")

    @mock.patch("ai_providers.requests.post")
    def test_ask_returns_response_text(self, post):
        post.return_value = FakeResponse(payload={"response": "Hello there."})
        self.assertEqual(self.provider.ask("hi", "llama3"), "Hello there.")

        url = post.call_args[0][0]
        payload = post.call_args[1]["json"]
        self.assertEqual(url, "http://localhost:11434/api/generate")
        self.assertEqual(payload, {"model": "llama3", "prompt": "hi", "stream": False})

    @mock.patch("ai_providers.requests.post")
    def test_ask_connection_error(self, post):
        post.side_effect = requests.exceptions.ConnectionError()
        with self.assertRaises(ProviderError) as ctx:
            self.provider.ask("hi", "llama3")
        self.assertEqual(ctx.exception.kind, "offline")
        self.assertIn("Connection error", ctx.exception.message)

    @mock.patch("ai_providers.requests.post")
    def test_ask_http_error(self, post):
        post.return_value = FakeResponse(status_code=500)
        with self.assertRaises(ProviderError) as ctx:
            self.provider.ask("hi", "llama3")
        self.assertEqual(ctx.exception.kind, "http")


class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = GeminiProvider("test-key")

    def test_requires_an_api_key(self):
        self.assertTrue(self.provider.requires_api_key)
        self.assertEqual(GeminiProvider().status_text(), "API key required")
        self.assertEqual(self.provider.status_text(), "API key saved")

    def test_missing_key_is_reported_before_any_request(self):
        with mock.patch("ai_providers.requests.get") as get:
            with self.assertRaises(ProviderError) as ctx:
                GeminiProvider().list_models()
        self.assertEqual(ctx.exception.kind, "no_key")
        get.assert_not_called()

    @mock.patch("ai_providers.requests.get")
    def test_list_models_filters_and_strips_prefix(self, get):
        get.return_value = FakeResponse(payload={"models": [
            {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.0-flash-embed", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        ]})
        self.assertEqual(
            self.provider.list_models(), ["gemini-2.0-flash", "gemini-2.5-pro"]
        )
        self.assertEqual(get.call_args[1]["params"]["key"], "test-key")

    @mock.patch("ai_providers.requests.get")
    def test_rejected_key(self, get):
        get.return_value = FakeResponse(status_code=403, payload={})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.list_models()
        self.assertEqual(ctx.exception.kind, "bad_key")

    @mock.patch("ai_providers.requests.get")
    def test_offline(self, get):
        get.side_effect = requests.exceptions.ConnectionError()
        with self.assertRaises(ProviderError) as ctx:
            self.provider.list_models()
        self.assertEqual(ctx.exception.kind, "offline")

    @mock.patch("ai_providers.requests.post")
    def test_ask_parses_candidate_text(self, post):
        post.return_value = FakeResponse(payload={
            "candidates": [{"content": {"parts": [{"text": "Part one. "}, {"text": "Part two."}]}}]
        })
        self.assertEqual(self.provider.ask("hello", "gemini-2.0-flash"), "Part one. Part two.")
        self.assertIn("/models/gemini-2.0-flash:generateContent", post.call_args[0][0])
        self.assertEqual(
            post.call_args[1]["json"], {"contents": [{"parts": [{"text": "hello"}]}]}
        )

    @mock.patch("ai_providers.requests.post")
    def test_ask_strips_model_prefix(self, post):
        post.return_value = FakeResponse(payload={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
        })
        self.provider.ask("hello", "models/gemini-2.0-flash")
        self.assertIn("/models/gemini-2.0-flash:generateContent", post.call_args[0][0])

    @mock.patch("ai_providers.requests.post")
    def test_ask_safety_block_is_explained(self, post):
        post.return_value = FakeResponse(payload={
            "candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]
        })
        self.assertIn("safety", self.provider.ask("hello", "gemini-2.0-flash").lower())

    @mock.patch("ai_providers.requests.post")
    def test_ask_unknown_model(self, post):
        post.return_value = FakeResponse(status_code=404, payload={})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.ask("hello", "gemini-nope")
        self.assertEqual(ctx.exception.kind, "http")
        self.assertIn("model", ctx.exception.message)

    @mock.patch("ai_providers.requests.get")
    def test_verify_key_accepts_valid_key(self, get):
        get.return_value = FakeResponse(payload={"models": [
            {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]}
        ]})
        self.assertTrue(self.provider.verify_key("another-key"))
        self.assertEqual(get.call_args[1]["params"]["key"], "another-key")
        # The provider keeps its original key after a check.
        self.assertEqual(self.provider.api_key, "test-key")

    @mock.patch("ai_providers.requests.get")
    def test_verify_key_rejects_bad_key(self, get):
        get.return_value = FakeResponse(status_code=400, payload={})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.verify_key("wrong")
        self.assertEqual(ctx.exception.kind, "bad_key")

    def test_verify_key_requires_input(self):
        with self.assertRaises(ProviderError) as ctx:
            GeminiProvider().verify_key("")
        self.assertEqual(ctx.exception.kind, "no_key")


class OpenRouterProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = OpenRouterProvider("sk-or-test")

    def test_requires_an_api_key(self):
        self.assertTrue(self.provider.requires_api_key)

    @mock.patch("ai_providers.requests.get")
    def test_list_models_is_sorted(self, get):
        get.return_value = FakeResponse(payload={"data": [
            {"id": "z-ai/model"}, {"id": "anthropic/claude"}, {"id": "anthropic/claude"},
        ]})
        self.assertEqual(self.provider.list_models(), ["anthropic/claude", "z-ai/model"])
        self.assertEqual(get.call_args[0][0], "https://openrouter.ai/api/v1/models")

    @mock.patch("ai_providers.requests.get")
    def test_list_models_without_key_still_works(self, get):
        get.return_value = FakeResponse(payload={"data": [{"id": "openai/gpt-4o-mini"}]})
        provider = OpenRouterProvider()
        self.assertEqual(provider.list_models(), ["openai/gpt-4o-mini"])
        self.assertIsNone(get.call_args[1]["headers"])

    @mock.patch("ai_providers.requests.post")
    def test_ask_sends_key_and_parses_reply(self, post):
        post.return_value = FakeResponse(payload={
            "choices": [{"message": {"content": "Hi from OpenRouter."}}]
        })
        self.assertEqual(self.provider.ask("hi", "openai/gpt-4o-mini"), "Hi from OpenRouter.")

        headers = post.call_args[1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-or-test")
        payload = post.call_args[1]["json"]
        self.assertEqual(payload["model"], "openai/gpt-4o-mini")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])

    def test_ask_without_key(self):
        with self.assertRaises(ProviderError) as ctx:
            OpenRouterProvider().ask("hi", "openai/gpt-4o-mini")
        self.assertEqual(ctx.exception.kind, "no_key")

    @mock.patch("ai_providers.requests.post")
    def test_rejected_key_on_ask(self, post):
        post.return_value = FakeResponse(status_code=401, payload={})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.ask("hi", "openai/gpt-4o-mini")
        self.assertEqual(ctx.exception.kind, "bad_key")

    @mock.patch("ai_providers.requests.post")
    def test_ask_offline(self, post):
        post.side_effect = requests.exceptions.ConnectionError()
        with self.assertRaises(ProviderError) as ctx:
            self.provider.ask("hi", "openai/gpt-4o-mini")
        self.assertEqual(ctx.exception.kind, "offline")

    @mock.patch("ai_providers.requests.post")
    def test_ask_empty_reply(self, post):
        post.return_value = FakeResponse(payload={"choices": []})
        self.assertIn("empty", self.provider.ask("hi", "openai/gpt-4o-mini").lower())

    @mock.patch("ai_providers.requests.get")
    def test_verify_key_uses_key_endpoint(self, get):
        get.return_value = FakeResponse(payload={"data": {"label": "test"}})
        self.assertTrue(self.provider.verify_key("sk-or-new"))
        self.assertEqual(get.call_args[0][0], "https://openrouter.ai/api/v1/key")
        self.assertEqual(get.call_args[1]["headers"]["Authorization"], "Bearer sk-or-new")

    @mock.patch("ai_providers.requests.get")
    def test_verify_key_rejects_bad_key(self, get):
        get.return_value = FakeResponse(status_code=401, payload={})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.verify_key("sk-or-wrong")
        self.assertEqual(ctx.exception.kind, "bad_key")


class SystemInstructionTests(unittest.TestCase):
    """The PyOS reference travels in each provider's own kind of instruction."""

    SYSTEM = "You are the AI Assistant built into PyOS."

    @mock.patch("ai_providers.requests.post")
    def test_ollama_sends_it_in_the_system_field(self, post):
        post.return_value = FakeResponse(payload={"response": "ok"})
        OllamaProvider().ask("hi", "llama3", system=self.SYSTEM)

        payload = post.call_args[1]["json"]
        self.assertEqual(payload["system"], self.SYSTEM)
        self.assertEqual(payload["prompt"], "hi")
        self.assertFalse(payload["stream"])

    @mock.patch("ai_providers.requests.post")
    def test_gemini_sends_it_as_a_system_instruction(self, post):
        post.return_value = FakeResponse(payload={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
        })
        provider = GeminiProvider("test-key")
        provider.ask("hi", "gemini-2.0-flash", system=self.SYSTEM)

        payload = post.call_args[1]["json"]
        self.assertEqual(payload["systemInstruction"], {"parts": [{"text": self.SYSTEM}]})
        # Gemini rejects a system role among the contents, so it stays out of them.
        self.assertEqual(payload["contents"], [{"parts": [{"text": "hi"}]}])
        self.assertNotIn("system", payload)

    @mock.patch("ai_providers.requests.post")
    def test_openrouter_sends_it_as_the_first_message(self, post):
        post.return_value = FakeResponse(payload={
            "choices": [{"message": {"content": "ok"}}]
        })
        OpenRouterProvider("sk-or-test").ask(
            "hi", "openai/gpt-4o-mini", system=self.SYSTEM
        )

        messages = post.call_args[1]["json"]["messages"]
        self.assertEqual(messages, [
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": "hi"},
        ])

    @mock.patch("ai_providers.requests.post")
    def test_ollama_payload_is_unchanged_without_instructions(self, post):
        post.return_value = FakeResponse(payload={"response": "ok"})
        OllamaProvider().ask("hi", "llama3")

        self.assertEqual(
            post.call_args[1]["json"],
            {"model": "llama3", "prompt": "hi", "stream": False},
        )

    @mock.patch("ai_providers.requests.post")
    def test_empty_instructions_are_left_out(self, post):
        post.return_value = FakeResponse(payload={"response": "ok"})
        OllamaProvider().ask("hi", "llama3", system="")

        self.assertNotIn("system", post.call_args[1]["json"])

    @mock.patch("ai_providers.requests.post")
    def test_gemini_payload_is_unchanged_without_instructions(self, post):
        post.return_value = FakeResponse(payload={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
        })
        GeminiProvider("test-key").ask("hi", "gemini-2.0-flash")

        payload = post.call_args[1]["json"]
        self.assertNotIn("systemInstruction", payload)
        self.assertEqual(payload, {"contents": [{"parts": [{"text": "hi"}]}]})

    @mock.patch("ai_providers.requests.post")
    def test_openrouter_payload_is_unchanged_without_instructions(self, post):
        post.return_value = FakeResponse(payload={
            "choices": [{"message": {"content": "ok"}}]
        })
        OpenRouterProvider("sk-or-test").ask("hi", "openai/gpt-4o-mini")

        self.assertEqual(
            post.call_args[1]["json"]["messages"],
            [{"role": "user", "content": "hi"}],
        )

    def test_every_provider_carries_a_knowledge_budget(self):
        for provider_class in (OllamaProvider, GeminiProvider, OpenRouterProvider):
            self.assertIsInstance(provider_class.system_char_limit, int)
            self.assertGreater(provider_class.system_char_limit, 0)


class ProviderRegistryTests(TempDataDirTestCase):
    def test_picker_order(self):
        keys = [provider.key for provider in list_providers()]
        self.assertEqual(keys, ["ollama", "gemini", "openrouter"])

    def test_get_provider_returns_none_for_unknown_name(self):
        self.assertIsNone(get_provider("mystery-service"))

    def test_get_provider_uses_stored_key(self):
        set_api_key("gemini", "stored-key")
        provider = get_provider("gemini")
        self.assertEqual(provider.api_key, "stored-key")

    def test_get_provider_accepts_explicit_key(self):
        provider = get_provider("openrouter", "explicit-key")
        self.assertEqual(provider.api_key, "explicit-key")

    def test_status_labels_for_the_picker(self):
        providers = {p.key: p for p in list_providers()}
        self.assertEqual(providers["ollama"].status_text(), "no API key needed")
        self.assertEqual(providers["gemini"].status_text(), "API key required")

        set_api_key("gemini", "stored-key")
        providers = {p.key: p for p in list_providers()}
        self.assertEqual(providers["gemini"].status_text(), "API key saved")


if __name__ == "__main__":
    unittest.main()
