"""Checks for the text a model writes arriving intact.

Everything here comes from one real failure. A streamed answer is a
``text/event-stream`` body, that header almost never names a charset, and
``requests`` answers a charset-less ``text/*`` response by decoding it as
ISO-8859-1. UTF-8 bytes read as Latin-1 turn ``café`` into ``cafÃ©``, an em dash
into ``â€"`` and an emoji into ``ðŸ˜€``; a byte such as ``0x85`` inside one of
those characters then told ``str.splitlines`` the JSON line had ended, so the
payload was cut in half and thrown away.

The provider layer now decodes the raw bytes itself as UTF-8, and
:mod:`text_integrity` repairs anything that was mangled before it got here and
keeps characters no voice can carry away from the engine. These tests pin all of
that down with byte-level fixtures, because a smiley that arrives as four
question marks is not something a reading test would catch.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import netguard
import ai_providers
import speech
import text_integrity as ti
from ai_providers import GeminiProvider, OpenAICompatibleProvider, OllamaProvider

# One sentence with everything that used to break: accents, an em dash, curly
# quotes, an emoji and a tick whose UTF-8 bytes contain the line-splitting byte
# 0x85.
TRICKY = "Caf\u00e9 \u2014 na\u00efve \u2018quoted\u2019 \U0001F600 \u2705 done"


def setUpModule():
    netguard.block()


def tearDownModule():
    netguard.restore()


def sse_bytes(payload):
    """One server-sent event, as the provider would put it on the wire."""
    body = json.dumps(payload, ensure_ascii=False)
    return ("data: " + body + "\n\n").encode("utf-8")


class RawStreamResponse:
    """A response that hands over the exact bytes given to it."""

    status_code = 200

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=None, decode_unicode=False):
        for chunk in self.chunks:
            yield chunk

    def json(self):
        raise ValueError("no json here")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class StreamDecodingTests(unittest.TestCase):
    """The bytes of a stream are read as UTF-8, whatever the headers say."""

    def test_a_full_alphabet_of_characters_survives_the_wire(self):
        body = sse_bytes({"choices": [{"delta": {"content": TRICKY}}]})
        response = RawStreamResponse([body])
        (event,) = list(ai_providers._iter_sse(response))

        self.assertEqual(event["choices"][0]["delta"]["content"], TRICKY)

    def test_a_character_split_across_two_chunks_is_rejoined(self):
        body = sse_bytes({"content": TRICKY})
        # Cut in the middle of a multi-byte character on purpose.
        cut = body.index("\u2014".encode("utf-8")) + 1
        response = RawStreamResponse([body[:cut], body[cut:]])
        (event,) = list(ai_providers._iter_sse(response))

        self.assertEqual(event["content"], TRICKY)

    def test_a_line_splitting_byte_inside_a_character_does_not_cut_the_event(self):
        # 0x85 lives inside the tick's UTF-8 bytes, and str.splitlines treats it
        # as a line break: the old code read a JSON line in halves and dropped it.
        self.assertIn(b"\x85", TRICKY.encode("utf-8"))
        response = RawStreamResponse([sse_bytes({"content": TRICKY})])
        (event,) = list(ai_providers._iter_sse(response))

        self.assertEqual(event["content"], TRICKY)

    def test_nothing_turns_into_a_question_mark(self):
        response = RawStreamResponse([sse_bytes({"content": TRICKY})])
        (event,) = list(ai_providers._iter_sse(response))

        self.assertNotIn("?", event["content"])

    def test_a_whole_ollama_answer_keeps_its_accents_and_emoji(self):
        chunks = [
            (json.dumps({"message": {"content": TRICKY}}, ensure_ascii=False) + "\n").encode(),
            (json.dumps({"done": True}) + "\n").encode(),
        ]
        response = RawStreamResponse(chunks)
        with mock.patch("ai_providers.requests.post", return_value=response):
            reply = OllamaProvider().ask_stream("hi", "llama3")

        self.assertEqual(reply, TRICKY)

    def test_an_openai_dialect_answer_keeps_them_too(self):
        chunks = [
            sse_bytes({"choices": [{"delta": {"content": TRICKY}}]}),
            b"data: [DONE]\n\n",
        ]
        response = RawStreamResponse(chunks)

        class Named(OpenAICompatibleProvider):
            provider_name = "Test"
            base_url = "https://example.invalid/v1"

            def _require_key(self):
                return None

        with mock.patch("ai_providers.requests.post", return_value=response):
            reply = Named("k").ask_stream("hi", "test-model")

        self.assertEqual(reply, TRICKY)

    def test_a_trailing_payload_without_a_newline_is_still_read(self):
        response = RawStreamResponse([json.dumps({"content": "last"}).encode("utf-8")])
        self.assertEqual(
            list(ai_providers._iter_ndjson(response)),
            [{"content": "last"}],
        )


class MojibakeRepairTests(unittest.TestCase):
    """Text mangled before it reached us is put back together."""

    def test_latin_1_mangled_text_is_repaired(self):
        mangled = TRICKY.encode("utf-8").decode("latin-1")
        self.assertNotEqual(mangled, TRICKY)

        self.assertEqual(ti.for_display(mangled), TRICKY)

    def test_windows_ansi_mangled_text_is_repaired(self):
        mangled = TRICKY.encode("utf-8").decode("cp1252")

        self.assertEqual(ti.for_display(mangled), TRICKY)

    def test_the_repair_is_announced_as_suspicious_before_it_is_made(self):
        mangled = "caf\u00c3\u00a9"
        self.assertTrue(ti.is_suspicious(mangled))
        self.assertFalse(ti.is_suspicious(ti.for_display(mangled)))

    def test_ordinary_prose_with_accents_is_left_alone(self):
        for text in ("Caf\u00e9 \u00e0 la carte", "na\u00efve r\u00e9sum\u00e9", "5 + 3 = 8"):
            self.assertEqual(ti.for_display(text), text)
            self.assertFalse(ti.is_suspicious(text))

    def test_a_trademark_in_a_name_is_not_mistaken_for_a_repair(self):
        # No hint sequence means no attempt is made, whatever the codepage.
        text = "The \u2122 symbol and the \u20ac sign"
        self.assertEqual(ti.for_display(text), text)


class SurrogateTests(unittest.TestCase):
    """A half of an emoji is not a character, and must never reach a voice."""

    def test_two_halves_are_rejoined_into_the_character(self):
        split = "\ud83d\ude00"
        self.assertEqual(ti.for_display(split), "\U0001F600")

    def test_a_lone_half_becomes_a_replacement_mark_not_a_question_mark(self):
        display = ti.for_display("ok \ud83d done")
        self.assertNotIn("\ud83d", display)
        self.assertNotIn("?", display)

    def test_a_lone_half_never_reaches_the_engine(self):
        self.assertEqual(ti.for_speech("ok \ud83d done"), "ok done")

    def test_an_emoji_is_dropped_from_speech_but_kept_in_the_history(self):
        self.assertIn("\U0001F600", ti.for_display("done \U0001F600"))
        self.assertNotIn("\U0001F600", ti.for_speech("done \U0001F600"))

    def test_dashes_become_pauses_and_accents_are_kept(self):
        speech = ti.for_speech("caf\u00e9 \u2014 na\u00efve \u2014 5 + 3")
        self.assertIn("caf\u00e9", speech)
        self.assertNotIn("\u2014", speech)
        self.assertIn("5 + 3", speech)

    def test_control_characters_are_removed(self):
        self.assertEqual(ti.for_display("a\x07b\x1fc"), "abc")

    def test_a_replacement_mark_run_is_collapsed(self):
        self.assertEqual(ti.for_display("a\ufffd\ufffd\ufffdb"), "a\ufffdb")

    def test_cleaning_twice_changes_nothing(self):
        for text in (TRICKY, "caf\u00c3\u00a9", "ok \ud83d", "a\x07b"):
            once = ti.for_speech(text)
            self.assertEqual(ti.for_speech(once), once)

    def test_a_sign_of_damage_is_reported(self):
        for text in ("\ud83d", "a\x07b", "caf\u00c3\u00a9", "a\ufffdb"):
            self.assertTrue(ti.is_suspicious(text), text)
        self.assertFalse(ti.is_suspicious("Nothing wrong here at all."))


class SpeechEngineTests(unittest.TestCase):
    """The engine itself is the last gate before a voice sees anything."""

    class FakeDll:
        def __init__(self):
            self.spoken = []

        def nvdaController_speakText(self, text):
            # The real call takes a c_wchar_p, so unwrap what the engine handed
            # over: this is exactly the text a screen reader would receive.
            self.spoken.append(getattr(text, "value", text))
            return 0

        def nvdaController_cancelSpeech(self):
            return 0

    def setUp(self):
        self._previous = os.environ.get("PY_OS_DATA_DIR")
        self.tmpdir = tempfile.mkdtemp()
        os.environ["PY_OS_DATA_DIR"] = self.tmpdir
        self.addCleanup(self._restore)

    def _restore(self):
        if self._previous is None:
            os.environ.pop("PY_OS_DATA_DIR", None)
        else:
            os.environ["PY_OS_DATA_DIR"] = self._previous

    def build_engine(self):
        with mock.patch.object(speech, "get_platform_name", return_value="Windows"), \
                mock.patch.object(speech.SpeechEngine, "_load_config"), \
                mock.patch.object(speech.SpeechEngine, "_load_nvda_if_available"), \
                mock.patch.object(speech.SpeechEngine, "_apply_mode"):
            engine = speech.SpeechEngine()
        engine.use_nvda = True
        engine.nvda_dll = self.FakeDll()
        return engine

    def speak_through(self, text):
        engine = self.build_engine()
        with mock.patch.object(
            speech.SpeechEngine, "_apply_mode", return_value=None
        ), mock.patch.object(speech.SpeechEngine, "_nvda_available", return_value=True):
            engine.speak(text)
        return engine.nvda_dll.spoken

    def test_an_emoji_and_a_lone_surrogate_never_reach_the_voice(self):
        spoken = self.speak_through("done \U0001F600 and \ud83d partly")
        self.assertEqual(len(spoken), 1)
        self.assertNotIn("\U0001F600", spoken[0])
        self.assertNotIn("\ud83d", spoken[0])
        self.assertNotIn("?", spoken[0])

    def test_text_that_is_only_an_emoji_says_nothing_at_all(self):
        self.assertEqual(self.speak_through("\U0001F600"), [])

    def test_ordinary_text_is_passed_through_with_its_accents(self):
        spoken = self.speak_through("Caf\u00e9 time.")
        self.assertEqual(spoken, ["Caf\u00e9 time."])


class PromptEncodingTests(unittest.TestCase):
    """What is sent is UTF-8 on the wire, so the model is not sent mojibake."""

    def test_a_prompt_and_reference_with_accents_are_sent_as_utf8(self):
        payload = {
            "model": "llama3",
            "messages": [
                {"role": "system", "content": "The PyOS data folder \u2014 caf\u00e9"},
                {"role": "user", "content": "What about na\u00efve?"},
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8").decode("utf-8")
        self.assertIn("caf\u00e9", encoded)
        self.assertNotIn("\u00c3", encoded)
        self.assertNotIn("\ufffd", encoded)
        self.assertNotIn("\u2014".encode("utf-8").decode("latin-1"), encoded)


if __name__ == "__main__":
    unittest.main()
