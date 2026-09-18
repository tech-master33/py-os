"""Tests for the streaming side of the AI providers.

The assistant speaks from these events, so what matters here is that a provider
reports only what it can actually observe: reasoning when reasoning arrives,
answer text when text arrives, and a search or a page visit only when the
provider itself says one happened. Everything is driven from canned response
bodies, so these tests never touch the network.
"""

import json
import os
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import netguard
import ai_providers
from ai_providers import (
    EVENT_CONTENT,
    EVENT_REASONING,
    EVENT_SOURCE,
    EVENT_STATUS,
    STATUS_GENERATING,
    STATUS_RESEARCHING,
    STATUS_THINKING,
    STATUS_VISITING,
    STATUS_WAITING,
    AIEvent,
    AIProvider,
    GeminiProvider,
    GroqProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderError,
    ThinkingTagSplitter,
    status_speech,
)


def setUpModule():
    # Nothing in this module may touch a real provider, and a local model load is
    # the most expensive way to find out it did.
    netguard.block()


def tearDownModule():
    netguard.restore()


class FakeStreamResponse:
    """A streaming response body, with the ``requests`` surface we use."""

    def __init__(self, lines=(), status_code=200, payload=None, fail_after=None):
        self.status_code = status_code
        self.lines = list(lines)
        self._payload = payload
        self.closed = False
        self.fail_after = fail_after

    def iter_content(self, chunk_size=None, decode_unicode=False):
        """Yield the raw bytes of each line, the way requests hands them over.

        Bytes are what the provider layer decodes itself, so a fixture may pass
        bytes directly to pin down a chunk boundary or a bad encoding.
        """
        for index, line in enumerate(self.lines):
            if self.fail_after is not None and index >= self.fail_after:
                raise requests.exceptions.ConnectionError("connection lost")
            if isinstance(line, bytes):
                yield line
            else:
                yield (line + "\n").encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("no json here")
        return self._payload

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def sse(payload):
    return "data: " + json.dumps(payload)


def ndjson(payload):
    return json.dumps(payload)


def collect(provider, **kwargs):
    """Run one streamed question and return ``(reply, events)``."""
    events = []
    reply = provider.ask_stream("hello", kwargs.pop("model", "test-model"),
                                on_event=events.append, **kwargs)
    return reply, events


def kinds(events):
    return [event.kind for event in events]


def texts(events, kind):
    return [event.text for event in events if event.kind == kind]


class NetworkGuardTests(unittest.TestCase):
    """The guard is what stops a stray test from loading a model on this machine."""

    def test_a_raw_connection_is_refused(self):
        with self.assertRaises(AssertionError) as ctx:
            socket.create_connection(("localhost", 11434), timeout=0.2)
        self.assertIn("tried to open a connection", str(ctx.exception))

    def test_an_unstubbed_provider_call_fails_instead_of_reaching_ollama(self):
        # This is the mistake the guard exists for: a provider call nobody stubbed
        # would otherwise load a multi-gigabyte model onto the user's computer.
        # The refusal travels all the way out of the provider, so a leak is never
        # mistaken for an ordinary offline error.
        with self.assertRaises(AssertionError) as ctx:
            OllamaProvider().list_models()
        self.assertIn("tried to open a connection", str(ctx.exception))
        self.assertIn("Stub the provider call", str(ctx.exception))


class EventTests(unittest.TestCase):
    def test_status_speech_covers_the_vocabulary(self):
        self.assertEqual(status_speech(STATUS_THINKING), "Thinking...")
        self.assertEqual(status_speech(STATUS_GENERATING), "Generating response...")
        self.assertEqual(status_speech(STATUS_RESEARCHING), "Researching...")
        self.assertEqual(status_speech(STATUS_VISITING), "Visiting website...")
        self.assertEqual(status_speech(STATUS_WAITING, "Groq"), "Waiting for Groq...")

    def test_an_unknown_status_says_nothing(self):
        self.assertEqual(status_speech("plotting"), "")

    def test_events_compare_by_contents(self):
        self.assertEqual(AIEvent("status", "thinking"), AIEvent("status", "thinking"))
        self.assertNotEqual(AIEvent("status", "thinking"), AIEvent("status", "generating"))

    def test_the_default_stream_asks_once_and_reports_one_answer(self):
        seen = {}

        class Stub(AIProvider):
            def ask(self, prompt, model, system=None, web=False, history=None):
                seen["history"] = history
                return "A finished answer."

        reply, events = collect(Stub(), history=[("user", "earlier")])
        self.assertEqual(reply, "A finished answer.")
        self.assertEqual(kinds(events), [EVENT_CONTENT])
        self.assertEqual(texts(events, EVENT_CONTENT), ["A finished answer."])
        # The one-shot path carries the conversation too.
        self.assertEqual(seen["history"], [("user", "earlier")])

    def test_a_provider_that_cannot_research_says_so(self):
        provider = OllamaProvider()
        self.assertFalse(provider.can_research("llama3"))
        self.assertIn("cannot search the web", provider.web_unavailable("llama3"))


