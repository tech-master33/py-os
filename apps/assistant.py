"""The PyOS AI Assistant.

The assistant sends each question to the chosen provider with the PyOS reference
attached, and reports what the model is doing as it happens. The rule that shapes
this file is that it never announces a phase on the model's behalf: "Thinking...",
"Generating response...", "Researching..." and "Visiting website..." are spoken
only when the provider reports that the model really did that. Pressing Enter
says "Waiting for <provider>..." and nothing more, so a rejected key or a dead
connection can never be prefixed by a word that was never true.

Nothing said on the assistant's own initiative interrupts what a screen reader is
saying: automatic speech goes out with interrupt=False and waits in the reader's
queue, which paces it and carries on after the model has stopped. PyOS keeps no
speech queue of its own.

Two things travel with every question besides the reference. The first is the
remembered conversation, kept here in memory for the session and never written to
disk. The second is the block from :mod:`ai_capability`, which tells the model who
it is and whether web research is switched on for this particular request, so it
searches when asked instead of advising the user to check their providers.
"""

import threading

import wx

from api import BlindApp
from ai_capability import RESERVE_CHARS, capability_block, looks_like_research
from ai_provider_dialog import KnowledgeViewerDialog, ProviderPickerDialog
from pyos_knowledge import KnowledgePack, KnowledgeSection, build_knowledge
from text_integrity import for_display, is_suspicious
from ai_providers import (
    EVENT_CONTENT,
    EVENT_REASONING,
    EVENT_SOURCE,
    EVENT_STATUS,
    STATUS_RESEARCHING,
    STATUS_VISITING,
    STATUS_WAITING,
    ProviderError,
    content_event,
    get_api_key,
    get_model,
    get_provider,
    get_provider_name,
    get_speak_reasoning,
    get_web,
    set_model,
    set_provider_name,
    set_speak_reasoning,
    set_web,
    status_speech,
    trim_history,
)

# Where a spoken sentence is allowed to end while reasoning is being read out.
SENTENCE_ENDS = ".!?;\n"


def _complete_sentences(buffer):
    """Split the finished sentences out of a partly received buffer.

    Returns ``(sentences, remainder)``. The remainder is held back so a sentence
    that is still arriving is never read out in halves.
    """
    sentences = []
    start = 0
    for index, character in enumerate(buffer):
        if character in SENTENCE_ENDS:
            sentence = buffer[start:index + 1].strip()
            if sentence:
                sentences.append(sentence)
            start = index + 1
    return sentences, buffer[start:].lstrip()


class _EventBuffer:
    """Carries provider events from the worker thread to the window.

    Everything is handed over the moment it arrives: text is meant to be read as
    it lands, and with no queue of PyOS's own there is nothing to be gained by
    holding events back. The provider worker thread calls :meth:`add`; the window
    is only touched from ``wx.CallAfter`` handlers in the main thread.
    """

    def __init__(self, app, request_id):
        self.app = app
        self.request_id = request_id
        self.lock = threading.Lock()
        self.events = []

    def add(self, event):
        with self.lock:
            self.events.append(event)
            batch, self.events = self.events, []
        wx.CallAfter(self.app.apply_events, self.request_id, batch)

    def flush(self):
        """Send anything still buffered, so nothing is lost on the way out."""
        with self.lock:
            batch, self.events = self.events, []
        if batch:
            wx.CallAfter(self.app.apply_events, self.request_id, batch)


class AssistantApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "AI Assistant"
        self.description = (
            "Chat with Ollama on this computer, Google Gemini, OpenRouter, or Groq."
        )
        self.category = "Tools"
        self.help_text = (
            "Pick a provider when the assistant starts, choose a model, type a question "
            "and press Enter. The answer is spoken, and you hear what the model is "
            "doing while you wait: thinking, researching, or writing the answer. The "
            "model remembers the conversation while the assistant is open, so you can "
            "ask follow-up questions; Clear conversation starts again from nothing. "
            "Use the Provider button to switch provider or enter a new API key, the "
            "Knowledge button to hear what the AI has been told about PyOS, the Web "
            "research box to let a model search the web, and Escape to stop a long "
            "answer. Nothing is read out over the top of what your screen reader is "
            "saying, so the model's thinking is never cut off."
        )
        self.docs = (
            "AI Assistant can talk to a local Ollama server, Google Gemini, OpenRouter, "
            "or Groq. Ollama needs no API key; the others ask for one the first time "
            "you choose them. Each provider remembers its own model, its own API key "
            "and its own web research setting in ai_config.json inside the PyOS data "
            "folder, so after the first setup you can pick your provider and start "
            "asking questions straight away. API keys are stored as plain text on this "
            "computer. Every question also carries a short PyOS reference, so the model "
            "knows how this simulator works; the Knowledge button reads out exactly "
            "what was sent. While a question is being answered the assistant says "
            "Waiting for the provider, then reports only what the model is actually "
            "doing: Thinking when reasoning arrives, Generating response when answer "
            "text arrives, and Researching or Visiting website when the provider "
            "reports a search or a page it read. What the model is reasoning about is "
            "written to the history as it arrives, so it can be read as it comes and "
            "scrolled back through afterwards. The assistant remembers the conversation "
            "while it is open, separately for each provider, and sends the earlier "
            "exchanges with every new question, so a follow-up question makes sense; "
            "Clear conversation forgets them, switching provider forgets them, and none "
            "of it is ever written to disk. Every question also carries a short note "
            "saying which provider the model is and whether web research is switched "
            "on for that question, so a model asked to search either searches or says "
            "plainly that research is off, rather than suggesting the user look at "
            "providers. If research was on and the model never used the web, the "
            "assistant says so; answers are shown exactly as the model wrote them, and "
            "only the spoken form leaves out characters a voice cannot say. Nothing PyOS says cuts "
            "into whatever is already being read: status words, the thinking and the "
            "answer are handed over without interrupting, so a screen reader reads at "
            "its own pace and carries on after the model has stopped. Escape stops the "
            "answer that is being written. On a computer with no screen reader, "
            "thinking is written to the history and not read out; the answer is still "
            "spoken."
        )
        self.provider_key = None
        self.provider = None
        self.provider_api_key = ""
        self.model = ""
        self.models = []
        self.knowledge = None
        # State for the question in flight.
        self.request_id = 0
        self.cancel_event = None
        self.buffer = None
        self.busy = False
        self.active_label = ""
        self.answer_text = ""
        self.reasoning_text = ""
        self.sources = []
        self.pending_sentence = ""
        self.block = None
        self.web_enabled = False
        self.speak_reasoning = True
        # Conversation memory: the questions and answers of this session for the
        # provider in use. It lives in memory only, is never written to disk, and
        # is emptied by Clear conversation or by switching provider.
        self.conversation = []
        self.pending_question = ""
        self.dropped_turns = 0
        self.memory_note_spoken = False
        # What the request in flight was asked to do, so the report afterwards is
        # about that request rather than about the settings as they are now.
        self.request_web = False
        self.research_seen = False
        # Counting the text that had to be repaired or that a window cannot carry,
        # so damage is mentioned once per answer instead of passing unnoticed.
        self.text_issues = 0
        self.text_issues_reported = False

    def run(self):
        self.frame = wx.Frame(None, title="AI Assistant", size=(550, 460))
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

        options_row = wx.BoxSizer(wx.HORIZONTAL)
        self.web_check = wx.CheckBox(panel, label="&Web research")
        self.web_check.SetForegroundColour(wx.Colour(200, 200, 255))
        self.web_check.Bind(wx.EVT_CHECKBOX, self.on_web_toggle)
        self.web_check.Bind(wx.EVT_SET_FOCUS, self.on_web_focus)
        options_row.Add(self.web_check, 0, wx.ALL | wx.CENTER, 5)

        # The mnemonic moved off R, which Refresh Models already claims, so both
        # keys do what they say.
        self.reasoning_check = wx.CheckBox(panel, label="Narrate reaso&ning")
        self.reasoning_check.SetForegroundColour(wx.Colour(200, 200, 255))
        self.reasoning_check.SetValue(bool(self.speak_reasoning))
        self.reasoning_check.Bind(wx.EVT_CHECKBOX, self.on_reasoning_toggle)
        self.reasoning_check.Bind(wx.EVT_SET_FOCUS, self.on_reasoning_focus)
        options_row.Add(self.reasoning_check, 0, wx.ALL | wx.CENTER, 5)

        self.clear_btn = wx.Button(panel, label="C&lear conversation")
        self.clear_btn.SetBackgroundColour(wx.Colour(40, 40, 80))
        self.clear_btn.SetForegroundColour(wx.Colour(255, 255, 255))
        self.clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_conversation)
        self.clear_btn.Bind(wx.EVT_SET_FOCUS, self.on_clear_focus)
        options_row.Add(self.clear_btn, 0, wx.ALL, 5)

        hint = wx.StaticText(panel, label="Press Escape to stop a long answer.")
        hint.SetForegroundColour(wx.Colour(180, 180, 220))
        options_row.Add(hint, 0, wx.ALL | wx.CENTER, 5)
        sizer.Add(options_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

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
        self.frame.Bind(wx.EVT_CHAR_HOOK, self.on_key)
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

    def _idle_label(self):
        if self.provider is None:
            return "AI Assistant"
        return f"AI Assistant — {self.provider.label}"

    def _active_label(self):
        return self.active_label or (self.provider.label if self.provider else "AI")

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

        # Memory belongs to the provider that was in use, so switching provider
        # starts a new conversation rather than feeding one model's answers to
        # another.
        self.conversation = []
        self.pending_question = ""
        self.dropped_turns = 0
        self.memory_note_spoken = False

        remembered = get_model(provider_key) or self.provider.default_model
        self.model = remembered
        self.knowledge = self._build_knowledge()
        self.models = []
        self.model_choice.Clear()
        self.model_choice.Append(remembered)
        self.model_choice.SetSelection(0)

        self.web_enabled = get_web(provider_key)
        self.speak_reasoning = get_speak_reasoning()
        self._update_capability_controls()
        self._sync_web_check()
        self._sync_reasoning_check()

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

    # -- capability controls ---------------------------------------------
    def _can_research(self):
        """Whether the model in the box could research if the box were on."""
        if self.provider is None or not self.model:
            return False
        try:
            return bool(self.provider.can_research(self.model))
        except Exception:
            return False

    def _update_capability_controls(self):
        """Show where research stands, without ever changing what the user set.

        This used to force the box down whenever the current model could not
        research, which meant the box could read "off" while the setting was on,
        and a model list arriving seconds after a click could undo that click in
        front of the user. The box is only enabled or greyed out here; its value
        is the stored setting, and only a click or a provider switch changes it.
        """
        self.web_check.Enable(self._can_research())

    def _sync_web_check(self):
        """Make the box show the stored setting, whatever that setting is."""
        if self.web_check.GetValue() != bool(self.web_enabled):
            self.web_check.SetValue(bool(self.web_enabled))

    def _sync_reasoning_check(self):
        if self.reasoning_check.GetValue() != bool(self.speak_reasoning):
            self.reasoning_check.SetValue(bool(self.speak_reasoning))

    def _web_active(self):
        """Whether the question being asked may use the provider's web tools."""
        if not self.web_enabled or self.provider is None:
            return False
        return self._can_research()

    def _web_state_speech(self):
        """The state of research, said plainly, for focus and for a click."""
        if self.provider is None:
            return "Web research, currently off. Choose a provider first."
        if not self._can_research():
            reason = ""
            try:
                reason = self.provider.web_unavailable(self.model)
            except Exception:
                reason = ""
            if self.web_enabled:
                # On, but this model cannot use it: say both facts rather than
                # pretending either one is not true.
                return (
                    f"Web research, currently on, but {self.model} cannot research. "
                    f"{reason} It will be used again with a model that can."
                )
            return (
                f"Web research, currently off, and not available for {self.model}. "
                f"{reason}"
            )
        if not self.web_enabled:
            return (
                "Web research, currently off. Turn it on to let this model search "
                "the web and read pages; searching costs extra."
            )
        tools = ""
        try:
            tools = (self.provider.web_tool_summary(self.model) or "").strip()
        except Exception:
            tools = ""
        detail = f" {self.model} is {tools}." if tools else ""
        return f"Web research, currently on.{detail}"

    def on_web_focus(self, event):
        self.api.speak(self._web_state_speech())
        event.Skip()

    def on_web_toggle(self, event):
        """Turn research on or off, and make the box show what really happened."""
        desired = bool(self.web_check.GetValue())
        if desired and not self._can_research():
            # The click cannot be honoured, so the box is put back rather than
            # left claiming something that will not happen.
            self.web_enabled = False
            self._sync_web_check()
            self.api.speak(self._web_state_speech())
            return
        self.web_enabled = desired
        self._remember_web()
        self._sync_web_check()
        self.api.speak(self._web_state_speech())

    def _reasoning_state_speech(self):
        if not self.speak_reasoning:
            return (
                "Narrate reasoning, currently off. The model's thinking is written "
                "to the history but not read out."
            )
        if self._uses_screen_reader():
            return (
                "Narrate reasoning, currently on. The model's thinking is handed to "
                "your screen reader as it arrives, a sentence at a time; switch "
                "this off to hear only the answer."
            )
        return (
            "Narrate reasoning, currently on, but this computer has no screen "
            "reader, so thinking is written to the history and not read out. The "
            "answer is still spoken."
        )

    def on_reasoning_focus(self, event):
        self.api.speak(self._reasoning_state_speech())
        event.Skip()

    def on_reasoning_toggle(self, event):
        self.speak_reasoning = bool(self.reasoning_check.GetValue())
        try:
            set_speak_reasoning(self.speak_reasoning)
        except Exception:
            pass
        self._sync_reasoning_check()
        self.api.speak(self._reasoning_state_speech())

    # -- conversation memory ---------------------------------------------
    def on_clear_focus(self, event):
        count = len(self.conversation) // 2
        if count:
            self.api.speak(
                f"Clear conversation. {count} exchange{'s' if count != 1 else ''} "
                "remembered; pressing this forgets them."
            )
        else:
            self.api.speak("Clear conversation. Nothing is remembered yet.")
        event.Skip()

    def on_clear_conversation(self, event):
        had = bool(self.conversation)
        self.conversation = []
        self.pending_question = ""
        self.dropped_turns = 0
        self.memory_note_spoken = False
        if had:
            self.api.speak(
                "Conversation cleared. This model will not remember the earlier "
                "questions."
            )
        else:
            self.api.speak("Nothing was remembered, so there is nothing to clear.")

    def _remember_turn(self, role, text):
        """Add one question or answer to this session's conversation."""
        text = (text or "").strip()
        if text:
            self.conversation.append({"role": role, "text": text})

    def _forget_pending_question(self):
        """Drop the question in flight from the conversation.

        Used when a question never got an answer: a failure, or a stop before
        anything arrived. Leaving it behind would make the next question read as
        a follow-up to something the model never replied to.
        """
        if not self.pending_question or not self.conversation:
            self.pending_question = ""
            return
        last = self.conversation[-1]
        if last.get("role") == "user" and last.get("text") == self.pending_question:
            self.conversation.pop()
        self.pending_question = ""

    def _remember_web(self):
        if not self.provider_key:
            return
        try:
            set_web(self.provider_key, self.web_enabled)
        except Exception:
            pass

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
        """Build the reference that grounds this provider's answers.

        The reference shares the system budget with the per-request capability
        block, so the room that block could need is measured from the block
        itself rather than estimated. Switching research on gives the longest
        version, and that is the one measured.
        """
        limit = getattr(self.provider, "system_char_limit", None)
        reserve = RESERVE_CHARS
        try:
            reserve = max(
                reserve,
                len(capability_block(self.provider, self.model, True, 8)) + 16,
            )
        except Exception:
            pass
        budget = limit - reserve if limit else None
        try:
            return build_knowledge(apps=self._live_apps(), max_chars=budget)
        except Exception as err:
            # Grounding is a bonus; the assistant still works without it.
            self.api.speak(
                f"Could not prepare the PyOS reference, so answers will be unguided. {err}",
                interrupt=False,
            )
            return None

    def _capability_note(self, web, remembered):
        """The block that tells the model who it is for one question."""
        try:
            return capability_block(self.provider, self.model, web, remembered)
        except Exception:
            return ""

    def _system_text(self, web, remembered):
        """Everything sent as standing instruction with one question."""
        parts = []
        if self.knowledge is not None:
            parts.append(self.knowledge.text.strip())
        note = self._capability_note(web, remembered)
        if note:
            parts.append(note)
        return "\n\n".join(parts) or None

    def on_knowledge_button(self, event):
        """Read out what the model was told about PyOS.

        The reference alone is not the whole story any more: the model is also
        told who it is and whether research is on for the question it is
        answering, so that block is shown as its own section. What is read here
        is what is actually sent.
        """
        if self.knowledge is None:
            self.api.speak("Choose a provider first.")
            return
        dialog = KnowledgeViewerDialog(self.frame, self.api, self._viewer_pack())
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _viewer_pack(self):
        """The reference plus the live capability block, as one pack."""
        note = self._capability_note(self._web_active(), len(self.conversation))
        if not note:
            return self.knowledge
        section = KnowledgeSection("you_now", "You, right now", note)
        text = f"{self.knowledge.text.strip()}\n\n{section.rendered()}\n"
        return KnowledgePack(
            text,
            list(self.knowledge.sections) + [section],
            self.knowledge.dropped,
            self.knowledge.truncated,
        )

    # -- models ----------------------------------------------------------
    def _announce_web_availability(self, before):
        """Say so when a model change really changes what research will do."""
        if before == self._web_active():
            return
        if self._web_active():
            self.api.speak(
                f"Web research will now be used with {self.model}.", interrupt=False
            )
        elif self._can_research():
            self.api.speak(
                "Web research is switched off, so it will not be used.", interrupt=False
            )
        else:
            self.api.speak(
                f"{self.model} cannot research, so web research will not be used."
                + (" It stays switched on for a model that can." if self.web_enabled else ""),
                interrupt=False,
            )

    def on_model_change(self, event):
        sel = self.model_choice.GetSelection()
        if 0 <= sel < len(self.models):
            before = self._web_active()
            self.model = self.models[sel]
            self._remember_model()
            self._update_capability_controls()
            self._sync_web_check()
            if not self.api.is_enhanced_mode():
                self.api.speak(f"Model set to {self.model}")
            self._announce_web_availability(before)

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

        before = self._web_active()
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

        self._update_capability_controls()
        self._sync_web_check()
        self.api.speak(f"Loaded {len(self.models)} models. {self.model} is selected.")
        self._announce_web_availability(before)

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
        if not prompt:
            return
        if self.busy:
            # The input is disabled while a question is in flight, so this is
            # only reachable if something asks on the user's behalf.
            self.api.speak(
                "Still working on the last question. Press Escape to stop it."
            )
            return

        self.input_ctrl.Clear()
        self.request_id += 1
        request_id = self.request_id
        self.cancel_event = threading.Event()
        self.buffer = _EventBuffer(self, request_id)
        self.active_label = self.provider.label
        self.answer_text = ""
        self.reasoning_text = ""
        self.sources = []
        self.pending_sentence = ""
        self.block = None
        self.text_issues = 0
        self.text_issues_reported = False

        web = self._web_active()
        self.request_web = web
        self.research_seen = False

        # Said before the question is sent, because afterwards it is a guess: a
        # model with no web tools for this question would otherwise be asked to
        # search and would have to explain the settings back to the user.
        if not web and self._can_research() and looks_like_research(prompt):
            self.api.speak(
                "Web research is off, so this answer will come from what the "
                "model already knows. The Web research box turns searching on.",
                interrupt=False,
            )

        history, dropped = trim_history(
            self.conversation, getattr(self.provider, "history_char_limit", 0)
        )
        if dropped > self.dropped_turns:
            self.dropped_turns = dropped
            self.api.speak(
                "The conversation is long, so its earliest part is no longer sent "
                "with the question.",
                interrupt=False,
            )

        system = self._system_text(web, len(history))
        self._remember_turn("user", prompt)
        self.pending_question = prompt

        self._append(f"You: {prompt}\n")
        self._set_busy(True)
        self.status_label.SetLabel(f"{self._idle_label()} — Waiting...")
        # The only words said on the assistant's own initiative. Everything after
        # this is reported by the provider, never guessed.
        self.api.speak(status_speech(STATUS_WAITING, self.provider.label))

        model = self.model
        threading.Thread(
            target=self._ask_worker,
            args=(prompt, model, request_id, system, web, list(history)),
            daemon=True,
        ).start()

    def _ask_worker(self, prompt, model, request_id, system, web, history):
        provider = self.provider
        if provider is None:
            return
        cancel = self.cancel_event
        received = []

        def on_event(event):
            received.append(event)
            self.buffer.add(event)

        try:
            reply = provider.ask_stream(
                prompt,
                model,
                system=system,
                on_event=on_event,
                cancel=cancel,
                web=web,
                history=history,
            )
        except ProviderError as err:
            if not received and err.kind == "http":
                # Nothing arrived at all, so the plain one-shot request gets one
                # try before the user is told the question failed.
                reply = self._ask_without_streaming(
                    provider, prompt, model, system, web, request_id, history
                )
                if reply is None:
                    return
                on_event(content_event(reply))
            else:
                # Whatever already arrived still belongs to the user, so it is
                # flushed to the window before the failure is reported.
                self.buffer.flush()
                wx.CallAfter(
                    self.finish_error, request_id, self._error_text(err), bool(received)
                )
                return
        except Exception as err:
            self.buffer.flush()
            wx.CallAfter(
                self.finish_error, request_id, f"Unexpected error: {err}", bool(received)
            )
            return

        self.buffer.flush()
        wx.CallAfter(self.finish_answer, request_id, reply, cancel.is_set())

    def _ask_without_streaming(
        self, provider, prompt, model, system, web, request_id, history=None
    ):
        """Fall back to a single request. Returns the reply, or None if it failed."""
        try:
            return provider.ask(prompt, model, system=system, web=web, history=history)
        except ProviderError as err:
            wx.CallAfter(self.finish_error, request_id, self._error_text(err), False)
        except Exception as err:
            wx.CallAfter(self.finish_error, request_id, f"Unexpected error: {err}", False)
        return None

    def _error_text(self, error):
        if error.kind in ("no_key", "bad_key"):
            return f"{error.message} Press the Provider button to enter a working key."
        return error.message

    # -- streaming events ------------------------------------------------
    def apply_events(self, request_id, events):
        """Move a batch of provider events into the window and the speech.

        Events from a question that has already been replaced are dropped, so an
        abandoned answer can never write itself over a newer one.
        """
        if request_id != self.request_id:
            return
        for event in events:
            if event.kind == EVENT_STATUS:
                self._on_status_event(event)
            elif event.kind == EVENT_REASONING:
                self._on_reasoning_event(event)
            elif event.kind == EVENT_CONTENT:
                self._on_content_event(event)
            elif event.kind == EVENT_SOURCE:
                self._on_source_event(event)

    def _clean(self, text):
        """Repair one piece of incoming text and note that it needed repairing.

        Everything that reaches the history goes through here, so mangled bytes
        are put right and characters a window cannot carry never get in. The
        count is what lets the assistant say, once per answer, that something
        could not be shown as written instead of hiding the damage.
        """
        if not text:
            return ""
        if is_suspicious(text):
            self.text_issues += 1
        return for_display(text)

    def _on_status_event(self, event):
        if event.text in (STATUS_RESEARCHING, STATUS_VISITING):
            # The model really did use the web for this question, which is what
            # makes the report at the end of the answer honest either way.
            self.research_seen = True
        phrase = status_speech(event.text)
        if not phrase:
            return
        self.status_label.SetLabel(f"AI Assistant — {self._active_label()} — {phrase.rstrip('.')}")
        if event.detail:
            # A search query or a page address: worth writing down, and worth
            # hearing for a search, but too noisy to read a URL out loud.
            self._append(f"[{phrase.rstrip('.')}: {event.detail}]\n")
            if event.text == STATUS_RESEARCHING:
                self.api.speak(f"{phrase} {event.detail}", interrupt=False)
                return
        # Said without interrupting, so a status word never cuts into the thinking
        # it is describing; the screen reader's own queue decides when it is heard.
        self.api.speak(phrase, interrupt=False)

    def _on_reasoning_event(self, event):
        text = self._clean(event.text)
        if not text:
            return
        self.reasoning_text += text
        self._open_block("reasoning", "Thinking: ")
        self._append(text)
        # Nothing is collected for reading while narration is switched off.
        if not self.speak_reasoning:
            return
        self.pending_sentence += text
        sentences, remainder = _complete_sentences(self.pending_sentence)
        self.pending_sentence = remainder
        if not self._voice_reasoning():
            return
        for sentence in sentences:
            # Handed over as soon as the sentence is complete, never interrupting:
            # the reader's queue paces it and keeps going after the model stops.
            self.api.speak(sentence, interrupt=False)

    def _flush_pending_sentence(self):
        sentence = self.pending_sentence.strip()
        self.pending_sentence = ""
        if sentence and self._voice_reasoning():
            self.api.speak(sentence, interrupt=False)

    def _on_content_event(self, event):
        text = self._clean(event.text)
        if not text:
            return
        # The model has started answering, so any half-spoken thought is finished
        # off before the answer is written down.
        self._flush_pending_sentence()
        self.answer_text += text
        self._open_block("answer", f"{self._active_label()}: ")
        self._append(text)

    def _on_source_event(self, event):
        url = event.detail or ""
        if not url or any(url == existing for _, existing in self.sources):
            return
        self.sources.append((event.text or url, url))

    def _open_block(self, name, prefix):
        """Start a new block in the history, closing whatever was open."""
        if self.block == name:
            return
        self._close_block()
        self._append(prefix)
        self.block = name

    def _close_block(self, suffix=None):
        if self.block is None:
            return
        if suffix:
            self._append(f" ({suffix})")
        self._append("\n")
        self.block = None

    # -- finishing -------------------------------------------------------
    def finish_answer(self, request_id, reply, stopped=False):
        if request_id != self.request_id:
            return
        self._close_block("stopped" if stopped else None)
        self._flush_pending_sentence()

        text = self._clean((reply or "").strip()) or self.answer_text.strip()
        if not text:
            text = "The model did not return an answer."
        self.status_label.SetLabel(self._idle_label())
        if text == "The model did not return an answer.":
            # Nothing came back, so the question is dropped rather than left in
            # the conversation as a follow-up to nothing.
            self._forget_pending_question()
        else:
            # The answer becomes part of what the model remembers, so a follow-up
            # question has something to refer back to. A stopped answer is kept
            # too: the part that arrived is what the user read.
            self._remember_turn("assistant", text)
            self.pending_question = ""
        if stopped:
            # Whoever pressed Escape did so to stop hearing the answer, so it is
            # not read back at them; it stays in the history.
            self.api.speak("Stopped. The part that arrived is in the history.")
        else:
            # The words were written to the history as they arrived; this speaks
            # the finished answer in one go, without interrupting the thinking that
            # may still be being read out.
            self.show_response(
                text, False, self._active_label(), write_history=False, interrupt=False
            )
        self._report_research_result()
        if self.sources:
            self._report_sources()
        self._report_text_issues()
        self._report_memory_note()
        self._set_busy(False)

    def _report_research_result(self):
        """Say whether the research that was asked for actually happened.

        A question sent with research on but answered without a single search or
        page visit is worth saying out loud: the user asked the model to look
        something up, and being told nothing would leave them assuming it did.
        """
        if not self.request_web or self.research_seen:
            return
        self._append("[No web search or page visit was reported by the model.]\n")
        self.api.speak(
            "Web research was on, but the model did not report using the web.",
            interrupt=False,
        )

    def _report_text_issues(self):
        """Mention, once, that part of an answer had to be put back together."""
        if not self.text_issues or self.text_issues_reported:
            return
        self.text_issues_reported = True
        self._append("[Some characters were repaired or left out of the spoken text.]\n")
        self.api.speak(
            "Some characters in that answer could not be shown exactly as the "
            "model wrote them; the history has the rest.",
            interrupt=False,
        )

    def _report_memory_note(self):
        """Say so if the provider could not be sent the conversation at all."""
        note = getattr(self.provider, "memory_note", "") if self.provider else ""
        if not note or self.memory_note_spoken:
            return
        self.memory_note_spoken = True
        self.api.speak(note, interrupt=False)

    def finish_error(self, request_id, message, partial=False):
        if request_id != self.request_id:
            return
        self._close_block("interrupted" if partial else None)
        self.status_label.SetLabel(self._idle_label())
        if partial and self.answer_text.strip():
            spoken = f"The answer was interrupted. {message}"
            self._remember_turn("assistant", self.answer_text.strip())
        else:
            spoken = message
            self._forget_pending_question()
        self.api.speak(spoken, interrupt=False)
        self._append(f"Error: {message}\n")
        self._report_text_issues()
        self._report_memory_note()
        self._set_busy(False)

    def _report_sources(self):
        count = len(self.sources)
        self._append(f"Sources ({count}):\n")
        for title, url in self.sources:
            self._append(f"  {title} — {url}\n")
        self.api.speak(
            f"{count} source{'s' if count != 1 else ''} used. "
            "They are listed in the conversation history.",
            interrupt=False,
        )

    def _set_busy(self, busy):
        self.busy = busy
        self._set_input_enabled(not busy)

    def show_response(self, text, is_error=False, provider_label=None, write_history=True,
                      interrupt=True):
        # The label travels with the reply so a late answer is still credited to
        # the provider that produced it, even after a switch.
        label = provider_label or (self.provider.label if self.provider else "AI")
        if write_history:
            speaker = "Error" if is_error else label
            self._append(f"{speaker}: {text}\n")
        # An answer that lands while the model's thinking is still being read out
        # must not cut it off, so the caller decides whether this may interrupt.
        self.api.speak(text, interrupt=interrupt)
        if not is_error:
            self.status_label.SetLabel(f"AI Assistant — {label}" if self.provider else "AI Assistant")

    # -- stopping --------------------------------------------------------
    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE and self.busy:
            self.cancel_request()
            return
        event.Skip()

    def cancel_request(self):
        """Stop the answer being written, keeping whatever has arrived.

        The provider checks between chunks, so this is immediate while text is
        streaming but can wait until the model says anything at all if it is
        still loading or thinking hard. The spoken wording says so rather than
        promising an instant stop.
        """
        if not self.busy or self.cancel_event is None:
            return
        self.cancel_event.set()
        self.api.speak(
            "Stopping. This can take a moment if the model has not started yet."
        )

    # -- speaking --------------------------------------------------------
    def _voice_reasoning(self):
        """Whether the model's thinking should be read out at all.

        Only when the voice in use belongs to a screen reader. PyOS hands it each
        finished sentence the moment it is complete and the reader's own queue does
        the rest, so the thinking keeps flowing while the model works and after it
        stops - without PyOS keeping a queue, a timer or a speech rate of its own.
        On a machine whose only voice is PyOS's own engine, thinking would come out
        fragment by fragment, so it stays as text in the transcript and only the
        finished answer is spoken.
        """
        if not self.speak_reasoning:
            return False
        return self._uses_screen_reader()

    def _uses_screen_reader(self):
        engine = getattr(self.api, "engine", None)
        ask = getattr(engine, "uses_screen_reader", None)
        if ask is None:
            return False
        try:
            return bool(ask())
        except Exception:
            return False

    def _append(self, text):
        """Add text to the transcript and keep the newest of it in view.

        The same trick the Terminal uses, so the pane follows what is arriving and
        can be scrolled back through afterwards.
        """
        if not text:
            return
        # Cleaned here too, without counting: the count is for text that arrived
        # from the provider, not for the assistant's own labels.
        self.history.AppendText(for_display(text))
        self.history.ShowPosition(self.history.GetLastPosition())
