import wx
import threading

from api import BlindApp
from ai_provider_dialog import KnowledgeViewerDialog, ProviderPickerDialog
from pyos_knowledge import build_knowledge
from ai_providers import (
    ProviderError,
    get_api_key,
    get_model,
    get_provider,
    get_provider_name,
    set_model,
    set_provider_name,
)


class AssistantApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "AI Assistant"
        self.description = "Chat with Ollama on this computer, Google Gemini, or OpenRouter."
        self.category = "Tools"
        self.help_text = (
            "Pick a provider when the assistant starts, choose a model, type a question "
            "and press Enter. The answer is spoken. Use the Provider button to switch "
            "provider or enter a new API key, and the Knowledge button to hear what the "
            "AI has been told about PyOS."
        )
        self.docs = (
            "AI Assistant can talk to a local Ollama server, Google Gemini, or OpenRouter. "
            "Ollama needs no API key; Gemini and OpenRouter ask for one the first time you "
            "choose them. Each provider remembers its own model and API key in ai_config.json "
            "inside the PyOS data folder, so after the first setup you can pick your provider "
            "and start asking questions straight away. API keys are stored as plain text on "
            "this computer. Every question also carries a short PyOS reference, so the model "
            "knows how this simulator works; the Knowledge button reads out exactly what "
            "was sent."
        )
        self.provider_key = None
        self.provider = None
        self.provider_api_key = ""
        self.model = ""
        self.models = []
        self.knowledge = None

    def run(self):
        self.frame = wx.Frame(None, title="AI Assistant", size=(550, 400))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(20, 20, 50))
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.status_label = wx.StaticText(panel, label="AI Assistant")
        self.status_label.SetForegroundColour(wx.Colour(255, 255, 255))
        self.status_label.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(self.status_label, 0, wx.ALL | wx.CENTER, 10)

        model_row = wx.BoxSizer(wx.HORIZONTAL)
        model_lbl = wx.StaticText(panel, label="Model:")
        model_lbl.SetForegroundColour(wx.Colour(200, 200, 255))
        model_row.Add(model_lbl, 0, wx.ALL | wx.CENTER, 5)

        self.model_choice = wx.Choice(panel, choices=[])
        self.model_choice.SetBackgroundColour(wx.Colour(30, 30, 60))
        self.model_choice.SetForegroundColour(wx.Colour(255, 255, 255))
        self.model_choice.SetSelection(0)
        self.model_choice.Bind(wx.EVT_CHOICE, self.on_model_change)
        self.model_choice.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Model selection"))
        model_row.Add(self.model_choice, 1, wx.EXPAND | wx.ALL, 5)

        self.refresh_btn = wx.Button(panel, label="&Refresh Models")
        self.refresh_btn.SetBackgroundColour(wx.Colour(40, 40, 80))
        self.refresh_btn.SetForegroundColour(wx.Colour(255, 255, 255))
        self.refresh_btn.Bind(wx.EVT_BUTTON, self.on_refresh_models)
        self.refresh_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Refresh Models"))
        model_row.Add(self.refresh_btn, 0, wx.ALL, 5)

        self.provider_btn = wx.Button(panel, label="&Provider...")
        self.provider_btn.SetBackgroundColour(wx.Colour(40, 40, 80))
        self.provider_btn.SetForegroundColour(wx.Colour(255, 255, 255))
        self.provider_btn.Bind(wx.EVT_BUTTON, self.on_provider_button)
        self.provider_btn.Bind(
            wx.EVT_SET_FOCUS,
            lambda e: self.api.speak("Change provider or enter an API key"),
        )
        model_row.Add(self.provider_btn, 0, wx.ALL, 5)

        self.knowledge_btn = wx.Button(panel, label="&Knowledge...")
        self.knowledge_btn.SetBackgroundColour(wx.Colour(40, 40, 80))
        self.knowledge_btn.SetForegroundColour(wx.Colour(255, 255, 255))
        self.knowledge_btn.Bind(wx.EVT_BUTTON, self.on_knowledge_button)
        self.knowledge_btn.Bind(
            wx.EVT_SET_FOCUS,
            lambda e: self.api.speak("Review the PyOS reference sent to the AI"),
        )
        model_row.Add(self.knowledge_btn, 0, wx.ALL, 5)
        sizer.Add(model_row, 0, wx.EXPAND | wx.ALL, 10)

        self.input_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.input_ctrl.SetBackgroundColour(wx.Colour(30, 30, 60))
        self.input_ctrl.SetForegroundColour(wx.Colour(255, 255, 255))
        self.input_ctrl.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Type your question"))
        sizer.Add(self.input_ctrl, 0, wx.EXPAND | wx.ALL, 10)

        self.history = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.history.SetBackgroundColour(wx.Colour(10, 10, 30))
        self.history.SetForegroundColour(wx.Colour(200, 200, 255))
        self.history.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Conversation history"))
        sizer.Add(self.history, 1, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(sizer)
        self.input_ctrl.Bind(wx.EVT_TEXT_ENTER, self.on_ask)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        self.frame.Show()

        self._set_input_enabled(False)
        # Give the window a moment to appear before the picker takes focus.
        wx.CallAfter(self.choose_provider)

    # -- provider selection ----------------------------------------------
    def _set_input_enabled(self, enabled):
        self.model_choice.Enable(enabled)
        self.refresh_btn.Enable(enabled)
        self.input_ctrl.Enable(enabled)

    def choose_provider(self):
        """Ask which provider to use, then hand over to :meth:`activate_provider`."""
        dialog = ProviderPickerDialog(
            self.frame, self.api, current_provider=get_provider_name()
        )
        result = dialog.ShowModal()
        if result == wx.ID_OK:
            provider_key = dialog.provider_key
            api_key = dialog.api_key
        else:
            provider_key = None
            api_key = ""
        dialog.Destroy()

        if not provider_key:
            self.api.speak("No provider selected. Closing the AI Assistant.")
            self.on_close()
            return

        self.activate_provider(provider_key, api_key)

    def on_provider_button(self, event):
        self.choose_provider()

    def activate_provider(self, provider_key, api_key=None):
        self.provider_key = provider_key
        self.provider = get_provider(provider_key, api_key or get_api_key(provider_key))
        if self.provider is None:
            self.api.speak("That provider is not available.")
            return
        self.provider_api_key = self.provider.api_key
        try:
            set_provider_name(provider_key)
        except Exception:
            pass

        self.knowledge = self._build_knowledge()

        remembered = get_model(provider_key) or self.provider.default_model
        self.model = remembered
        self.models = []
        self.model_choice.Clear()
        self.model_choice.Append(remembered)
        self.model_choice.SetSelection(0)

        title = f"AI Assistant — {self.provider.label}"
        self.frame.SetTitle(title)
        self.status_label.SetLabel(title)
        self._set_input_enabled(True)
        self.input_ctrl.SetFocus()
        grounding = (
            "PyOS reference loaded. Press the Knowledge button to review it."
            if self.knowledge
            else ""
        )
        self.api.speak(
            f"{self.provider.label} selected. Model {self.model}. {grounding} "
            "Type your question and press Enter."
        )
        threading.Thread(target=self._fetch_models, daemon=True).start()

    # -- PyOS knowledge --------------------------------------------------
    def _live_apps(self):
        """Metadata of the apps actually installed, for the app catalogue."""
        desktop = getattr(self.api, "desktop", None)
        apps = getattr(desktop, "apps", None)
        if not apps:
            return None
        return [
            {
                "name": getattr(app, "name", ""),
                "description": getattr(app, "description", ""),
                "category": getattr(app, "category", ""),
            }
            for app in apps
        ]

    def _build_knowledge(self):
        """Build the reference that grounds this provider's answers."""
        limit = getattr(self.provider, "system_char_limit", None)
        try:
            return build_knowledge(apps=self._live_apps(), max_chars=limit)
        except Exception as err:
            # Grounding is a bonus; the assistant still works without it.
            self.api.speak(
                f"Could not prepare the PyOS reference, so answers will be unguided. {err}",
                interrupt=False,
            )
            return None

    def on_knowledge_button(self, event):
        """Read out what the model was told about PyOS."""
        if self.knowledge is None:
            self.api.speak("Choose a provider first.")
            return
        dialog = KnowledgeViewerDialog(self.frame, self.api, self.knowledge)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    # -- models ----------------------------------------------------------
    def on_model_change(self, event):
        sel = self.model_choice.GetSelection()
        if 0 <= sel < len(self.models):
            self.model = self.models[sel]
            self._remember_model()
            if not self.api.is_enhanced_mode():
                self.api.speak(f"Model set to {self.model}")

    def on_refresh_models(self, event):
        if self.provider is None:
            self.api.speak("Choose a provider first.")
            return
        self.api.speak("Refreshing models...")
        threading.Thread(target=self._fetch_models, daemon=True).start()

    def _fetch_models(self):
        provider = self.provider
        if provider is None:
            return
        try:
            model_names = provider.list_models()
        except ProviderError as err:
            wx.CallAfter(self._on_models_failed, provider, err.message)
            return
        except Exception as err:
            wx.CallAfter(self._on_models_failed, provider, f"Could not load models: {err}")
            return
        wx.CallAfter(self._update_model_list, provider, model_names)

    def _on_models_failed(self, provider, message):
        # Ignore answers from a provider the user has already switched away from.
        if provider is not self.provider:
            return
        # A remembered model still works, so only warn instead of blocking.
        if self.models:
            self.api.speak(f"Could not refresh the model list. {message}", interrupt=False)
            return
        if self.model:
            self.api.speak(f"{message} Using {self.model}.", interrupt=False)
            return
        self.api.speak(message)

    def _update_model_list(self, provider, model_names):
        if provider is not self.provider:
            return
        if not model_names:
            self._on_models_failed(provider, "The provider did not report any models.")
            return

        self.models = list(model_names)
        self.model_choice.Clear()
        for name in self.models:
            self.model_choice.Append(name)

        if self.model in self.models:
            self.model_choice.SetSelection(self.models.index(self.model))
        else:
            self.model_choice.SetSelection(0)
            self.model = self.models[0]
            self._remember_model()

        self.api.speak(f"Loaded {len(self.models)} models. {self.model} is selected.")

    def _remember_model(self):
        if not self.provider_key:
            return
        try:
            set_model(self.provider_key, self.model)
        except Exception:
            pass

    # -- questions -------------------------------------------------------
    def on_ask(self, event):
        if self.provider is None:
            self.api.speak("Choose a provider first.")
            return

        prompt = self.input_ctrl.GetValue().strip()
        self.input_ctrl.Clear()
        if not prompt:
            return

        model = self.model
        self.history.AppendText(f"You: {prompt}\n")
        self.status_label.SetLabel(f"{self.provider.label} is thinking...")
        self.api.speak("Thinking...")
        threading.Thread(
            target=self._ask_provider, args=(prompt, model), daemon=True
        ).start()

    def _ask_provider(self, prompt, model):
        provider = self.provider
        if provider is None:
            return
        system = self.knowledge.text if self.knowledge else None
        try:
            reply = provider.ask(prompt, model, system=system)
        except ProviderError as err:
            wx.CallAfter(self.show_response, self._error_text(err), True, provider.label)
            return
        except Exception as err:
            wx.CallAfter(self.show_response, f"Unexpected error: {err}", True, provider.label)
            return
        wx.CallAfter(self.show_response, reply, False, provider.label)

    def _error_text(self, error):
        if error.kind in ("no_key", "bad_key"):
            return f"{error.message} Press the Provider button to enter a working key."
        return error.message

    def show_response(self, text, is_error=False, provider_label=None):
        # The label travels with the reply so a late answer is still credited to
        # the provider that produced it, even after a switch.
        label = provider_label or (self.provider.label if self.provider else "AI")
        self.status_label.SetLabel(f"AI Assistant — {label}" if self.provider else "AI Assistant")
        speaker = "Error" if is_error else label
        self.history.AppendText(f"{speaker}: {text}\n")
        self.api.speak(text)