class ThinkTagTests(unittest.TestCase):
    def test_plain_text_passes_through(self):
        splitter = ThinkingTagSplitter()
        self.assertEqual(splitter.feed("just an answer"), [(False, "just an answer")])

    def test_tagged_reasoning_is_separated(self):
        splitter = ThinkingTagSplitter()
        pieces = splitter.feed("<think>weighing it up</think>the answer")
        self.assertEqual(pieces, [(True, "weighing it up"), (False, "the answer")])

    def test_a_tag_split_across_chunks_is_not_read_as_text(self):
        splitter = ThinkingTagSplitter()
        self.assertEqual(splitter.feed("Answer <thi"), [(False, "Answer ")])
        self.assertEqual(splitter.feed("nk>hidden</think>rest"), [(True, "hidden"), (False, "rest")])

    def test_a_lone_angle_bracket_is_still_text(self):
        splitter = ThinkingTagSplitter()
        self.assertEqual(splitter.feed("a < b"), [(False, "a < b")])


class LineReaderTests(unittest.TestCase):
    def test_sse_skips_comments_events_and_done(self):
        response = FakeStreamResponse([
            ": keep-alive",
            "event: message",
            "",
            sse({"a": 1}),
            "data: [DONE]",
        ])
        self.assertEqual(list(ai_providers._iter_sse(response)), [{"a": 1}])

    def test_a_payload_that_never_parses_is_surfaced_not_dropped(self):
        # It used to be skipped silently, which lost text without a trace.
        response = FakeStreamResponse([sse({"a": 1}), "data: not json", "data: [DONE]"])
        self.assertEqual(
            list(ai_providers._iter_sse(response)),
            [{"a": 1}, {"_unreadable": "not json"}],
        )

    def test_a_json_object_split_over_two_data_lines_is_put_back_together(self):
        whole = json.dumps({"content": "the answer"})
        cut = len(whole) // 2
        response = FakeStreamResponse(["data: " + whole[:cut], "data: " + whole[cut:]])
        self.assertEqual(list(ai_providers._iter_sse(response)), [{"content": "the answer"}])

    def test_data_lines_of_one_event_are_joined_the_way_the_format_says(self):
        response = FakeStreamResponse(["data: {\"content\":", "data: \"hello\"}"])
        self.assertEqual(list(ai_providers._iter_sse(response)), [{"content": "hello"}])

    def test_ndjson_skips_blank_lines(self):
        response = FakeStreamResponse(["", ndjson({"a": 1}), ""])
        self.assertEqual(list(ai_providers._iter_ndjson(response)), [{"a": 1}])

    def test_ndjson_surfaces_a_line_that_is_not_json(self):
        response = FakeStreamResponse([ndjson({"a": 1}), "nonsense"])
        self.assertEqual(
            list(ai_providers._iter_ndjson(response)),
            [{"a": 1}, {"_unreadable": "nonsense"}],
        )

    def test_a_dropped_connection_is_reported_as_offline(self):
        response = FakeStreamResponse(
            [ndjson({"response": "half"}), ndjson({"response": "more"})], fail_after=1
        )
        with self.assertRaises(ProviderError) as ctx:
            list(ai_providers._iter_ndjson(response))
        self.assertEqual(ctx.exception.kind, "offline")


