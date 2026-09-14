"""Accessible dialogs that ask which AI provider to use, collect API keys, and
read back the PyOS reference the assistant sends with every question.

Kept separate from :mod:`ai_providers` so the provider logic stays importable
without wxPython (and therefore testable headless), mirroring how
``oobe_wizard.py`` sits apart from the core services.
"""

import threading

import wx

from ai_providers import (
    PROVIDER_ORDER,
    ProviderError,
    clear_api_key,
    get_api_key,
    get_provider_name,
    list_providers,
    set_api_key,
)


def _provider_rows():
    """Return provider instances whose status reflects the stored keys."""
    return list_providers()


class ProviderPickerDialog(wx.Dialog):
    """Ask which AI provider to use. Ollama continues immediately; the cloud
    providers chain into :class:`ApiKeyDialog` when no key is stored yet.

    The dialog does not destroy itself. Callers use ``ShowModal()`` and then
    read ``provider_key`` / ``api_key`` when the result is ``wx.ID_OK``.
    """

    def __init__(self, parent, api, current_provider=None):
        super().__init__(
            parent,
            title="Choose an AI Provider",
            size=(600, 470),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )
        self.api = api
        self.current_provider = current_provider or get_provider_name()
        self.provider_key = None
        self.api_key = ""

        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(wx.Colour(0, 51, 153))
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.panel.SetSizer(self.sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self._build()
        self.Centre()
        wx.CallAfter(self._greet)

    # -- layout ----------------------------------------------------------
    def _build(self):
        self.sizer.Clear(True)
        self.providers = _provider_rows()

        title = wx.StaticText(self.panel, label="Which AI provider do you want to use?")
        title.SetFont(
            wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        )
        title.SetForegroundColour(wx.Colour(255, 255, 255))
        self.sizer.Add(title, 0, wx.ALL | wx.CENTER, 12)

        note = wx.StaticText(
            self.panel,
            label=(
                "Ollama runs on this computer and needs no API key.\n"
                "Gemini and OpenRouter ask for an API key once and save it,\n"
                "so next time you can just pick the provider and start."
            ),
        )
        note.SetForegroundColour(wx.Colour(220, 220, 255))
        self.sizer.Add(note, 0, wx.ALL | wx.CENTER, 8)

        labels = [f"{p.label} — {p.status_text()}" for p in self.providers]
        self.radio = wx.RadioBox(
            self.panel,
            label="Provider",
            choices=labels,
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        self.radio.SetBackgroundColour(wx.Colour(240, 240, 240))
        self.radio.SetForegroundColour(wx.Colour(0, 0, 0))

        selected = 0
        if self.current_provider in PROVIDER_ORDER:
            selected = PROVIDER_ORDER.index(self.current_provider)
        self.radio.SetSelection(selected)
        self.radio.Bind(wx.EVT_RADIOBOX, self.on_provider_changed)
        self.radio.Bind(wx.EVT_SET_FOCUS, self.on_radio_focus)
        self.sizer.Add(self.radio, 0, wx.EXPAND | wx.ALL, 12)

        self.status = wx.StaticText(self.panel, label="")
        self.status.SetForegroundColour(wx.Colour(255, 255, 200))
        self.sizer.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        continue_btn = wx.Button(self.panel, label="&Continue")
        continue_btn.Bind(wx.EVT_BUTTON, self.on_continue)
        buttons.Add(continue_btn, 0, wx.ALL, 5)

        clear_btn = wx.Button(self.panel, label="Clear Saved &Key")
        clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_key)
        buttons.Add(clear_btn, 0, wx.ALL, 5)

        cancel_btn = wx.Button(self.panel, label="Cancel")
        cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel)
        buttons.Add(cancel_btn, 0, wx.ALL, 5)

        self.sizer.Add(buttons, 0, wx.ALL | wx.CENTER, 10)
        self.panel.Layout()
        self._update_status()

    def _update_status(self):
        provider = self.providers[self.radio.GetSelection()]
        self.status.SetLabel(f"{provider.label}: {provider.description}")
        self.panel.Layout()

    def _greet(self):
        self.api.speak(
            "Which AI provider do you want to use? "
            "Use the arrow keys to choose between Ollama, Google Gemini and OpenRouter, "
            "then press Continue."
        )
        provider = self.providers[self.radio.GetSelection()]
        if provider.requires_api_key and provider.api_key:
            self.api.speak(
                f"{provider.label} is already selected and its saved API key will be used.",
                interrupt=False,
            )
        self.radio.SetFocus()

    # -- events ----------------------------------------------------------
    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.on_cancel(None)
        else:
            event.Skip()

    def on_radio_focus(self, event):
        self.api.speak("Provider selection")
        event.Skip()

    def on_provider_changed(self, event):
        provider = self.providers[self.radio.GetSelection()]
        self._update_status()
        if not self.api.is_enhanced_mode():
            self.api.speak(f"{provider.label}. {provider.description}", interrupt=False)

    def on_clear_key(self, event):
        provider = self.providers[self.radio.GetSelection()]
        if not provider.requires_api_key:
            self.api.speak(f"{provider.label} does not use an API key.")
            return
        if not provider.api_key:
            self.api.speak(f"There is no saved API key for {provider.label}.")
            return
        clear_api_key(provider.key)
        self._build()
        self.radio.SetFocus()
        self.api.speak(f"Saved {provider.label} API key removed.")

    def on_continue(self, event):
        provider = self.providers[self.radio.GetSelection()]

        if provider.requires_api_key and not get_api_key(provider.key):
            key_dialog = ApiKeyDialog(self, self.api, provider)
            result = key_dialog.ShowModal()
            saved_key = key_dialog.saved_key if result == wx.ID_OK else ""
            key_dialog.Destroy()
            if not saved_key:
                self.status.SetLabel(f"{provider.label} still needs an API key.")
                self.panel.Layout()
                self.api.speak(
                    f"{provider.label} needs an API key before it can be used. "
                    "Choose Ollama instead, or try the key again."
                )
                self.radio.SetFocus()
                return

        self.provider_key = provider.key
        self.api_key = get_api_key(provider.key)
        self.EndModal(wx.ID_OK)

    def on_cancel(self, event):
        self.provider_key = None
        self.EndModal(wx.ID_CANCEL)


