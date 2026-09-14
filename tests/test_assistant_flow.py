"""End-to-end checks for the AI Assistant app with a stubbed provider picker.

The picker and the provider network calls are replaced with fakes so the whole
"pick a provider, load models, ask a question, hear the answer" flow runs
without a desktop session or an internet connection.
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

import apps.assistant as assistant_module
from ai_providers import (
    GeminiProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderError,
    get_model,
    get_provider_name,
    set_api_key,
)


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


class FakeAPI:
    """Only the SystemAPI surface the assistant actually touches."""

    def __init__(self):
        self.spoken = []
        self.sounds = FakeSounds()
        self.desktop = FakeDesktop()

    def speak(self, text, interrupt=True):
        self.spoken.append(text)

    def is_enhanced_mode(self):
        # Enhanced mode keeps focus chatter quiet, which keeps tests fast.
        return True

    def said(self, fragment):
        return any(fragment.lower() in message.lower() for message in self.spoken)


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
        app.input_ctrl.SetValue(question)
        app.on_ask(None)
        self._pump(lambda: len(app.history.GetValue().splitlines()) >= 2)

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
                mock.patch.object(OllamaProvider, "ask", return_value="The answer is 42."):
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "What is the answer?")

        history = app.history.GetValue()
        self.assertIn("You: What is the answer?", history)
        self.assertIn("Ollama: The answer is 42.", history)
        self.assertTrue(self.api.said("The answer is 42."))
        self.assertEqual(app.status_label.GetLabel(), "AI Assistant — Ollama")

    def test_questions_are_sent_to_the_active_provider_with_its_model(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask", return_value="ok") as ask:
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "hi")

        self.assertEqual(ask.call_args[0], ("hi", "llama3"))

    def test_cloud_provider_uses_its_saved_key(self):
        set_api_key("gemini", "stored-key")
        with mock.patch.object(GeminiProvider, "list_models", return_value=["gemini-2.0-flash"]), \
                mock.patch.object(GeminiProvider, "ask", return_value="Gemini says hi."):
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

    def test_offline_error_when_asking_is_explained(self):
        error = ProviderError("offline", "Connection error. Make sure Ollama is running.")
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask", side_effect=error):
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
                mock.patch.object(GeminiProvider, "ask", side_effect=error):
            app = self._start_assistant("gemini")
            self._pump(lambda: app.models)
            self._ask(app, "hello")

        self.assertTrue(self.api.said("Press the Provider button"))

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
        app = assistant_module.AssistantApp(self.api)
        app.run()
        self.addCleanup(self._destroy_frame, app)
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app.activate_provider("ollama")
            self._pump(lambda: app.models)

        app.show_response("A late answer.", False, "Ollama")
        self.assertIn("Ollama: A late answer.", app.history.GetValue())

    # -- PyOS grounding --------------------------------------------------
    def test_every_question_carries_the_pyos_reference(self):
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]), \
                mock.patch.object(OllamaProvider, "ask", return_value="ok") as ask:
            app = self._start_assistant("ollama")
            self._pump(lambda: app.models)
            self._ask(app, "how do I create a file?")

        system = ask.call_args[1]["system"]
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
        self.assertIs(viewer.call_args[0][2], app.knowledge)
        viewer.return_value.ShowModal.assert_called_once()

    def test_the_knowledge_button_before_choosing_a_provider_explains_itself(self):
        app = assistant_module.AssistantApp(self.api)
        app.run()
        self.addCleanup(self._destroy_frame, app)

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
        app = assistant_module.AssistantApp(self.api)
        app.run()
        self.addCleanup(self._destroy_frame, app)

        app.input_ctrl.SetValue("hello")
        app.on_ask(None)

        self.assertTrue(self.api.said("Choose a provider first"))
        self.assertEqual(app.history.GetValue(), "")

    def test_input_is_disabled_until_a_provider_is_chosen(self):
        app = assistant_module.AssistantApp(self.api)
        app.run()
        self.addCleanup(self._destroy_frame, app)

        self.assertFalse(app.input_ctrl.IsEnabled())
        with mock.patch.object(OllamaProvider, "list_models", return_value=["llama3"]):
            app.activate_provider("ollama")
            self._pump(lambda: app.models)
        self.assertTrue(app.input_ctrl.IsEnabled())

    def test_cancelling_the_picker_closes_the_assistant(self):
        app = assistant_module.AssistantApp(self.api)
        app.run()
        StubPicker.cancelled = True
        app.choose_provider()

        self.assertTrue(self.api.said("No provider selected"))
        self.assertIn(app, self.api.desktop.closed_apps)
        self.assertIn("close", self.api.sounds.played)


if __name__ == "__main__":
    unittest.main()