class OllamaStreamTests(unittest.TestCase):
    THINKING_MODEL = [
        ndjson({"thinking": "Let me work it out.", "done": False}),
        ndjson({"thinking": " That makes 42.", "done": False}),
        ndjson({"response": "The answer", "done": False}),
        ndjson({"response": " is 42.", "done": True}),
    ]

    @mock.patch("ai_providers.requests.post")
    def test_thinking_is_reported_before_the_answer(self, post):
        post.return_value = FakeStreamResponse(self.THINKING_MODEL)

        reply, events = collect(OllamaProvider(), model="deepseek-r1:7b")

        self.assertEqual(reply, "The answer is 42.")
        self.assertEqual(
            kinds(events),
            [EVENT_STATUS, EVENT_REASONING, EVENT_REASONING, EVENT_STATUS, EVENT_CONTENT, EVENT_CONTENT],
        )
        self.assertEqual(
            texts(events, EVENT_STATUS), [STATUS_THINKING, STATUS_GENERATING]
        )
        self.assertEqual(
            texts(events, EVENT_REASONING), ["Let me work it out.", " That makes 42."]
        )

    @mock.patch("ai_providers.requests.post")
    def test_a_plain_model_never_claims_to_be_thinking(self, post):
        post.return_value = FakeStreamResponse([
            ndjson({"response": "Hello", "done": False}),
            ndjson({"response": " there.", "done": True}),
        ])

        reply, events = collect(OllamaProvider(), model="llama3")

        self.assertEqual(reply, "Hello there.")
        self.assertEqual(texts(events, EVENT_STATUS), [STATUS_GENERATING])
        self.assertNotIn(STATUS_THINKING, texts(events, EVENT_STATUS))

    @mock.patch("ai_providers.requests.post")
    def test_reasoning_is_only_asked_for_where_it_makes_sense(self, post):
        post.return_value = FakeStreamResponse([ndjson({"response": "ok", "done": True})])
        collect(OllamaProvider(), model="llama3")
        self.assertNotIn("think", post.call_args[1]["json"])

        collect(OllamaProvider(), model="qwen3:8b")
        self.assertTrue(post.call_args[1]["json"]["think"])

    @mock.patch("ai_providers.requests.post")
    def test_the_request_streams_and_keeps_the_reference(self, post):
        post.return_value = FakeStreamResponse([ndjson({"response": "ok", "done": True})])
        collect(OllamaProvider(), system="PYOS REFERENCE")
        payload = post.call_args[1]["json"]
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["system"], "PYOS REFERENCE")
        self.assertEqual(payload["prompt"], "hello")

    @mock.patch("ai_providers.requests.post")
    def test_an_error_chunk_is_raised(self, post):
        post.return_value = FakeStreamResponse([ndjson({"error": "model not found"})])
        with self.assertRaises(ProviderError) as ctx:
            collect(OllamaProvider())
        self.assertIn("model not found", ctx.exception.message)

    @mock.patch("ai_providers.requests.post")
    def test_a_failed_request_carries_the_servers_own_wording(self, post):
        post.return_value = FakeStreamResponse(
            status_code=404, payload={"error": "model 'llama3' not found"}
        )
        with self.assertRaises(ProviderError) as ctx:
            collect(OllamaProvider())
        self.assertEqual(ctx.exception.kind, "http")
        self.assertIn("not found", ctx.exception.message)

    @mock.patch("ai_providers.requests.post")
    def test_a_timeout_is_not_blamed_on_the_server_being_down(self, post):
        post.side_effect = requests.exceptions.ReadTimeout("timed out")
        with self.assertRaises(ProviderError) as ctx:
            collect(OllamaProvider())
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertIn("did not reply in time", ctx.exception.message)
        self.assertNotIn("Make sure Ollama is running", ctx.exception.message)

    @mock.patch("ai_providers.requests.post")
    def test_a_connect_timeout_still_reads_as_offline(self, post):
        post.side_effect = requests.exceptions.ConnectTimeout("cannot connect")
        with self.assertRaises(ProviderError) as ctx:
            collect(OllamaProvider())
        self.assertEqual(ctx.exception.kind, "offline")
        self.assertIn("Make sure Ollama is running", ctx.exception.message)

    @mock.patch("ai_providers.requests.post")
    def test_a_cloud_timeout_names_the_provider(self, post):
        post.side_effect = requests.exceptions.ReadTimeout("timed out")
        with self.assertRaises(ProviderError) as ctx:
            collect(GroqProvider("gsk-test"), model="groq/compound")
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertIn("Groq did not reply in time", ctx.exception.message)

    @mock.patch("ai_providers.requests.post")
    def test_an_empty_stream_says_so(self, post):
        post.return_value = FakeStreamResponse([])
        reply, events = collect(OllamaProvider())
        self.assertIn("couldn't generate", reply)
        self.assertEqual(events, [])


