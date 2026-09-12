import copy
import os

import wx

from api import BlindApp
from sounds import is_valid_theme_name, notes_to_tone_string, parse_tone_string


class ThemeCreatorApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "Theme Creator"
        self.description = "Create a new sound theme or open an existing one to edit."
        self.category = "System"
        self.help_text = (
            "Choose Create New Theme or Open Theme. In the editor, select a sound slot, "
            "choose Tones or Audio File, enter frequencies and durations or browse for a file, "
            "press Play to preview, then save."
        )
        self.docs = (
            "Theme Creator lets you create and edit themes for the existing sound slots: "
            "startup, nav, alert, launch, close, alarm, timer, shutdown, and background music. "
            "Each slot can use a tone sequence (frequencies in hertz and durations in "
            "milliseconds, comma separated) or an audio file. Slots may be left unassigned; "
            "unassigned slots fall back to the Modern theme sounds."
        )
        self.sound_slots = [
            ("startup", "startup"),
            ("shutdown", "shutdown"),
            ("nav", "nav"),
            ("alert", "alert"),
            ("launch", "launch"),
            ("close", "close"),
            ("alarm", "alarm"),
            ("timer", "timer"),
            ("background music", "background_music"),
        ]
        self.slot_labels = [label for label, _key in self.sound_slots]
        self.slot_keys = [key for _label, key in self.sound_slots]
        self.slot_to_label = {key: label for label, key in self.sound_slots}
        self.label_to_slot = {label: key for label, key in self.sound_slots}
        self.music_slot_key = "background_music"
        # Mode choices per slot type.
        self.tone_mode = "Tones"
        self.file_mode = "Audio File"
        self.none_mode = "No sound"
        self.music_file_mode = "Music file"
        self.music_none_mode = "No music"
        self.theme_name = ""
        self.theme_data = {}
        self.is_new_theme = False
        self.current_slot_key = None

        self.title_label = None
        self.subtitle_label = None
        self.theme_name_input = None
        self.sound_list = None
        self.assignment_display = None
        self.mode_choice = None
        self.freq_input = None
        self.duration_input = None
        self.file_path_input = None
        self.browse_button = None
        self.play_button = None
        self.save_button = None

    def run(self):
        self.theme_name = ""
        self.theme_data = {}
        self.is_new_theme = False
        self._show_launcher()

    def _discard_active_frame(self):
        if self.frame:
            try:
                self.frame.Unbind(wx.EVT_CLOSE)
            except Exception:
                pass
            active = self.frame
            self.frame = None
            active.Destroy()

    def _base_panel(self, title, subtitle, size=(560, 460)):
        self._discard_active_frame()
        self.frame = wx.Frame(None, title=title, size=size)
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.title_label = wx.StaticText(panel, label=title)
        self.title_label.SetForegroundColour(wx.Colour(255, 255, 255))
        self.title_label.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(self.title_label, 0, wx.ALL | wx.CENTER, 14)

        self.subtitle_label = wx.StaticText(panel, label=subtitle)
        self.subtitle_label.SetForegroundColour(wx.Colour(210, 210, 210))
        self.subtitle_label.Wrap(520)
        sizer.Add(self.subtitle_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(sizer)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        return panel, sizer

    def _show_launcher(self):
        panel, sizer = self._base_panel(
            "Theme Creator",
            "Choose whether to create a new theme or open an existing theme to edit.",
            size=(520, 260),
        )

        create_button = wx.Button(panel, label="&Create New Theme")
        open_button = wx.Button(panel, label="&Open Theme")

        create_button.SetBackgroundColour(wx.Colour(30, 90, 30))
        create_button.SetForegroundColour(wx.Colour(255, 255, 255))
        open_button.SetBackgroundColour(wx.Colour(40, 40, 90))
        open_button.SetForegroundColour(wx.Colour(255, 255, 255))

        create_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Create New Theme"))
        open_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Open Theme"))

        sizer.Add(create_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(open_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        create_button.Bind(wx.EVT_BUTTON, self.on_create_new_theme)
        open_button.Bind(wx.EVT_BUTTON, self.on_open_theme)

        self.frame.Show()
        create_button.SetFocus()
        self.api.speak("Theme Creator opened. Choose Create New Theme or Open Theme.")

    def on_create_new_theme(self, event=None):
        self.is_new_theme = True
        self.theme_name = ""
        self.theme_data = {}
        self._show_theme_name_screen()

    def _show_theme_name_screen(self):
        panel, sizer = self._base_panel(
            "Theme Creator",
            "Enter name for new theme:",
            size=(520, 240),
        )

        self.theme_name_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.theme_name_input.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.theme_name_input.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Theme name"))
        next_button = wx.Button(panel, label="&Next")
        next_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Next"))

        sizer.Add(self.theme_name_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        sizer.Add(next_button, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        self.theme_name_input.Bind(wx.EVT_TEXT_ENTER, self.on_confirm_theme_name)
        next_button.Bind(wx.EVT_BUTTON, self.on_confirm_theme_name)

        self.frame.Show()
        self.theme_name_input.SetFocus()
        self.api.speak("Enter a name for your new theme, then press Next.")

    def on_confirm_theme_name(self, event=None):
        proposed_name = self.theme_name_input.GetValue().strip()
        ok, error = is_valid_theme_name(proposed_name)
        if not ok:
            self.api.speak(error)
            self.theme_name_input.SetFocus()
            return
        if proposed_name in self.api.sounds.themes:
            self.api.speak("That theme already exists. Use Open Theme to edit it, or choose a different name.")
            self.theme_name_input.SetFocus()
            return

        self.theme_name = proposed_name
        self.theme_data = {}
        self._show_theme_editor()

    def on_open_theme(self, event=None):
        theme_names = sorted(self.api.sounds.get_available_themes(), key=str.lower)
        if not theme_names:
            self.api.speak("There are no themes to open.")
            return

        dialog = wx.SingleChoiceDialog(
            self.frame,
            "Choose a theme to edit.",
            "Open Theme",
            theme_names,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                self.api.speak("Open Theme cancelled.")
                return
            selected = dialog.GetStringSelection()
        finally:
            dialog.Destroy()

        self.is_new_theme = False
        self.theme_name = selected
        self.theme_data = copy.deepcopy(self.api.sounds.themes.get(selected, {}))
        self._show_theme_editor()

    # ------------------------------------------------------------------
    # Theme editor
    # ------------------------------------------------------------------

    def _show_theme_editor(self):
        panel, sizer = self._base_panel(
            "Theme Creator",
            f"Theme name: {self.theme_name}",
            size=(680, 620),
        )

        list_label = wx.StaticText(panel, label="Theme sounds:")
        list_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.sound_list = wx.ListBox(panel, choices=self.slot_labels, style=wx.LB_SINGLE)
        self.sound_list.SetBackgroundColour(wx.Colour(20, 20, 20))
        self.sound_list.SetForegroundColour(wx.Colour(255, 255, 255))
        self.sound_list.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Theme sounds list"))
        sizer.Add(self.sound_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        current_label = wx.StaticText(panel, label="Current sound assignment:")
        current_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(current_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.assignment_display = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.assignment_display.SetBackgroundColour(wx.Colour(25, 25, 25))
        self.assignment_display.SetForegroundColour(wx.Colour(220, 220, 220))
        self.assignment_display.SetMinSize((-1, 90))
        self.assignment_display.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Current sound assignment"))
        sizer.Add(self.assignment_display, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        mode_label = wx.StaticText(panel, label="Sound type:")
        mode_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(mode_label, 0, wx.LEFT | wx.RIGHT, 8)
        self.mode_choice = wx.Choice(panel, choices=[])
        self.mode_choice.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.mode_choice.SetForegroundColour(wx.Colour(255, 255, 255))
        self.mode_choice.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Sound type"))
        self.mode_choice.Bind(wx.EVT_CHOICE, self.on_mode_changed)
        sizer.Add(self.mode_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.freq_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.freq_input.SetHint("Frequencies in Hz, e.g. 440, 660, 0")
        self.freq_input.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Frequencies"))
        self.freq_input.Bind(wx.EVT_TEXT_ENTER, self.on_tone_input_enter)
        sizer.Add(self.freq_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.duration_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.duration_input.SetHint("Durations in ms, e.g. 200, 300, 500")
        self.duration_input.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Durations"))
        self.duration_input.Bind(wx.EVT_TEXT_ENTER, self.on_tone_input_enter)
        sizer.Add(self.duration_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        file_row = wx.BoxSizer(wx.HORIZONTAL)
        self.file_path_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.file_path_input.SetHint("Type a file path or click Browse")
        self.file_path_input.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("File path"))
        self.file_path_input.Bind(wx.EVT_TEXT_ENTER, self.on_file_path_enter)
        file_row.Add(self.file_path_input, 1, wx.EXPAND | wx.RIGHT, 8)
        self.browse_button = wx.Button(panel, label="&Browse...")
        self.browse_button.Bind(wx.EVT_BUTTON, self.on_browse_file)
        self.browse_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Browse"))
        file_row.Add(self.browse_button, 0)
        sizer.Add(file_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self.play_button = wx.Button(panel, label="&Play")
        self.save_button = wx.Button(panel, label="&Save Theme")
        self.play_button.SetBackgroundColour(wx.Colour(40, 40, 90))
        self.play_button.SetForegroundColour(wx.Colour(255, 255, 255))
        self.save_button.SetBackgroundColour(wx.Colour(0, 100, 0))
        self.save_button.SetForegroundColour(wx.Colour(255, 255, 255))
        self.play_button.Bind(wx.EVT_BUTTON, self.on_play_slot)
        self.play_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Play"))
        self.save_button.Bind(wx.EVT_BUTTON, self.on_save_theme)
        self.save_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Save Theme"))
        button_row.Add(self.play_button, 0, wx.RIGHT, 10)
        button_row.AddStretchSpacer(1)
        button_row.Add(self.save_button, 0)
        sizer.Add(button_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.sound_list.Bind(wx.EVT_LISTBOX, self.on_sound_selected)
        self.sound_list.Bind(wx.EVT_LISTBOX_DCLICK, self.on_play_slot)

        self.frame.Show()
        if self.slot_labels:
            self.sound_list.SetSelection(0)
            self._load_slot_into_editor()
        self.sound_list.SetFocus()
        self.api.speak(
            f"Editing theme {self.theme_name}. Select a sound, choose tones or audio file, "
            "then press Play to preview or Save Theme to save."
        )

    def _slot_modes(self, slot_key):
        if slot_key == self.music_slot_key:
            return [self.music_file_mode, self.music_none_mode]
        return [self.tone_mode, self.file_mode, self.none_mode]

    def _slot_value_mode(self, slot_key):
        """Which mode label matches the slot's current value."""
        value = self.theme_data.get(slot_key)
        if slot_key == self.music_slot_key:
            if isinstance(value, str) and value and value != "None":
                return self.music_file_mode
            return self.music_none_mode
        if isinstance(value, list) and value:
            return self.tone_mode
        if isinstance(value, str) and value:
            return self.file_mode
        return self.none_mode

    def _load_slot_into_editor(self):
        """Populate mode choice and inputs for the selected slot."""
        slot_key = self.get_selected_slot_key()
        self.current_slot_key = slot_key
        if not slot_key:
            self.mode_choice.Clear()
            self._hide_slot_inputs()
            self.assignment_display.SetValue("Select a sound name first.")
            return

        modes = self._slot_modes(slot_key)
        self.mode_choice.Set(modes)
        current_mode = self._slot_value_mode(slot_key)
        if current_mode in modes:
            self.mode_choice.SetStringSelection(current_mode)
        else:
            self.mode_choice.SetSelection(0)
            current_mode = modes[0]
        self._apply_mode_inputs(slot_key, current_mode)
        self._refresh_assignment_display()

    def _hide_slot_inputs(self):
        self.freq_input.Hide()
        self.duration_input.Hide()
        self.file_path_input.Hide()
        self.browse_button.Hide()

    def _apply_mode_inputs(self, slot_key, mode_label):
        """Show the inputs for the chosen mode and prefill them."""
        value = self.theme_data.get(slot_key)
        if mode_label in (self.tone_mode,):
            notes = value if isinstance(value, list) else []
            frequencies, durations = notes_to_tone_string(notes)
            self.freq_input.SetValue(frequencies)
            self.duration_input.SetValue(durations)
            self.freq_input.Show()
            self.duration_input.Show()
            self.file_path_input.Hide()
            self.browse_button.Hide()
        elif mode_label in (self.file_mode, self.music_file_mode):
            path = value if isinstance(value, str) and value != "None" else ""
            self.file_path_input.SetValue(path)
            self.freq_input.Hide()
            self.duration_input.Hide()
            self.file_path_input.Show()
            self.browse_button.Show()
        else:
            self._hide_slot_inputs()
        self.frame.Layout()

    def on_mode_changed(self, event=None):
        slot_key = self.get_selected_slot_key()
        if not slot_key:
            return
        mode_label = self.mode_choice.GetStringSelection()
        # Preserve anything typed but not yet confirmed before switching modes.
        if mode_label != self.tone_mode:
            self._commit_tone_inputs(announce=False)
        if mode_label not in (self.file_mode, self.music_file_mode):
            self._commit_file_path(announce=False)
        # "No sound" clears the slot's assignment; unassigned slots fall back
        # to the Modern theme sound at playback time.
        if mode_label in (self.none_mode, self.music_none_mode):
            self.theme_data.pop(slot_key, None)
        self._apply_mode_inputs(slot_key, mode_label)
        if not self.api.is_enhanced_mode():
            self.api.speak(mode_label)

    def on_tone_input_enter(self, event=None):
        self._commit_tone_inputs(announce=True)

    def on_file_path_enter(self, event=None):
        self._commit_file_path(announce=True)

    def _commit_tone_inputs(self, announce=False):
        """Parse the tone inputs for the current slot; assign on success."""
        slot_key = self.current_slot_key or self.get_selected_slot_key()
        if not slot_key or slot_key == self.music_slot_key:
            return False
        notes, error = parse_tone_string(
            self.freq_input.GetValue(), self.duration_input.GetValue()
        )
        if error:
            if announce:
                self.api.speak(error)
            return False
        self.theme_data[slot_key] = notes
        self._refresh_assignment_display()
        if announce:
            self.api.speak(f"{self.slot_to_label.get(slot_key, slot_key)} tone sequence saved in the editor.")
        return True

    def _commit_file_path(self, announce=False):
        """Assign the typed file path for the current slot."""
        slot_key = self.current_slot_key or self.get_selected_slot_key()
        if not slot_key:
            return False
        path = self.file_path_input.GetValue().strip()
        if not path:
            if announce:
                self.api.speak("Enter a file path or browse for one.")
            return False
        if not os.path.exists(path):
            if announce:
                self.api.speak("That file does not exist.")
            return False
        self.theme_data[slot_key] = path
        self._refresh_assignment_display()
        if announce:
            self.api.speak(f"{self.slot_to_label.get(slot_key, slot_key)} set to {os.path.basename(path)}.")
        return True

    def on_sound_selected(self, event=None):
        self._load_slot_into_editor()
        slot_key = self.get_selected_slot_key()
        if slot_key and not self.api.is_enhanced_mode():
            slot_label = self.slot_to_label.get(slot_key, slot_key)
            self.api.speak(f"{slot_label} selected. {self._assignment_summary(slot_key)}")

    def get_selected_slot_key(self):
        if not self.sound_list:
            return None
        selection = self.sound_list.GetSelection()
        if selection == wx.NOT_FOUND:
            return None
        return self.label_to_slot.get(self.sound_list.GetString(selection))

    def _assignment_summary(self, slot_key):
        value = self.theme_data.get(slot_key)
        if slot_key == self.music_slot_key:
            if isinstance(value, str) and value and value != "None":
                return f"Music file {os.path.basename(value)}."
            return "No music."
        if isinstance(value, str) and value:
            return f"Assigned to file {os.path.basename(value)}."
        if isinstance(value, list) and value:
            return f"Tone sequence with {len(value)} notes."
        return "Not assigned. Falls back to the Modern theme sound."

    def _format_assignment(self, slot_key):
        slot_label = self.slot_to_label.get(slot_key, slot_key)
        value = self.theme_data.get(slot_key)
        if slot_key == self.music_slot_key:
            if isinstance(value, str) and value and value != "None":
                return f"Sound: {slot_label}\nType: Audio file\nPath: {value}"
            return f"Sound: {slot_label}\nType: No music"
        if isinstance(value, str) and value:
            return f"Sound: {slot_label}\nType: Audio file\nPath: {value}"
        if isinstance(value, list) and value:
            frequencies, durations = notes_to_tone_string(value)
            return f"Sound: {slot_label}\nType: Tone sequence\nFrequencies: {frequencies}\nDurations: {durations}"
        return f"Sound: {slot_label}\nType: Not assigned (uses Modern fallback)"

    def _refresh_assignment_display(self):
        slot_key = self.get_selected_slot_key()
        if not slot_key:
            self.assignment_display.SetValue("Select a sound name first.")
            return
        self.assignment_display.SetValue(self._format_assignment(slot_key))

    def on_browse_file(self, event=None):
        slot_key = self.get_selected_slot_key()
        if not slot_key:
            self.api.speak("Select a sound name before browsing for a file.")
            return
        slot_label = self.slot_to_label.get(slot_key, slot_key)

        wildcard = (
            "Audio files (*.wav;*.mp3;*.ogg;*.flac;*.aif;*.aiff)|*.wav;*.mp3;*.ogg;*.flac;*.aif;*.aiff|"
            "WAV files (*.wav)|*.wav|MP3 files (*.mp3)|*.mp3|"
            "OGG files (*.ogg)|*.ogg|FLAC files (*.flac)|*.flac|"
            "All files (*.*)|*.*"
        )
        selected_path = self.api.choose_file(
            self.frame, "open", f"Choose audio file for {slot_label}", wildcard
        )
        if not selected_path:
            self.api.speak("Browse cancelled.")
            return

        self.theme_data[slot_key] = selected_path
        if self.mode_choice.GetStringSelection() in (self.file_mode, self.music_file_mode):
            self.file_path_input.SetValue(selected_path)
        self._refresh_assignment_display()
        self.api.speak(f"{slot_label} set to {os.path.basename(selected_path)}.")

    def on_play_slot(self, event=None):
        """Audition the selected slot's current assignment without saving."""
        slot_key = self.get_selected_slot_key()
        if not slot_key:
            self.api.speak("Select a sound name first.")
            return
        slot_label = self.slot_to_label.get(slot_key, slot_key)
        mode_label = self.mode_choice.GetStringSelection() if self.mode_choice else ""

        # Work on a pending copy so unsaved edits preview without committing.
        pending = dict(self.theme_data)
        if mode_label == self.tone_mode:
            notes, error = parse_tone_string(
                self.freq_input.GetValue(), self.duration_input.GetValue()
            )
            if error:
                self.api.speak(error)
                return
            pending[slot_key] = notes
        elif mode_label in (self.file_mode, self.music_file_mode):
            path = self.file_path_input.GetValue().strip()
            if path and not os.path.exists(path):
                self.api.speak("That file does not exist.")
                return
            if path:
                pending[slot_key] = path

        value = pending.get(slot_key)
        if slot_key == self.music_slot_key:
            value = None if value in (None, "None") else value
        if not value:
            self.api.speak(f"{slot_label} has no sound to play.")
            return
        if self.api.sounds.play_preview(value):
            self.api.speak(f"Playing {slot_label}.", interrupt=False)
        else:
            self.api.speak(f"Could not play {slot_label}.")

    def _validate_theme_before_save(self):
        for slot_label, slot_key in self.sound_slots:
            value = self.theme_data.get(slot_key)
            if value is None or value == "" or value == "None":
                continue
            if isinstance(value, str) and not os.path.exists(value):
                raise ValueError(f"The file for {slot_label} does not exist anymore.")

    def on_save_theme(self, event=None):
        # Commit anything typed but not yet confirmed before validating.
        if self.mode_choice and self.mode_choice.GetStringSelection() == self.tone_mode:
            self._commit_tone_inputs(announce=False)
        elif self.mode_choice and self.mode_choice.GetStringSelection() == self.file_mode:
            self._commit_file_path(announce=False)
        elif self.mode_choice and self.mode_choice.GetStringSelection() == self.music_file_mode:
            self._commit_file_path(announce=False)

        try:
            self._validate_theme_before_save()
        except ValueError as error:
            self.api.speak(str(error))
            return

        ok, error = is_valid_theme_name(self.theme_name)
        if not ok:
            self.api.speak(error)
            return

        self.api.sounds.themes[self.theme_name] = copy.deepcopy(self.theme_data)
        self.api.sounds.save_custom_themes()
        self.api.sounds.save_theme_name(self.theme_name)
        self.api.sounds.current_theme = self.theme_name
        self.api.speak(f"Theme {self.theme_name} saved and applied.")
        self.on_close()
