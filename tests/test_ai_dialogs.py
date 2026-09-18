import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import wx
except ImportError:  # pragma: no cover - wxPython is a hard requirement of PyOS
    wx = None

import netguard
import ai_provider_dialog
from pyos_knowledge import build_knowledge
from ai_providers import (
    PROVIDER_ORDER,
    AIProvider,
    ProviderError,
    get_api_key,
    get_provider_name,
    set_api_key,
)


def setUpModule():
    # The key dialog checks a key against the provider on a worker thread, so the
    # guard matters here too.
    netguard.block()


def tearDownModule():
    netguard.restore()


class FakeAPI:
    """Minimal stand-in for SystemAPI: records speech instead of talking."""

    def __init__(self):
        self.spoken = []

    def speak(self, text, interrupt=True):
        self.spoken.append(text)

    def is_enhanced_mode(self):
        # Enhanced mode keeps focus chatter quiet, which keeps these tests fast.
        return True


class StubProvider(AIProvider):
    key = "gemini"
    label = "Google Gemini"
    description = "A stub provider for dialog tests."
    requires_api_key = True
    default_model = "gemini-2.0-flash"

    def __init__(self, api_key=None, verify_error=None):
        super().__init__(api_key)
        self.verify_error = verify_error
        self.verified_keys = []

    def verify_key(self, api_key=None):
        self.verified_keys.append(api_key)
        if self.verify_error:
            raise self.verify_error
        return True


class StubKeyDialog:
    """Stands in for ApiKeyDialog so the picker can be tested without modals."""

    def __init__(self, parent, api, provider, result=wx.ID_CANCEL if wx else 0, key=""):
        self.result = result
        self.saved_key = key
        self.destroyed = False

    def ShowModal(self):
        return self.result

    def Destroy(self):
        self.destroyed = True


def _setup_wx():
    if wx is None:
        return None
    try:
        app = wx.App(False)
    except Exception as exc:  # no display, missing backend, and so on
        raise unittest.SkipTest(f"wxPython cannot start here: {exc}")
    return app


class DialogTestCase(unittest.TestCase):
    api = None

    @classmethod
    def setUpClass(cls):
        cls.app = _setup_wx()

    def setUp(self):
        if wx is None:
            self.skipTest("wxPython is not installed")
        self._previous = os.environ.get("PY_OS_DATA_DIR")
        self.tmpdir = tempfile.mkdtemp()
        os.environ["PY_OS_DATA_DIR"] = self.tmpdir
        self.addCleanup(self._restore_env)
        self.api = FakeAPI()
        self.parent = wx.Frame(None)

    def _restore_env(self):
        if self._previous is None:
            os.environ.pop("PY_OS_DATA_DIR", None)
        else:
            os.environ["PY_OS_DATA_DIR"] = self._previous
        self.parent.Destroy()


class ProviderPickerTests(DialogTestCase):
    def _picker(self, current_provider=None):
        dialog = ai_provider_dialog.ProviderPickerDialog(
            self.parent, self.api, current_provider=current_provider
        )
        dialog.EndModal = lambda result: setattr(dialog, "ended_with", result)
        self.addCleanup(dialog.Destroy)
        return dialog

    def test_lists_every_provider_in_order(self):
        dialog = self._picker()
        choices = [dialog.radio.GetString(i) for i in range(dialog.radio.GetCount())]
        self.assertEqual(len(choices), len(PROVIDER_ORDER))
        self.assertIn("Ollama", choices[0])
        self.assertIn("no API key needed", choices[0])
        self.assertIn("API key required", choices[1])

    def test_saved_key_shows_as_saved(self):
        set_api_key("gemini", "stored-key")
        dialog = self._picker()
        self.assertIn("API key saved", dialog.radio.GetString(1))

    def test_groq_is_offered_last_and_asks_for_a_key(self):
        dialog = self._picker()
        last = dialog.radio.GetString(dialog.radio.GetCount() - 1)
        self.assertIn("Groq", last)
        self.assertIn("API key required", last)

    def test_the_note_explains_the_key_rules_for_every_provider(self):
        dialog = self._picker()
        note = ai_provider_dialog._provider_note(ai_provider_dialog._provider_rows())

        shown = [
            child.GetLabel()
            for child in dialog.panel.GetChildren()
            if isinstance(child, wx.StaticText)
        ]

        self.assertIn(note, shown)
        for label in ("Ollama", "Google Gemini", "OpenRouter", "Groq"):
            self.assertIn(label, note)

    def test_the_greeting_names_every_provider(self):
        dialog = self._picker()
        self.api.spoken.clear()

        dialog._greet()

        greeting = " ".join(self.api.spoken)
        for label in ("Ollama", "Google Gemini", "OpenRouter", "Groq"):
            self.assertIn(label, greeting)

    def test_remembered_provider_is_preselected(self):
        dialog = self._picker(current_provider="openrouter")
        self.assertEqual(dialog.radio.GetSelection(), PROVIDER_ORDER.index("openrouter"))

    def test_ollama_continues_without_a_key(self):
        dialog = self._picker(current_provider="ollama")
        dialog.on_continue(None)

        self.assertEqual(dialog.ended_with, wx.ID_OK)
        self.assertEqual(dialog.provider_key, "ollama")
        self.assertEqual(dialog.api_key, "")

    def test_cloud_provider_asks_for_a_key_when_none_is_saved(self):
        dialog = self._picker(current_provider="gemini")
        stub = StubKeyDialog(None, self.api, None, result=wx.ID_CANCEL)
        with mock.patch.object(ai_provider_dialog, "ApiKeyDialog", return_value=stub):
            dialog.on_continue(None)

        self.assertTrue(stub.destroyed)
        self.assertIsNone(dialog.provider_key)
        self.assertFalse(hasattr(dialog, "ended_with"))
        self.assertIn("needs an API key", dialog.status.GetLabel())

    def test_cloud_provider_continues_once_a_key_is_saved(self):
        dialog = self._picker(current_provider="gemini")
        stub = StubKeyDialog(
            None, self.api, None, result=wx.ID_OK, key="fresh-key"
        )
        with mock.patch.object(ai_provider_dialog, "ApiKeyDialog", return_value=stub):
            set_api_key("gemini", "fresh-key")
            dialog.on_continue(None)

        self.assertEqual(dialog.ended_with, wx.ID_OK)
        self.assertEqual(dialog.provider_key, "gemini")
        self.assertEqual(dialog.api_key, "fresh-key")

    def test_clear_key_button_forgets_the_selected_key(self):
        set_api_key("gemini", "stored-key")
        dialog = self._picker(current_provider="gemini")

        dialog.on_clear_key(None)

        self.assertEqual(get_api_key("gemini"), "")
        self.assertIn("API key required", dialog.radio.GetString(1))

    def test_clear_key_button_is_harmless_for_ollama(self):
        dialog = self._picker(current_provider="ollama")
        dialog.on_clear_key(None)
        self.assertIn("does not use an API key", " ".join(self.api.spoken))