class GeminiStreamTests(unittest.TestCase):
    def setUp(self):
        self.provider = GeminiProvider("test-key")
        ai_providers.THINKING_CONFIG_REFUSED.clear()
        self.addCleanup(ai_providers.THINKING_CONFIG_REFUSED.clear)

    def _thinking_stream(self):
        return FakeStreamResponse([
            sse({"candidates": [{"content": {"parts": [
                {"text": "First, weigh the clues.", "thought": True}
            ]}}]}),
            sse({"candidates": [{
                "content": {"parts": [{"text": "The answer is 42."}]},
                "finishReason": "STOP",
            }]}),
        ])

    @mock.patch("ai_providers.requests.post")
    def test_thought_summaries_come_before_the_answer(self, post):
        post.return_value = self._thinking_stream()

        reply, events = collect(self.provider, model="gemini-2.5-flash")

        self.assertEqual(reply, "The answer is 42.")
        self.assertEqual(
            texts(events, EVENT_STATUS), [STATUS_THINKING, STATUS_GENERATING]
        )
        self.assertEqual(texts(events, EVENT_REASONING), ["First, weigh the clues."])
        self.assertEqual(texts(events, EVENT_CONTENT), ["The answer is 42."])

    @mock.patch("ai_providers.requests.post")
    def test_the_stream_url_and_thought_setting_are_sent(self, post):
        post.return_value = self._thinking_stream()
        collect(self.provider, model="models/gemini-2.5-flash")

        url = post.call_args[0][0]
        self.assertIn("/models/gemini-2.5-flash:streamGenerateContent", url)
        self.assertEqual(post.call_args[1]["params"]["alt"], "sse")
        payload = post.call_args[1]["json"]
        self.assertEqual(
            payload["generationConfig"], {"thinkingConfig": {"includeThoughts": True}}
        )

    @mock.patch("ai_providers.requests.post")
    def test_a_model_without_thought_summaries_is_retried_without_them(self, post):
        post.side_effect = [
            FakeStreamResponse(
                status_code=400,
                payload={"error": {"message": "thinkingConfig is not supported"}},
            ),
            self._thinking_stream(),
        ]

        reply, events = collect(self.provider, model="gemini-2.0-flash-lite")

        self.assertEqual(reply, "The answer is 42.")
        self.assertNotIn("generationConfig", post.call_args[1]["json"])
        # A later question to the same model does not try the setting again.
        self.assertIn("gemini-2.0-flash-lite", ai_providers.THINKING_CONFIG_REFUSED)
        post.side_effect = [self._thinking_stream()]
        collect(self.provider, model="gemini-2.0-flash-lite")
        self.assertNotIn("generationConfig", post.call_args[1]["json"])

    @mock.patch("ai_providers.requests.post")
    def test_search_and_pages_are_reported_from_the_grounding_metadata(self, post):
        post.return_value = FakeStreamResponse([
            sse({"candidates": [{
                "content": {"parts": [{"text": "It lists files."}]},
                "groundingMetadata": {
                    "webSearchQueries": ["pyos terminal list command"],
                    "groundingChunks": [
                        {"web": {"uri": "https://example.test/pyos", "title": "PyOS docs"}}
                    ],
                },
            }]}),
            sse({"candidates": [{
                "content": {"parts": [{"text": " Sources:"}]},
                "urlContextMetadata": {"urlMetadata": [
                    {"retrievedUrl": "https://example.test/pyos",
                     "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS"},
                    {"retrievedUrl": "https://example.test/gone",
                     "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_ERROR"},
                ]},
                "finishReason": "STOP",
            }]}),
        ])

        reply, events = collect(self.provider, model="gemini-2.5-flash", web=True)

        # The search is reported before the answer text in the same chunk, which
        # is the order a grounded reply really arrives in.
        self.assertEqual(
            texts(events, EVENT_STATUS),
            [STATUS_RESEARCHING, STATUS_GENERATING, STATUS_VISITING, STATUS_VISITING],
        )
        self.assertEqual(
            [event.detail for event in events if event.text == STATUS_VISITING],
            ["https://example.test/pyos", "https://example.test/gone"],
        )
        # A page that was already counted as a search result is not counted
        # twice, and a page that could not be read is never offered as a source.
        self.assertEqual(texts(events, EVENT_SOURCE), ["PyOS docs"])
        self.assertEqual(
            [event.detail for event in events if event.kind == EVENT_SOURCE],
            ["https://example.test/pyos"],
        )

    @mock.patch("ai_providers.requests.post")
    def test_web_tools_are_only_asked_for_when_research_is_on(self, post):
        post.return_value = self._thinking_stream()
        collect(self.provider, model="gemini-2.5-flash")
        self.assertNotIn("tools", post.call_args[1]["json"])

        post.return_value = self._thinking_stream()
        collect(self.provider, model="gemini-2.5-flash", web=True)
        tools = post.call_args[1]["json"]["tools"]
        self.assertEqual(tools, [{"google_search": {}}, {"url_context": {}}])

    @mock.patch("ai_providers.requests.post")
    def test_a_stream_that_dies_mid_answer_is_reported(self, post):
        post.return_value = FakeStreamResponse([
            sse({"candidates": [{"content": {"parts": [{"text": "Half an answer"}]}}]}),
            sse({"candidates": [{"content": {"parts": [{"text": " and more"}]}}]}),
        ], fail_after=1)
        events = []

        with self.assertRaises(ProviderError) as ctx:
            self.provider.ask_stream(
                "hello", "gemini-2.5-flash", on_event=events.append
            )

        # The text that did arrive is still reported, and the failure is raised
        # so the assistant can say the answer was cut short.
        self.assertEqual(ctx.exception.kind, "offline")
        self.assertEqual(texts(events, EVENT_CONTENT), ["Half an answer"])

    @mock.patch("ai_providers.requests.post")
    def test_an_api_key_problem_is_read_from_the_body(self, post):
        post.return_value = FakeStreamResponse(
            status_code=400, payload={"error": {"message": "API key not valid"}}
        )
        with self.assertRaises(ProviderError) as ctx:
            collect(self.provider, model="gemini-2.5-flash")
        self.assertEqual(ctx.exception.kind, "bad_key")
        self.assertIn("API key not valid", ctx.exception.message)

    @mock.patch("ai_providers.requests.post")
    def test_a_stream_with_no_answer_explains_itself(self, post):
        post.return_value = FakeStreamResponse([
            sse({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}),
        ])
        reply, _ = collect(self.provider, model="gemini-2.5-flash")
        self.assertIn("safety", reply.lower())

    def test_a_missing_key_is_explained_before_anything_is_sent(self):
        with self.assertRaises(ProviderError) as ctx:
            GeminiProvider().ask_stream("hi", "gemini-2.5-flash")
        self.assertEqual(ctx.exception.kind, "no_key")