class ApiKeyDialog(wx.Dialog):
    """Collect and check one provider API key, then store it locally."""

    def __init__(self, parent, api, provider):
        super().__init__(
            parent,
            title=f"{provider.label} API Key",
            size=(600, 340),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )
        self.api = api
        self.provider = provider
        self.saved_key = ""
        self._busy = False

        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer = sizer

        heading = wx.StaticText(self.panel, label=f"Enter your {provider.label} API key")
        heading.SetFont(
            wx.Font(15, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        )
        heading.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(heading, 0, wx.ALL | wx.CENTER, 12)

        note = wx.StaticText(
            self.panel,
            label=(
                f"{provider.description}\n\n"
                "The key is saved in plain text in your PyOS data folder, so you only\n"
                "enter it once and can start the assistant with a single click after that."
            ),
        )
        note.SetForegroundColour(wx.Colour(200, 200, 255))
        sizer.Add(note, 0, wx.ALL | wx.CENTER, 8)

        existing = get_api_key(provider.key)
        self.key_input = wx.TextCtrl(self.panel, value=existing, style=wx.TE_PROCESS_ENTER)
        self.key_input.SetBackgroundColour(wx.Colour(30, 30, 60))
        self.key_input.SetForegroundColour(wx.Colour(255, 255, 255))
        self.key_input.Bind(wx.EVT_TEXT_ENTER, self.on_save)
        self.key_input.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("API key field"))
        sizer.Add(self.key_input, 0, wx.EXPAND | wx.ALL, 12)

        self.status = wx.StaticText(self.panel, label="")
        self.status.SetForegroundColour(wx.Colour(255, 255, 200))
        sizer.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.save_btn = wx.Button(self.panel, label="&Save and Check")
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        buttons.Add(self.save_btn, 0, wx.ALL, 5)

        self.cancel_btn = wx.Button(self.panel, label="Cancel")
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel)
        buttons.Add(self.cancel_btn, 0, wx.ALL, 5)
        sizer.Add(buttons, 0, wx.ALL | wx.CENTER, 10)

        self.panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Centre()
        wx.CallAfter(self._greet)

    def _greet(self):
        existing = get_api_key(self.provider.key)
        if existing:
            self.api.speak(
                f"Your saved {self.provider.label} API key is shown in the text field. "
                "Press Save and Check to use it, or type a new key to replace it."
            )
        else:
            self.api.speak(
                f"Type or paste your {self.provider.label} API key, then press Save and Check. "
                "The key is stored in your PyOS folder so you only need to enter it once."
            )
        self.key_input.SetFocus()
        self.key_input.SetInsertionPointEnd()

    # -- events ----------------------------------------------------------
    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.on_cancel(None)
        else:
            event.Skip()

    def on_cancel(self, event):
        self.saved_key = ""
        self.EndModal(wx.ID_CANCEL)

    def on_save(self, event):
        if self._busy:
            return
        key = self.key_input.GetValue().strip()
        if not key:
            self.api.speak("Type or paste your API key first.")
            self.key_input.SetFocus()
            return

        self._busy = True
        self.save_btn.Disable()
        self.cancel_btn.Disable()
        self.status.SetLabel(f"Checking your {self.provider.label} API key...")
        self.sizer.Layout()
        self.api.speak("Checking your API key...")
        threading.Thread(target=self._verify, args=(key,), daemon=True).start()

    def _verify(self, key):
        try:
            self.provider.verify_key(key)
            error = None
        except ProviderError as err:
            error = err
        except Exception as err:  # network stack surprises
            error = ProviderError("http", f"Could not check the key: {err}")
        wx.CallAfter(self._on_verified, key, error)

    def _on_verified(self, key, error):
        self._busy = False
        self.save_btn.Enable()
        self.cancel_btn.Enable()

        if error is None:
            self._store(key)
            return

        self.status.SetLabel(error.message)
        self.sizer.Layout()

        if error.kind == "offline":
            self.api.speak(error.message)
            if self._confirm_save_anyway(error.message):
                self._store(key)
                return
            self.api.speak("The key was not saved.")
            self.key_input.SetFocus()
            return

        self.api.speak(error.message)
        if error.kind == "bad_key":
            self.key_input.SelectAll()
        self.key_input.SetFocus()

    def _confirm_save_anyway(self, message):
        dialog = wx.MessageDialog(
            self,
            f"{message}\n\nSave the key anyway?",
            "Could not check the key",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        try:
            return dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()

    def _store(self, key):
        try:
            set_api_key(self.provider.key, key)
        except Exception as err:
            self.api.speak(f"Could not save the API key. {err}")
            self.status.SetLabel("Could not save the API key.")
            return
        self.saved_key = key
        self.api.speak(f"{self.provider.label} API key saved.")
        self.EndModal(wx.ID_OK)


class KnowledgeViewerDialog(wx.Dialog):
    """Read out what the assistant tells the model about PyOS.

    The AI can only answer confidently about this simulator because a short
    reference travels with every question. This dialog exists so the user can
    hear exactly what was sent, one section at a time, instead of taking it on
    trust.
    """

    def __init__(self, parent, api, pack):
        super().__init__(
            parent,
            title="What the AI knows about PyOS",
            size=(700, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )
        self.api = api
        self.pack = pack

        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(panel, label="What the AI knows about PyOS")
        heading.SetFont(
            wx.Font(15, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        )
        heading.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(heading, 0, wx.ALL | wx.CENTER, 12)

        note = wx.StaticText(
            panel,
            label=(
                "This reference is sent with every question, so answers about the\n"
                "desktop, the apps and the Terminal are grounded in how PyOS really works."
            ),
        )
        note.SetForegroundColour(wx.Colour(200, 200, 255))
        sizer.Add(note, 0, wx.ALL | wx.CENTER, 8)

        self.summary = wx.StaticText(panel, label=self._summary_text())
        self.summary.SetForegroundColour(wx.Colour(255, 255, 200))
        sizer.Add(self.summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        list_label = wx.StaticText(panel, label="Sections:")
        list_label.SetForegroundColour(wx.Colour(200, 200, 255))
        sizer.Add(list_label, 0, wx.LEFT | wx.TOP, 12)

        self.list = wx.ListBox(panel, choices=self.pack.titles, style=wx.LB_SINGLE)
        self.list.SetBackgroundColour(wx.Colour(20, 20, 60))
        self.list.SetForegroundColour(wx.Colour(255, 255, 255))
        self.list.Bind(wx.EVT_SET_FOCUS, self.on_list_focus)
        self.list.Bind(wx.EVT_LISTBOX, self.on_select)
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, self.on_read)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)

        self.text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text.SetBackgroundColour(wx.Colour(10, 10, 30))
        self.text.SetForegroundColour(wx.Colour(200, 200, 255))
        self.text.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Section text"))
        sizer.Add(self.text, 2, wx.EXPAND | wx.ALL, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        read_btn = wx.Button(panel, label="&Read Section")
        read_btn.Bind(wx.EVT_BUTTON, self.on_read)
        read_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Read Section"))
        buttons.Add(read_btn, 0, wx.ALL, 5)

        all_btn = wx.Button(panel, label="Speak &All")
        all_btn.Bind(wx.EVT_BUTTON, self.on_speak_all)
        all_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Speak All"))
        buttons.Add(all_btn, 0, wx.ALL, 5)

        copy_btn = wx.Button(panel, label="&Copy All")
        copy_btn.Bind(wx.EVT_BUTTON, self.on_copy_all)
        copy_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Copy All"))
        buttons.Add(copy_btn, 0, wx.ALL, 5)

        close_btn = wx.Button(panel, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, self.on_close_button)
        close_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Close"))
        buttons.Add(close_btn, 0, wx.ALL, 5)
        sizer.Add(buttons, 0, wx.ALL | wx.CENTER, 10)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Centre()

        if self.pack.titles:
            self.list.SetSelection(0)
            self._show_section(0)
        wx.CallAfter(self._greet)

    # -- helpers ---------------------------------------------------------
    def _summary_text(self):
        text = (
            f"{len(self.pack.sections)} sections, {self.pack.char_count} characters, "
            f"revision {self.pack.fingerprint}."
        )
        if self.pack.dropped:
            text += f" Left out to fit this model: {', '.join(self.pack.dropped)}."
        if self.pack.truncated:
            text += " Shortened to fit this model."
        return text

    def current_section(self):
        index = self.list.GetSelection()
        if 0 <= index < len(self.pack.sections):
            return self.pack.sections[index]
        return None

    def _show_section(self, index):
        if not (0 <= index < len(self.pack.sections)):
            self.text.SetValue("")
            return None
        section = self.pack.sections[index]
        self.text.SetValue(section.text)
        self.text.SetInsertionPoint(0)
        return section

    def _greet(self):
        self.api.speak(
            f"{self.pack.summary()} Choose a section to read it, "
            "or press Speak All to hear the whole reference."
        )
        self.list.SetFocus()

    # -- events ----------------------------------------------------------
    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.on_close_button(None)
        else:
            event.Skip()

    def on_list_focus(self, event):
        self.api.speak("Knowledge sections")
        event.Skip()

    def on_select(self, event):
        section = self._show_section(self.list.GetSelection())
        if section and not self.api.is_enhanced_mode():
            self.api.speak(section.title, interrupt=False)

    def on_read(self, event):
        section = self.current_section()
        if section is None:
            self.api.speak("Select a section first.")
            return
        self._show_section(self.list.GetSelection())
        self.api.speak(f"{section.title}. {section.text}")

    def on_speak_all(self, event):
        self.api.speak(self.pack.summary())
        for section in self.pack.sections:
            self.api.speak(f"{section.title}. {section.text}", interrupt=False)

    def on_copy_all(self, event):
        if not wx.TheClipboard.Open():
            self.api.speak("Could not open the clipboard.")
            return
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(self.pack.text))
        finally:
            wx.TheClipboard.Close()
        self.api.speak("The PyOS reference was copied to the clipboard.")

    def on_close_button(self, event):
        self.EndModal(wx.ID_OK)