class ApiKeyDialogTests(DialogTestCase):
    def _dialog(self, verify_error=None, existing=""):
        if existing:
            set_api_key("gemini", existing)
        provider = StubProvider(verify_error=verify_error)
        dialog = ai_provider_dialog.ApiKeyDialog(self.parent, self.api, provider)
        dialog.EndModal = lambda result: setattr(dialog, "ended_with", result)
        self.addCleanup(dialog.Destroy)
        return dialog

    def test_prefills_a_saved_key_for_review(self):
        dialog = self._dialog(existing="saved-key")
        self.assertEqual(dialog.key_input.GetValue(), "saved-key")

    def test_empty_key_is_not_saved(self):
        dialog = self._dialog()
        dialog.key_input.SetValue("   ")
        dialog.on_save(None)

        self.assertEqual(dialog.saved_key, "")
        self.assertFalse(hasattr(dialog, "ended_with"))
        self.assertIn("Type or paste", " ".join(self.api.spoken))

    def test_valid_key_is_stored_and_reported(self):
        dialog = self._dialog()
        dialog.on_save(None)
        # The check runs on a worker thread; drive the result directly so the
        # test stays deterministic.
        dialog._on_verified("good-key", None)

        self.assertEqual(dialog.saved_key, "good-key")
        self.assertEqual(get_api_key("gemini"), "good-key")
        self.assertEqual(dialog.ended_with, wx.ID_OK)
        self.assertIn("API key saved", " ".join(self.api.spoken))

    def test_rejected_key_is_not_stored(self):
        dialog = self._dialog(verify_error=ProviderError("bad_key", "Gemini rejected that API key."))
        dialog._on_verified("wrong-key", ProviderError("bad_key", "Gemini rejected that API key."))

        self.assertEqual(dialog.saved_key, "")
        self.assertEqual(get_api_key("gemini"), "")
        self.assertFalse(hasattr(dialog, "ended_with"))
        self.assertIn("rejected", dialog.status.GetLabel())

    def test_offline_check_offers_to_save_anyway(self):
        dialog = self._dialog()
        dialog._confirm_save_anyway = lambda message: True
        dialog._on_verified("maybe-key", ProviderError("offline", "Could not reach Gemini."))

        self.assertEqual(get_api_key("gemini"), "maybe-key")

    def test_declining_the_offline_prompt_saves_nothing(self):
        dialog = self._dialog()
        dialog._confirm_save_anyway = lambda message: False
        dialog._on_verified("maybe-key", ProviderError("offline", "Could not reach Gemini."))

        self.assertEqual(dialog.saved_key, "")
        self.assertEqual(get_api_key("gemini"), "")
        self.assertIn("not saved", " ".join(self.api.spoken))

    def test_cancel_returns_without_a_key(self):
        dialog = self._dialog()
        dialog.on_cancel(None)
        self.assertEqual(dialog.saved_key, "")
        self.assertEqual(dialog.ended_with, wx.ID_CANCEL)

    def test_save_is_blocked_while_a_check_is_running(self):
        dialog = self._dialog()
        dialog._busy = True
        dialog.on_save(None)
        self.assertEqual(dialog.saved_key, "")