class OpenRouterStreamTests(unittest.TestCase):
    def setUp(self):
        self.provider = OpenRouterProvider("sk-or-test")

    def _reasoning_stream(self):
        return FakeStreamResponse([
            sse({"choices": [{"delta": {"reasoning": "Weighing it up."}}]}),
            sse({"choices": [{"delta": {"content": "It is 42."}}]}),
            sse({"choices": [{
                "delta": {},
                "message": {
                    "content": "It is 42.",
                    "annotations": [{
                        "type": "url_citation",
                        "url_citation": {"url": "https://x.test/one", "title": "One"},
                    }],
                },
                "finish_reason": "stop",
            }]}),
            "data: [DONE]",
        ])

    @mock.patch("ai_providers.requests.post")
    def test_reasoning_and_annotations_are_reported(self, post):
        post.return_value = self._reasoning_stream()

        reply, events = collect(self.provider, model="deepseek/deepseek-r1")

        self.assertEqual(reply, "It is 42.")
        self.assertEqual(
            texts(events, EVENT_STATUS), [STATUS_THINKING, STATUS_GENERATING]
        )
        self.assertEqual(texts(events, EVENT_REASONING), ["Weighing it up."])
        self.assertEqual(texts(events, EVENT_SOURCE), ["One"])
        self.assertEqual(post.call_args[1]["json"]["stream"], True)

    @mock.patch("ai_providers.requests.post")
    def test_tagged_reasoning_inside_content_is_still_reasoning(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "<think>why 42</thi"}}]}),
            sse({"choices": [{"delta": {"content": "nk>42"}}]}),
        ])

        reply, events = collect(self.provider, model="some/model")

        self.assertEqual(reply, "42")
        self.assertEqual(texts(events, EVENT_REASONING), ["why 42"])

    @mock.patch("ai_providers.requests.post")
    def test_web_research_is_asked_for_with_the_plugin(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "hi"}}]}),
        ])

        _, events = collect(self.provider, model="openai/gpt-4o-mini", web=True)

        self.assertEqual(post.call_args[1]["json"]["plugins"], [{"id": "web"}])
        # The plugin always searches, so this status is announced from the
        # request rather than from the stream.
        self.assertEqual(texts(events, EVENT_STATUS)[0], STATUS_RESEARCHING)

    @mock.patch("ai_providers.requests.post")
    def test_web_is_left_out_when_research_is_off(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "hi"}}]}),
        ])
        _, events = collect(self.provider, model="openai/gpt-4o-mini")
        self.assertNotIn("plugins", post.call_args[1]["json"])
        self.assertNotIn(STATUS_RESEARCHING, texts(events, EVENT_STATUS))

    @mock.patch("ai_providers.requests.post")
    def test_a_refusal_is_explained(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {}, "finish_reason": "content_filter"}]}),
        ])
        reply, _ = collect(self.provider, model="openai/gpt-4o-mini")
        self.assertIn("refused", reply)

    @mock.patch("ai_providers.requests.post")
    def test_a_rejected_key_is_reported(self, post):
        post.return_value = FakeStreamResponse(status_code=401)
        with self.assertRaises(ProviderError) as ctx:
            collect(self.provider, model="openai/gpt-4o-mini")
        self.assertEqual(ctx.exception.kind, "bad_key")


