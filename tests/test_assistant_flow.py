"""End-to-end checks for the AI Assistant app with a stubbed provider picker.

The picker and the provider calls are replaced with fakes so the whole "pick a
provider, load models, ask a question, hear what the model is doing" flow runs
without a desktop session, an internet connection, or a local Ollama server.

What is being pinned down here is honesty: the assistant says "Waiting for
Ollama..." when a question goes out, and every other status word it speaks must
have come from the provider. A question that fails before anything arrives is
never prefixed with "Thinking...".

Nothing said on the assistant's own initiative may cut into what is already being
read, so the fake speech API records whether each utterance was allowed to
interrupt, and the tests check that the automatic ones never are.
"""

import os
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import wx
except ImportError:  # pragma: no cover - wxPython is a required dependency
    wx = None

import netguard
import apps.assistant as assistant_module
from ai_providers import (
    STATUS_GENERATING,
    STATUS_RESEARCHING,
    STATUS_THINKING,
    STATUS_VISITING,
    GeminiProvider,
    GroqProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderError,
    content_event,
    get_model,
    get_provider_name,
    get_speak_reasoning,
    get_web,
    reasoning_event,
    set_api_key,
    set_model,
    set_speak_reasoning,
    set_web,
    status_event,
    source_event,
)


def setUpModule():
    # The assistant drives providers on worker threads, which is exactly how a
    # real request sneaks past a stubbed test and onto this computer.
    netguard.block()


def tearDownModule():
    netguard.restore()


class FakeSounds:
    def __init__(self):
        self.played = []

    def play(self, sound_name):
        self.played.append(sound_name)


FakeApp = namedtuple("FakeApp", "name description category")


class FakeDesktop:
    def __init__(self):
        self.closed_apps = []
        self.apps = []

    def on_app_closed(self, app):
        self.closed_apps.append(app)


class FakeEngine:
    """The speech engine, reduced to the one question an app may ask it."""

    def __init__(self, screen_reader=False, rate=200):
        self.screen_reader = screen_reader
        self.rate = rate
        self.stops = 0

    def uses_screen_reader(self):
        return self.screen_reader

    def stop(self):
        self.stops += 1


class FakeAPI:
    """Only the SystemAPI surface the assistant actually touches."""

    def __init__(self, screen_reader=False):
        self.spoken = []
        self.calls = []
        self.engine = FakeEngine(screen_reader=screen_reader)
        self.sounds = FakeSounds()
        self.desktop = FakeDesktop()

    def speak(self, text, interrupt=True):
        self.spoken.append(text)
        self.calls.append((text, interrupt))

    def spoken_since(self, index):
        """What was said after a moment, as ``(text, interrupt)`` pairs."""
        return self.calls[index:]

    def is_enhanced_mode(self):
        # Enhanced mode keeps focus chatter quiet, which keeps tests fast.
        return True

    def said(self, fragment):
        return any(fragment.lower() in message.lower() for message in self.spoken)

    def said_index(self, fragment):
        """Where in the spoken sequence a phrase was heard, or -1."""
        for index, message in enumerate(self.spoken):
            if fragment.lower() in message.lower():
                return index
        return -1


class StubPicker:
    """Answers the picker on the user's behalf."""

    next_provider = "ollama"
    next_api_key = ""
    cancelled = False

    def __init__(self, parent, api, current_provider=None):
        self.api = api
        self.provider_key = StubPicker.next_provider
        self.api_key = StubPicker.next_api_key

    def ShowModal(self):
        return wx.ID_CANCEL if StubPicker.cancelled else wx.ID_OK

    def Destroy(self):
        pass


class FakeStream:
    """A stand-in for a provider's ``ask_stream`` that records how it was called.

    Patching a class attribute with an instance of this works cleanly because
    calling it goes through ``__call__`` with exactly the arguments the assistant
    passed, and :attr:`last` exposes them for the assertions.
    """

    def __init__(self, *events, reply="", error=None, until_cancelled=False):
        self.events = list(events)
        self.reply = reply
        self.error = error
        self.until_cancelled = until_cancelled
        self.calls = []

    @property
    def last(self):
        return self.calls[-1] if self.calls else {}

    @property
    def called(self):
        return bool(self.calls)

    def __call__(
        self, prompt, model, system=None, on_event=None, cancel=None, web=False, history=None
    ):
        self.calls.append({
            "prompt": prompt,
            "model": model,
            "system": system,
            "cancel": cancel,
            "web": web,
            "history": history,
        })
        for event in self.events:
            if on_event is not None:
                on_event(event)
        if self.until_cancelled and cancel is not None:
            cancel.wait(5)
        if self.error is not None:
            raise self.error
        return self.reply