class KnowledgeViewerTests(DialogTestCase):
    """The user can hear exactly what the assistant tells the model about PyOS."""

    def _dialog(self, **kwargs):
        pack = build_knowledge(
            apps=[("Terminal", "Command-line interface to the system kernel.", "Tools")],
            shells=["bash"],
            **kwargs
        )
        dialog = ai_provider_dialog.KnowledgeViewerDialog(self.parent, self.api, pack)
        dialog.EndModal = lambda result: setattr(dialog, "ended_with", result)
        self.addCleanup(dialog.Destroy)
        return dialog, pack

    def test_lists_every_section(self):
        dialog, pack = self._dialog()
        titles = [dialog.list.GetString(i) for i in range(dialog.list.GetCount())]
        self.assertEqual(titles, pack.titles)

    def test_the_first_section_is_readable_as_text(self):
        dialog, pack = self._dialog()
        self.assertIn("simulates an operating system", dialog.text.GetValue())

    def test_reading_a_section_speaks_its_title_and_text(self):
        dialog, pack = self._dialog()
        dialog.list.SetSelection(1)
        dialog.on_select(None)
        self.api.spoken.clear()

        dialog.on_read(None)

        section = pack.sections[1]
        spoken = " ".join(self.api.spoken)
        self.assertIn(section.title, spoken)
        self.assertIn(section.text[:60], spoken)

    def test_summary_names_the_revision_and_size(self):
        dialog, pack = self._dialog()
        label = dialog.summary.GetLabel()

        self.assertIn(pack.fingerprint, label)
        self.assertIn(str(pack.char_count), label)
        self.assertIn(str(len(pack.sections)), label)

    def test_summary_reports_sections_left_out_for_a_tight_budget(self):
        dialog, pack = self._dialog(max_chars=6000)
        label = dialog.summary.GetLabel()

        self.assertIn("Left out to fit this model", label)
        self.assertIn("The AI Assistant itself", label)

    def test_speak_all_covers_every_section(self):
        dialog, pack = self._dialog()
        self.api.spoken.clear()

        dialog.on_speak_all(None)

        spoken = " ".join(self.api.spoken)
        for section in pack.sections:
            self.assertIn(section.title, spoken)

    def test_copy_all_puts_the_reference_on_the_clipboard(self):
        dialog, pack = self._dialog()
        clipboard = mock.MagicMock()
        clipboard.Open.return_value = True

        with mock.patch.object(ai_provider_dialog.wx, "TheClipboard", clipboard):
            dialog.on_copy_all(None)

        copied = clipboard.SetData.call_args[0][0]
        self.assertEqual(copied.GetText(), pack.text)
        self.assertIn("copied to the clipboard", " ".join(self.api.spoken))

    def test_a_clipboard_that_will_not_open_is_reported(self):
        dialog, pack = self._dialog()
        clipboard = mock.MagicMock()
        clipboard.Open.return_value = False

        with mock.patch.object(ai_provider_dialog.wx, "TheClipboard", clipboard):
            dialog.on_copy_all(None)

        self.assertIn("Could not open the clipboard", " ".join(self.api.spoken))

    def test_selecting_a_section_speaks_its_title_when_not_enhanced(self):
        dialog, pack = self._dialog()
        self.api.is_enhanced_mode = lambda: False
        dialog.list.SetSelection(0)
        self.api.spoken.clear()

        dialog.on_select(None)

        self.assertIn(pack.titles[0], " ".join(self.api.spoken))

    def test_enhanced_mode_keeps_selection_quiet(self):
        dialog, pack = self._dialog()
        self.api.spoken.clear()
        dialog.list.SetSelection(0)

        dialog.on_select(None)

        self.assertEqual(self.api.spoken, [])

    def test_reading_with_nothing_selected_is_explained(self):
        dialog, pack = self._dialog()
        dialog.list.SetSelection(wx.NOT_FOUND)
        self.api.spoken.clear()

        dialog.on_read(None)

        self.assertIn("Select a section first", " ".join(self.api.spoken))

    def test_close_button_ends_the_dialog(self):
        dialog, pack = self._dialog()

        dialog.on_close_button(None)

        self.assertEqual(dialog.ended_with, wx.ID_OK)


class ProviderOrderTest(DialogTestCase):
    def test_assistant_saves_the_chosen_provider(self):
        # The assistant persists the pick through set_provider_name; make sure a
        # round trip through the dialog's provider_key works with it.
        from ai_providers import set_provider_name

        dialog = ai_provider_dialog.ProviderPickerDialog(self.parent, self.api)
        dialog.EndModal = lambda result: None
        self.addCleanup(dialog.Destroy)

        dialog.on_continue(None)  # Ollama is preselected by default
        set_provider_name(dialog.provider_key)
        self.assertEqual(get_provider_name(), "ollama")


if __name__ == "__main__":
    unittest.main()