class GroqStreamTests(unittest.TestCase):
    def setUp(self):
        self.provider = GroqProvider("gsk-test")
        self.compound_reply = [
            sse({"choices": [{"delta": {"reasoning": "I should search."}}]}),
            sse({"choices": [{
                "delta": {
                    "executed_tools": [
                        {"type": "search", "arguments": '{"query": "pyos terminal"}',
                         "search_results": [
                             {"url": "https://a.test/1", "title": "First"},
                             {"url": "https://b.test/2", "title": "Second"},
                         ]},
                        {"type": "visit", "arguments": '{"url": "https://a.test/1"}'},
                    ]
                },
            }]}),
            sse({"choices": [{"delta": {"content": "It lists files."}, "finish_reason": "stop"}]}),
        ]

    @mock.patch("ai_providers.requests.post")
    def test_reasoning_arrives_in_its_own_field(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"reasoning": "Two r's."}}]}),
            sse({"choices": [{"delta": {"content": "Two."}}]}),
        ])

        reply, events = collect(self.provider, model="openai/gpt-oss-120b")

        self.assertEqual(reply, "Two.")
        self.assertEqual(
            texts(events, EVENT_STATUS), [STATUS_THINKING, STATUS_GENERATING]
        )
        self.assertEqual(texts(events, EVENT_REASONING), ["Two r's."])

    @mock.patch("ai_providers.requests.post")
    def test_reasoning_is_asked_for_in_parsed_form(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "ok"}}]}),
        ])
        collect(self.provider, model="openai/gpt-oss-120b")
        self.assertEqual(post.call_args[1]["json"]["reasoning_format"], "parsed")

    @mock.patch("ai_providers.requests.post")
    def test_compound_tools_are_reported_as_research_and_visits(self, post):
        post.return_value = FakeStreamResponse(self.compound_reply)

        reply, events = collect(self.provider, model="groq/compound", web=True)

        self.assertEqual(reply, "It lists files.")
        self.assertEqual(
            texts(events, EVENT_STATUS),
            [STATUS_THINKING, STATUS_RESEARCHING, STATUS_VISITING, STATUS_GENERATING],
        )
        self.assertEqual(
            [event.detail for event in events if event.text == STATUS_RESEARCHING],
            ["pyos terminal"],
        )
        self.assertEqual(
            texts(events, EVENT_SOURCE), ["First", "Second"]
        )

    @mock.patch("ai_providers.requests.post")
    def test_a_model_that_uses_no_tool_never_claims_to_research(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "Just an answer."}}]}),
        ])

        _, events = collect(self.provider, model="groq/compound", web=True)

        self.assertEqual(texts(events, EVENT_STATUS), [STATUS_GENERATING])

    @mock.patch("ai_providers.requests.post")
    def test_compound_tools_are_named_in_the_request(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "ok"}}]}),
        ])
        collect(self.provider, model="groq/compound", web=True)

        payload = post.call_args[1]["json"]
        self.assertEqual(
            payload["compound_custom"],
            {"tools": {"enabled_tools": ["web_search", "visit_website"]}},
        )
        self.assertNotIn("tools", payload)

    @mock.patch("ai_providers.requests.post")
    def test_gpt_oss_gets_its_own_browser_search_tool(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "ok"}}]}),
        ])
        collect(self.provider, model="openai/gpt-oss-120b", web=True)
        self.assertEqual(post.call_args[1]["json"]["tools"], [{"type": "browser_search"}])

    @mock.patch("ai_providers.requests.post")
    def test_a_plain_model_is_not_offered_research(self, post):
        post.return_value = FakeStreamResponse([
            sse({"choices": [{"delta": {"content": "ok"}}]}),
        ])
        _, events = collect(self.provider, model="llama-3.3-70b", web=True)
        payload = post.call_args[1]["json"]
        self.assertNotIn("tools", payload)
        self.assertNotIn("compound_custom", payload)
        self.assertNotIn(STATUS_RESEARCHING, texts(events, EVENT_STATUS))

    def test_research_capability_follows_the_model(self):
        self.assertTrue(self.provider.can_research("groq/compound"))
        self.assertTrue(self.provider.can_research("openai/gpt-oss-120b"))
        self.assertFalse(self.provider.can_research("llama-3.3-70b-versatile"))

    @mock.patch("ai_providers.requests.post")
    def test_a_missing_key_stops_before_the_network(self, post):
        with self.assertRaises(ProviderError) as ctx:
            GroqProvider().ask_stream("hi", "groq/compound")
        self.assertEqual(ctx.exception.kind, "no_key")
        post.assert_not_called()