def key_event(code):
    event = mock.Mock()
    event.GetKeyCode.return_value = code
    return event


class AssistantFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if wx is None:
            raise unittest.SkipTest("wxPython is not installed")
        try:
            cls.app = wx.App(False)
        except Exception as exc:
            raise unittest.SkipTest(f"wxPython cannot start here: {exc}")

    def setUp(self):
        self._previous = os.environ.get("PY_OS_DATA_DIR")
        self.tmpdir = tempfile.mkdtemp()
        os.environ["PY_OS_DATA_DIR"] = self.tmpdir
        self.addCleanup(self._restore_env)

        # No screen reader unless a test says otherwise: the fake engine's
        # `screen_reader` flag is what the assistant asks about named, and it can be
        # flipped mid-test.
        self.api = FakeAPI()
        StubPicker.next_provider = "ollama"
        StubPicker.next_api_key = ""
        StubPicker.cancelled = False
        self.picker_patch = mock.patch.object(
            assistant_module, "ProviderPickerDialog", StubPicker
        )
        self.picker_patch.start()
        self.addCleanup(self.picker_patch.stop)

    def _restore_env(self):
        if self._previous is None:
            os.environ.pop("PY_OS_DATA_DIR", None)
        else:
            os.environ["PY_OS_DATA_DIR"] = self._previous

    # -- helpers ---------------------------------------------------------
    def _pump(self, condition, timeout=5.0):
        """Run queued wx events (including worker-thread callbacks)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                wx.Yield()
            except Exception:
                pass
            if condition():
                return True
            time.sleep(0.01)
        return condition()

    def _start_assistant(self, provider_key="ollama", api_key=""):
        """Boot the assistant and let its own provider picker run."""
        StubPicker.next_provider = provider_key
        StubPicker.next_api_key = api_key
        app = assistant_module.AssistantApp(self.api)
        app.run()
        self.addCleanup(self._destroy_frame, app)
        self._pump(lambda: app.provider is not None)
        return app

    def _destroy_frame(self, app):
        try:
            if app.frame and not app.frame.IsBeingDeleted():
                app.frame.Destroy()
        except Exception:
            pass

    def _ask(self, app, question):
        """Ask a question and wait for the answer to finish arriving."""
        app.input_ctrl.SetValue(question)
        app.on_ask(None)
        self._pump(lambda: not app.busy)

    def _bare_app(self):
        """An assistant whose window exists but which has no provider yet."""
        app = assistant_module.AssistantApp(self.api)
        app.run()
        self.addCleanup(self._destroy_frame, app)
        return app

    # -- model loading ---------------------------------------------------
    def test_models_load_for_the_active_provider(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3", "mistral"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: len(app.models) == 2)

        self.assertEqual(app.models, ["llama3", "mistral"])
        self.assertEqual(app.model, "llama3")
        self.assertTrue(self.api.said("Loaded 2 models"))

    def test_model_choice_is_remembered_per_provider(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3", "mistral"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: len(app.models) == 2)

        app.model_choice.SetSelection(1)
        app.on_model_change(None)
        self.assertEqual(app.model, "mistral")
        self.assertEqual(get_model("ollama"), "mistral")

    def test_offline_provider_falls_back_to_the_remembered_model(self):
        error = ProviderError("offline", "Could not reach Ollama. Make sure Ollama is running.")
        with mock.patch.object(OllamaProvider, "list_models", side_effect=error):
            app = self._start_assistant("ollama")
            self._pump(lambda: self.api.said("Using llama3"))

        self.assertEqual(app.model, "llama3")
        self.assertTrue(self.api.said("Make sure Ollama is running"))
        # The user can still ask; the assistant is not stuck.
        self.assertTrue(app.input_ctrl.IsEnabled())

    # -- asking ----------------------------------------------------------
    def test_answer_is_spoken_and_written_to_history(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(content_event("The answer is 42."), reply="The answer is 42."),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "What is the answer?")

        history = app.history.GetValue()
        self.assertIn("You: What is the answer?", history)
        self.assertIn("Ollama: The answer is 42.", history)
        self.assertTrue(self.api.said("The answer is 42."))
        self.assertEqual(app.status_label.GetLabel(), "AI Assistant — Ollama")

    def test_questions_are_sent_to_the_active_provider_with_its_model(self):
        fake = FakeStream(content_event("ok"), reply="ok")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hi")

        self.assertEqual(fake.last["prompt"], "hi")
        self.assertEqual(fake.last["model"], "llama3")

    def test_cloud_provider_uses_its_saved_key(self):
        set_api_key("gemini", "stored-key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.0-flash"]), \
                mock.patch.object(
                    GeminiProvider,
                    "ask_stream",
                    FakeStream(content_event("Gemini says hi."), reply="Gemini says hi."),
                ):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        self.assertEqual(app.provider.key, "gemini")
        self.assertEqual(app.provider.api_key, "stored-key")
        self.assertIn("Gemini says hi.", app.history.GetValue())
        self.assertIn("Gemini", app.frame.GetTitle())

    def test_selected_provider_is_remembered_for_next_launch(self):
        set_api_key("openrouter", "key")
        with mock.patch.object(OpenRouterProvider, "list_models", return_value=["openai/gpt-4o-mini"]):
            app = self._start_assistant("openrouter")
            self._pump(lambda: app.models)

        self.assertEqual(get_provider_name(), "openrouter")
        self.assertIn("OpenRouter", app.frame.GetTitle())

    # -- truthful status reporting ---------------------------------------
    def test_the_only_thing_said_up_front_is_that_the_question_is_waiting(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider, "ask_stream", FakeStream(content_event("ok"), reply="ok")
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        self.assertTrue(self.api.said("Waiting for Ollama..."))
        self.assertFalse(self.api.said("Thinking"))
        self.assertNotIn("Thinking", app.history.GetValue())

    def test_the_status_words_come_from_the_provider_in_order(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        status_event(STATUS_THINKING),
                        reasoning_event("Let me see."),
                        status_event(STATUS_GENERATING),
                        content_event("It is 42."),
                        reply="It is 42.",
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        waiting = self.api.said_index("Waiting for Ollama")
        thinking = self.api.said_index("Thinking...")
        generating = self.api.said_index("Generating response...")
        self.assertLess(waiting, thinking)
        self.assertLess(thinking, generating)
        # The status line goes back to the idle title once the answer is in.
        self.assertEqual(app.status_label.GetLabel(), "AI Assistant — Ollama")

    def test_a_question_that_fails_never_says_thinking(self):
        error = ProviderError("offline", "Connection error. Make sure Ollama is running.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", FakeStream(error=error)):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        self.assertFalse(self.api.said("Thinking"))
        self.assertTrue(self.api.said("Make sure Ollama is running"))
        self.assertIn("Error:", app.history.GetValue())

    def test_an_offline_error_when_asking_is_explained(self):
        error = ProviderError("offline", "Connection error. Make sure Ollama is running.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", FakeStream(error=error)):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        history = app.history.GetValue()
        self.assertIn("Error:", history)
        self.assertIn("Make sure Ollama is running", history)

    def test_rejected_key_points_at_the_provider_button(self):
        set_api_key("gemini", "stale-key")
        error = ProviderError("bad_key", "Gemini rejected that API key. Check the key and try again.")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.0-flash"]), \
                mock.patch.object(GeminiProvider, "ask_stream", FakeStream(error=error)):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        self.assertTrue(self.api.said("Press the Provider button"))

    def test_research_is_only_reported_when_the_provider_reports_it(self):
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(
                    GeminiProvider,
                    "ask_stream",
                    FakeStream(
                        status_event(STATUS_RESEARCHING, "pyos terminal commands"),
                        status_event(STATUS_VISITING, "https://example.test/docs"),
                        content_event("It lists files."),
                        reply="It lists files.",
                    ),
                ):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "how do I list files?")

        spoken = " ".join(self.api.spoken)
        self.assertIn("Researching...", spoken)
        self.assertIn("pyos terminal commands", spoken)
        self.assertIn("Visiting website...", spoken)
        history = app.history.GetValue()
        self.assertIn("[Researching: pyos terminal commands]", history)
        self.assertIn("[Visiting website: https://example.test/docs]", history)

    # -- reasoning narration ---------------------------------------------
    def test_reasoning_is_spoken_in_whole_sentences(self):
        self.api.engine.screen_reader = True
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        status_event(STATUS_THINKING),
                        reasoning_event("First I count the letters."),
                        reasoning_event(" Then I check"),
                        reasoning_event(" again."),
                        content_event("Two."),
                        reply="Two.",
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "how many r's?")

        self.assertTrue(self.api.said("First I count the letters."))
        self.assertIn("Then I check again.", self.api.spoken)
        # The half-sentence that was still arriving was never spoken on its own.
        self.assertNotIn("Then I check", self.api.spoken)

    def test_thinking_stays_as_text_when_no_screen_reader_is_listening(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        reasoning_event("Weighing the options."),
                        content_event("The answer."),
                        reply="The answer.",
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "which one?")

        # Nobody without a screen reader should hear thinking fragment by fragment;
        # the text is there to read, and the answer is still spoken.
        self.assertIn("Thinking: Weighing the options.", app.history.GetValue())
        self.assertFalse(self.api.said("Weighing the options"))
        self.assertTrue(self.api.said("The answer."))

    # -- never interrupting what is being read ---------------------------
    def test_nothing_said_on_its_own_initiative_interrupts_the_reading(self):
        self.api.engine.screen_reader = True
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        status_event(STATUS_THINKING),
                        reasoning_event("First I read the question."),
                        status_event(STATUS_GENERATING),
                        content_event("The answer is 42."),
                        source_event("PyOS docs", "https://example.test/docs"),
                        reply="The answer is 42.",
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            start = len(self.api.calls)
            self._ask(app, "what is the answer?")

        uttered = {text: cut for text, cut in self.api.spoken_since(start)}
        for text in (
            "Thinking...",
            "Generating response...",
            "First I read the question.",
            "The answer is 42.",
        ):
            self.assertIn(text, uttered)
            self.assertFalse(uttered[text], f"{text!r} cut into the reading")
        self.assertFalse(uttered["1 source used. They are listed in the conversation history."])
        # The queue belongs to the screen reader, so the answer is handed over and
        # read after the thinking that came first.
        order = [text for text, _ in self.api.spoken_since(start)]
        self.assertLess(
            order.index("First I read the question."), order.index("The answer is 42.")
        )

    def test_a_failure_does_not_interrupt_the_reading_either(self):
        self.api.engine.screen_reader = True
        error = ProviderError("offline", "Connection error. Make sure Ollama is running.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        reasoning_event("Working on it."),
                        error=error,
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            start = len(self.api.calls)
            self._ask(app, "hello")

        uttered = dict(self.api.spoken_since(start))
        self.assertFalse(uttered["Connection error. Make sure Ollama is running."])
        self.assertIn("Error:", app.history.GetValue())

    def test_reasoning_is_written_to_the_history_as_well(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        reasoning_event("Weighing the options."),
                        content_event("The answer."),
                        reply="The answer.",
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "which one?")

        history = app.history.GetValue()
        self.assertIn("Thinking: Weighing the options.", history)
        self.assertIn("Ollama: The answer.", history)

    def test_reasoning_can_be_kept_quiet_without_hiding_it(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        reasoning_event("Silent reasoning."),
                        content_event("The answer."),
                        reply="The answer.",
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            app.reasoning_check.SetValue(False)
            app.on_reasoning_toggle(None)
            self._ask(app, "hello")

        self.assertFalse(get_speak_reasoning())
        self.assertTrue(self.api.said("Narrate reasoning, currently off"))
        self.assertIn("Thinking: Silent reasoning.", app.history.GetValue())
        self.assertFalse(self.api.said("Silent reasoning"))

    # -- sources ---------------------------------------------------------
    def test_sources_are_counted_out_loud_and_listed(self):
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(
                    GeminiProvider,
                    "ask_stream",
                    FakeStream(
                        source_event("PyOS docs", "https://example.test/docs"),
                        source_event("Another page", "https://example.test/other"),
                        content_event("It lists files."),
                        reply="It lists files.",
                    ),
                ):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "how do I list files?")

        history = app.history.GetValue()
        self.assertIn("Sources (2):", history)
        self.assertIn("  PyOS docs — https://example.test/docs", history)
        self.assertTrue(self.api.said("2 sources used"))

    def test_one_source_is_counted_in_the_singular(self):
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(
                    GeminiProvider,
                    "ask_stream",
                    FakeStream(
                        source_event("PyOS docs", "https://example.test/docs"),
                        content_event("It lists files."),
                        reply="It lists files.",
                    ),
                ):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "how do I list files?")

        self.assertTrue(self.api.said("1 source used"))
        self.assertFalse(self.api.said("1 sources used"))

    # -- stopping --------------------------------------------------------
    def test_escape_stops_the_answer_and_keeps_what_arrived(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        status_event(STATUS_GENERATING),
                        content_event("A partial answer"),
                        reply="A partial answer",
                        until_cancelled=True,
                    ),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            app.input_ctrl.SetValue("hello")
            app.on_ask(None)
            self._pump(lambda: self.api.said("Generating response"))
            app.on_key(key_event(wx.WXK_ESCAPE))
            self._pump(lambda: not app.busy)

        self.assertTrue(self.api.said("Stopping."))
        self.assertTrue(self.api.said("Stopped."))
        # Nobody wants the answer they just stopped read back at them.
        self.assertFalse(self.api.said("A partial answer"))
        self.assertIn("A partial answer", app.history.GetValue())
        self.assertIn("(stopped)", app.history.GetValue())
        self.assertTrue(app.input_ctrl.IsEnabled())

    def test_other_keys_are_left_alone(self):
        app = self._bare_app()
        event = key_event(ord("A"))
        app.on_key(event)
        self.assertTrue(event.Skip.called)

    def test_a_second_question_while_the_first_is_running_is_refused(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(content_event("thinking about it"), reply="done", until_cancelled=True),
                ):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            app.input_ctrl.SetValue("first")
            app.on_ask(None)
            self._pump(lambda: app.busy)
            app.input_ctrl.SetValue("second")
            app.on_ask(None)

            self.assertTrue(self.api.said("Still working on the last question"))
            self.assertEqual(app.history.GetValue().count("You:"), 1)

            app.cancel_request()
            self._pump(lambda: not app.busy)

    def test_events_from_an_abandoned_question_are_ignored(self):
        app = self._bare_app()
        app.history.SetValue("")
        app.apply_events(app.request_id - 1, [content_event("stale answer")])
        self.assertEqual(app.history.GetValue(), "")

    def test_events_for_the_current_question_are_applied(self):
        app = self._bare_app()
        app.history.SetValue("")
        app.active_label = "Ollama"
        app.apply_events(app.request_id, [content_event("fresh answer")])
        self.assertIn("Ollama: fresh answer", app.history.GetValue())

    # -- one-shot fallback -----------------------------------------------
    def test_a_stream_that_fails_with_nothing_received_falls_back_once(self):
        failure = ProviderError("http", "Ollama returned error 500.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", FakeStream(error=failure)), \
                mock.patch.object(OllamaProvider, "ask", return_value="Fallback answer.") as ask:
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        ask.assert_called_once()
        self.assertIn("Ollama: Fallback answer.", app.history.GetValue())
        self.assertTrue(self.api.said("Fallback answer."))

    def test_a_stream_that_dies_after_text_arrived_is_not_retried(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(
                    OllamaProvider,
                    "ask_stream",
                    FakeStream(
                        content_event("Half an answer"),
                        error=ProviderError("http", "Ollama reported an error: boom"),
                    ),
                ), \
                mock.patch.object(OllamaProvider, "ask", return_value="Should not be used.") as ask:
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        ask.assert_not_called()
        history = app.history.GetValue()
        self.assertIn("Half an answer", history)
        self.assertIn("(interrupted)", history)
        self.assertIn("Error:", history)
        self.assertTrue(self.api.said("The answer was interrupted"))

    def test_a_rejected_key_is_not_retried_with_the_one_shot_path(self):
        error = ProviderError("bad_key", "Gemini rejected that API key.")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.0-flash"]), \
                mock.patch.object(GeminiProvider, "ask_stream", FakeStream(error=error)), \
                mock.patch.object(GeminiProvider, "ask", return_value="nope") as ask:
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        ask.assert_not_called()

    # -- capability controls ---------------------------------------------
    def test_the_web_box_follows_the_model(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        self.assertFalse(app.web_check.IsEnabled())

    def test_the_web_box_is_available_on_a_provider_that_can_research(self):
        set_api_key("gemini", "key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)

        self.assertTrue(app.web_check.IsEnabled())
        self.assertFalse(app.web_check.GetValue())

    def test_the_web_box_tracks_a_compound_model_on_groq(self):
        set_api_key("groq", "key")
        with mock.patch.object(
            GroqProvider, "list_models", return_value=["groq/compound", "llama-3.3-70b"]
        ):
            app = self._start_assistant("groq")
            self._pump(lambda: len(app.models) == 2)
            app.model_choice.SetSelection(app.models.index("llama-3.3-70b"))
            app.on_model_change(None)
            self.assertFalse(app.web_check.IsEnabled())

            app.model_choice.SetSelection(app.models.index("groq/compound"))
            app.on_model_change(None)
            self.assertTrue(app.web_check.IsEnabled())

    def test_web_research_is_remembered_for_next_time(self):
        set_api_key("gemini", "key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)

        app.web_check.SetValue(True)
        app.on_web_toggle(None)

        self.assertTrue(get_web("gemini"))
        # The state comes first, then what it will actually do for this model.
        self.assertTrue(self.api.said("Web research, currently on"))
        self.assertTrue(self.api.said("able to search Google"))
        self.assertTrue(app._web_active())

    def test_a_question_only_researches_when_the_box_is_ticked(self):
        set_api_key("gemini", "key")
        fake = FakeStream(content_event("ok"), reply="ok")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(GeminiProvider, "ask_stream", fake):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "hello")
            self.assertFalse(fake.last["web"])

            app.web_check.SetValue(True)
            app.on_web_toggle(None)
            self._ask(app, "and now?")
            self.assertTrue(fake.last["web"])

    def test_a_local_model_never_researches_even_if_the_setting_says_so(self):
        from ai_providers import set_web

        set_web("ollama", True)
        fake = FakeStream(content_event("ok"), reply="ok")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        self.assertFalse(fake.last["web"])

    # -- switching providers ---------------------------------------------
    def test_model_list_from_a_previous_provider_is_ignored(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
        previous_provider = app.provider

        set_api_key("gemini", "key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.0-flash"]):
            app.activate_provider("gemini")
            self._pump(lambda: app.models == ["gemini-2.0-flash"])

        # A slow answer from the provider we left behind must not replace the list.
        app._update_model_list(previous_provider, ["stale-model"])
        self.assertEqual(app.models, ["gemini-2.0-flash"])
        self.assertEqual(app.model, "gemini-2.0-flash")

    def test_late_reply_is_credited_to_the_provider_that_answered(self):
        app = self._bare_app()
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app.activate_provider("ollama")
            self._pump(lambda: app.models)

        app.show_response("A late answer.", False, "Ollama")
        self.assertIn("Ollama: A late answer.", app.history.GetValue())

    # -- PyOS grounding --------------------------------------------------
    def test_every_question_carries_the_pyos_reference(self):
        fake = FakeStream(content_event("ok"), reply="ok")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "how do I create a file?")

        system = fake.last["system"]
        self.assertIn("You are the AI Assistant built into PyOS", system)
        self.assertIn("create <name>", system)

    def test_the_reference_lists_the_apps_that_are_installed(self):
        self.api.desktop.apps = [FakeApp("Terminal", "Command-line interface.", "Tools")]
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        self.assertIn("- Terminal — Command-line interface. [Tools]", app.knowledge.text)

    def test_the_launch_speech_mentions_the_reference(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        self.assertTrue(self.api.said("PyOS reference loaded"))

    def test_the_reference_is_rebuilt_when_the_provider_changes(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
        first_pack = app.knowledge

        set_api_key("gemini", "key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.0-flash"]):
            app.activate_provider("gemini")
            self._pump(lambda: app.models == ["gemini-2.0-flash"])

        self.assertIsNot(app.knowledge, first_pack)
        self.assertIsNotNone(app.knowledge.section("overview"))

    def test_the_knowledge_button_shows_exactly_what_was_sent(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        with mock.patch.object(assistant_module, "KnowledgeViewerDialog") as viewer:
            app.on_knowledge_button(None)

        viewer.assert_called_once()
        shown = viewer.call_args[0][2]
        # The reference alone is no longer the whole story: the model is also
        # told who it is and whether research is on, so the viewer shows both,
        # and what it shows is what is really sent.
        self.assertIn(app.knowledge.text.strip(), shown.text)
        self.assertIn("YOU, RIGHT NOW", shown.text)
        self.assertEqual(len(shown.sections), len(app.knowledge.sections) + 1)
        self.assertEqual(shown.sections[-1].title, "You, right now")
        viewer.return_value.ShowModal.assert_called_once()

    def test_the_knowledge_button_before_choosing_a_provider_explains_itself(self):
        app = self._bare_app()

        app.on_knowledge_button(None)

        self.assertTrue(self.api.said("Choose a provider first"))

    def test_a_broken_reference_does_not_stop_the_assistant(self):
        with mock.patch.object(
            assistant_module, "build_knowledge", side_effect=RuntimeError("no disk")
        ), mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        self.assertIsNone(app.knowledge)
        self.assertTrue(app.input_ctrl.IsEnabled())
        self.assertTrue(self.api.said("Could not prepare the PyOS reference"))

    # -- guards ----------------------------------------------------------
    def test_ask_before_choosing_a_provider_is_blocked(self):
        app = self._bare_app()

        app.input_ctrl.SetValue("hello")
        app.on_ask(None)

        self.assertTrue(self.api.said("Choose a provider first"))
        self.assertEqual(app.history.GetValue(), "")

    def test_input_is_disabled_until_a_provider_is_chosen(self):
        app = self._bare_app()

        self.assertFalse(app.input_ctrl.IsEnabled())
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app.activate_provider("ollama")
            self._pump(lambda: app.models)
        self.assertTrue(app.input_ctrl.IsEnabled())

    def test_cancelling_the_picker_closes_the_assistant(self):
        app = self._bare_app()
        StubPicker.cancelled = True
        app.choose_provider()

        self.assertTrue(self.api.said("No provider selected"))
        self.assertIn(app, self.api.desktop.closed_apps)
        self.assertIn("close", self.api.sounds.played)

    # -- conversation memory ---------------------------------------------
    def test_the_first_question_carries_no_conversation(self):
        fake = FakeStream(content_event("It lists files."), reply="It lists files.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "what does list do?")

        self.assertEqual(fake.last["history"], [])

    def test_a_follow_up_question_carries_the_earlier_exchange(self):
        fake = FakeStream(content_event("It lists files."), reply="It lists files.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "what does list do?")
            self._ask(app, "and open?")

        self.assertEqual(
            fake.last["history"],
            [
                {"role": "user", "text": "what does list do?"},
                {"role": "assistant", "text": "It lists files."},
            ],
        )

    def test_clearing_the_conversation_forgets_it_and_says_so(self):
        fake = FakeStream(content_event("Yes."), reply="Yes.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "first")
            self.assertEqual(len(app.conversation), 2)

            app.on_clear_conversation(None)
            self.assertEqual(app.conversation, [])
            self.assertTrue(self.api.said("Conversation cleared"))

            self._ask(app, "second")

        self.assertEqual(fake.last["history"], [])

    def test_the_clear_button_reports_how_much_is_remembered(self):
        fake = FakeStream(content_event("Yes."), reply="Yes.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

            app.on_clear_focus(mock.Mock())
            self.assertTrue(self.api.said("Nothing is remembered yet"))

            self._ask(app, "first")
            app.on_clear_focus(mock.Mock())

        self.assertTrue(self.api.said("1 exchange remembered"))

    def test_switching_provider_forgets_the_conversation(self):
        fake = FakeStream(content_event("Yes."), reply="Yes.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "first")

        set_api_key("gemini", "key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]):
            app.activate_provider("gemini")
            self._pump(lambda: app.models)

        self.assertEqual(app.conversation, [])

    def test_a_question_that_failed_is_not_remembered(self):
        fake = FakeStream(error=ProviderError("http", "Groq returned error 500."))
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "a question that fails")

        self.assertEqual(app.conversation, [])

    def test_a_stopped_question_that_answered_nothing_is_not_remembered(self):
        fake = FakeStream(until_cancelled=True)
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            app.input_ctrl.SetValue("never answered")
            app.on_ask(None)
            self._pump(lambda: app.busy)
            app.cancel_request()
            self._pump(lambda: not app.busy)

        self.assertEqual(app.conversation, [])

    def test_a_stopped_question_keeps_the_part_that_arrived(self):
        fake = FakeStream(
            content_event("The first half of the answer."),
            reply="The first half of the answer.",
        )
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "half an answer")

        self.assertEqual(
            [turn["role"] for turn in app.conversation], ["user", "assistant"]
        )

    def test_the_remembered_conversation_never_reaches_the_disk(self):
        fake = FakeStream(content_event("Yes."), reply="Yes.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "a secret question")

        written = []
        for name in os.listdir(self.tmpdir):
            path = os.path.join(self.tmpdir, name)
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    written.append(handle.read())
        self.assertFalse(any("a secret question" in text for text in written))

    # -- research honesty -------------------------------------------------
    def test_a_look_up_question_says_research_is_off_before_it_is_sent(self):
        set_api_key("gemini", "key")
        fake = FakeStream(content_event("From memory."), reply="From memory.")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(GeminiProvider, "ask_stream", fake):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "look up the latest NVDA release")

        self.assertTrue(self.api.said("Web research is off"))
        self.assertFalse(fake.last["web"])

    def test_an_ordinary_question_is_not_interrupted_with_a_warning(self):
        set_api_key("gemini", "key")
        fake = FakeStream(content_event("It lists files."), reply="It lists files.")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(GeminiProvider, "ask_stream", fake):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "what does list do in the Terminal")

        self.assertFalse(self.api.said("Web research is off"))

    def test_research_on_with_no_search_reported_says_so(self):
        set_api_key("gemini", "key")
        fake = FakeStream(content_event("From memory."), reply="From memory.")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(GeminiProvider, "ask_stream", fake):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            app.web_check.SetValue(True)
            app.on_web_toggle(None)
            self._ask(app, "research the news")

        self.assertTrue(fake.last["web"])
        self.assertTrue(self.api.said("did not report using the web"))
        self.assertIn("No web search or page visit", app.history.GetValue())

    def test_research_on_with_a_search_reported_says_nothing_extra(self):
        set_api_key("gemini", "key")
        fake = FakeStream(
            status_event(STATUS_RESEARCHING, "NVDA release"),
            content_event("The latest release is 2026.1."),
            reply="The latest release is 2026.1.",
        )
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]), \
                mock.patch.object(GeminiProvider, "ask_stream", fake):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            app.web_check.SetValue(True)
            app.on_web_toggle(None)
            self._ask(app, "research the news")

        self.assertFalse(self.api.said("did not report using the web"))

    # -- the on/off boxes -------------------------------------------------
    def test_a_model_that_cannot_research_puts_the_research_box_back(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        app.web_check.SetValue(True)
        app.on_web_toggle(None)

        # The click cannot be honoured, so nothing claims that it was.
        self.assertFalse(app.web_check.GetValue())
        self.assertFalse(app.web_enabled)
        self.assertFalse(get_web("ollama"))
        self.assertTrue(self.api.said("not available for llama3"))

    def test_a_model_list_arriving_late_does_not_undo_the_choice(self):
        set_api_key("groq", "key")
        with mock.patch.object(GroqProvider, "list_models", return_value=["groq/compound"]):
            app = self._start_assistant("groq")
            self._pump(lambda: app.models)

        app.web_check.SetValue(True)
        app.on_web_toggle(None)
        self.assertTrue(app.web_check.GetValue())

        # A refresh that reports a model with no web tools used to flip the box
        # off in front of the user while the setting stayed on.
        with mock.patch.object(GroqProvider, "list_models", return_value=["llama-3.3-70b"]):
            app._update_model_list(app.provider, ["llama-3.3-70b"])

        self.assertTrue(app.web_check.GetValue())
        self.assertTrue(get_web("groq"))
        self.assertFalse(app._web_active())
        self.assertTrue(self.api.said("cannot research"))

    def test_the_research_box_says_its_state_before_anything_else(self):
        set_api_key("gemini", "key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)

        app.on_web_focus(mock.Mock())
        self.assertTrue(self.api.said("Web research, currently off"))

        app.web_check.SetValue(True)
        app.on_web_toggle(None)
        app.on_web_focus(mock.Mock())
        self.assertTrue(self.api.said("Web research, currently on"))

    def test_the_reasoning_box_says_its_state_before_anything_else(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        app.on_reasoning_focus(mock.Mock())
        self.assertTrue(self.api.said("Narrate reasoning, currently on"))

        app.reasoning_check.SetValue(False)
        app.on_reasoning_toggle(None)
        app.on_reasoning_focus(mock.Mock())
        self.assertTrue(self.api.said("Narrate reasoning, currently off"))

    def test_turning_research_on_off_always_states_the_new_state(self):
        set_api_key("gemini", "key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.5-flash"]):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)

        app.web_check.SetValue(True)
        app.on_web_toggle(None)
        self.assertTrue(self.api.said("Web research, currently on"))

        app.web_check.SetValue(False)
        app.on_web_toggle(None)
        self.assertTrue(self.api.said("Web research, currently off"))
        self.assertFalse(get_web("gemini"))

    def test_a_research_setting_left_on_is_shown_as_on_even_without_web_tools(self):
        set_web("ollama", True, os.path.join(self.tmpdir, "ai_config.json"))
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        # The box is greyed out because this model cannot research, but it still
        # shows what is really stored rather than contradicting it.
        self.assertTrue(app.web_check.GetValue())
        self.assertFalse(app.web_check.IsEnabled())
        app.on_web_focus(mock.Mock())
        self.assertTrue(self.api.said("currently on, but llama3 cannot research"))

    def test_every_mnemonic_in_the_window_is_unique(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)

        seen = {}
        for name in dir(app):
            control = getattr(app, name, None)
            label = getattr(control, "GetLabel", None)
            if not callable(label):
                continue
            text = label()
            for index, character in enumerate(text[:-1]):
                if character == "&":
                    key = text[index + 1].upper()
                    seen.setdefault(key, []).append(text)
        clashes = {key: names for key, names in seen.items() if len(names) > 1}
        self.assertEqual(clashes, {})

    # -- damaged text -----------------------------------------------------
    def test_text_that_had_to_be_repaired_is_mentioned_once(self):
        fake = FakeStream(
            content_event("caf\u00c3\u00a9 cr\u00c3\u00a8me"),
            reply="caf\u00c3\u00a9 cr\u00c3\u00a8me",
        )
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "what is the cafe called?")

        # Repaired in the history, and said out loud rather than passing silently.
        self.assertIn("caf\u00e9 cr\u00e8me", app.history.GetValue())
        self.assertTrue(self.api.said("could not be shown exactly"))

    def test_a_clean_answer_is_not_apologised_for(self):
        fake = FakeStream(content_event("All fine here."), reply="All fine here.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask_stream", fake):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        self.assertFalse(self.api.said("could not be shown exactly"))


if __name__ == "__main__":
    unittest.main()