class CancelTests(unittest.TestCase):
    STREAM = [
        ndjson({"response": "one ", "done": False}),
        ndjson({"response": "two ", "done": False}),
        ndjson({"response": "three", "done": True}),
    ]

    @mock.patch("ai_providers.requests.post")
    def test_a_cancelled_request_reports_nothing(self, post):
        post.return_value = FakeStreamResponse(self.STREAM)
        cancel = threading.Event()
        cancel.set()

        reply, events = collect(OllamaProvider(), cancel=cancel)

        self.assertEqual(reply, "")
        self.assertEqual(events, [])

    @mock.patch("ai_providers.requests.post")
    def test_cancelling_mid_stream_keeps_what_arrived(self, post):
        post.return_value = FakeStreamResponse(self.STREAM)
        cancel = threading.Event()
        events = []

        def on_event(event):
            events.append(event)
            if event.kind == EVENT_CONTENT:
                cancel.set()

        reply = OllamaProvider().ask_stream(
            "hello", "llama3", on_event=on_event, cancel=cancel
        )

        self.assertEqual(reply, "one")
        self.assertEqual(texts(events, EVENT_CONTENT), ["one "])

    @mock.patch("ai_providers.requests.post")
    def test_the_body_is_closed_when_the_user_stops(self, post):
        response = FakeStreamResponse(self.STREAM)
        post.return_value = response
        cancel = threading.Event()
        cancel.set()
        collect(OllamaProvider(), cancel=cancel)
        self.assertTrue(response.closed)


class AnswerParsingTests(unittest.TestCase):
    """The one-shot path still behaves exactly as it did before streaming."""

    @mock.patch("ai_providers.requests.post")
    def test_gemini_ignores_thought_parts_when_answering_once(self, post):
        post.return_value = FakeStreamResponse(payload={"candidates": [{"content": {
            "parts": [{"text": "thinking out loud", "thought": True}, {"text": "The answer."}]
        }}]})
        provider = GeminiProvider("test-key")
        self.assertEqual(provider.ask("hi", "gemini-2.5-flash"), "The answer.")

    @mock.patch("ai_providers.requests.post")
    def test_groq_answers_once_with_parsed_reasoning(self, post):
        post.return_value = FakeStreamResponse(payload={"choices": [{
            "message": {"content": "The answer.", "reasoning": "because"}
        }]})
        self.assertEqual(
            GroqProvider("gsk-test").ask("hi", "openai/gpt-oss-120b"), "The answer."
        )
        self.assertEqual(post.call_args[1]["json"]["reasoning_format"], "parsed")


if __name__ == "__main__":
    unittest.main()
