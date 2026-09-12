import wx
import os
import datetime
import subprocess
import sys
import platform
import json
import threading
import time
import speech
from api import BlindApp
import audio_devices
from platform_support import open_external_file

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False

class SettingsApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "System Settings"
        self.description = "Configure speech, audio devices, themes, and updates."
        self.category = "System"
        self.help_text = "Use Tab to navigate between tabs and controls. Switch tabs with Ctrl+Tab."
        self.docs = "Settings lets you adjust speech rate, choose a speech backend, configure audio devices, and manage sound themes."
        self.device_config_path = self.api.get_data_path("device_config.json")
        self.input_entries = []
        self.output_entries = []
        self.platform_name = platform.system()
        self.music_files = []
        self.music_choice_values = []

    def run(self):
        self.frame = wx.Frame(None, title="Settings", size=(550, 600))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(30, 30, 30))
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        title = wx.StaticText(panel, label="System Settings")
        title.SetForegroundColour(wx.Colour(255, 255, 255))
        title.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(title, 0, wx.ALL | wx.CENTER, 10)

        self.notebook = wx.Notebook(panel)
        self.notebook.SetBackgroundColour(wx.Colour(30, 30, 30))
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_tab_changed)

        self._build_speech_tab()
        if HAS_SOUNDDEVICE:
            self._build_audio_tab()
        self._build_theme_tab()
        self._build_updates_tab()

        sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)

        close_btn = wx.Button(panel, label="&Save and Close")
        close_btn.SetBackgroundColour(wx.Colour(0, 100, 0))
        close_btn.SetForegroundColour(wx.Colour(255, 255, 255))
        close_btn.Bind(wx.EVT_BUTTON, self.on_close)
        close_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Save and Close"))
        sizer.Add(close_btn, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)

        panel.SetSizer(sizer)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        self.frame.Show()
        self.notebook.SetFocus()
        self.api.speak("Settings opened with tabs. Use Ctrl+Tab to switch between Speech, Audio, Theme, and Updates.")

    def _make_tab_panel(self):
        panel = wx.Panel(self.notebook)
        panel.SetBackgroundColour(wx.Colour(20, 20, 20))
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)
        return panel, sizer

    def on_tab_changed(self, event):
        idx = self.notebook.GetSelection()
        labels = []
        for i in range(self.notebook.GetPageCount()):
            labels.append(self.notebook.GetPageText(i))
        if 0 <= idx < len(labels):
            self.api.speak(f"{labels[idx]} settings")

    def _build_speech_tab(self):
        panel, sizer = self._make_tab_panel()
        
        voice_label = wx.StaticText(panel, label="Voice Speed:")
        voice_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(voice_label, 0, wx.ALL, 10)
        
        self.speed_slider = wx.Slider(panel, value=200, minValue=50, maxValue=400, style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        self.speed_slider.SetValue(getattr(self.api.engine, "get_rate", lambda: 200)())
        self.speed_slider.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.speed_slider.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak(f"Voice speed: {self.speed_slider.GetValue()}"))
        sizer.Add(self.speed_slider, 0, wx.EXPAND | wx.ALL, 10)

        speech_label = wx.StaticText(panel, label="Speech Engine:")
        speech_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(speech_label, 0, wx.ALL, 10)

        self.speech_modes = getattr(self.api.engine, "get_available_modes", lambda: [("Auto", "auto")])()
        self.speech_choice = wx.Choice(panel, choices=[m[0] for m in self.speech_modes])
        current_mode = getattr(self.api.engine, "get_mode", lambda: "auto")()
        idx = next((i for i, m in enumerate(self.speech_modes) if m[1] == current_mode), 0)
        self.speech_choice.SetSelection(idx)
        self.speech_choice.Bind(wx.EVT_CHOICE, self.on_speech_mode_change)
        self.speech_choice.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Speech engine"))
        sizer.Add(self.speech_choice, 0, wx.EXPAND | wx.ALL, 8)

        self.notebook.AddPage(panel, "Speech")

    def _build_audio_tab(self):
        panel, sizer = self._make_tab_panel()

        config = self.load_device_config()

        input_label = wx.StaticText(panel, label="Input Device (Microphone):")
        input_label.SetForegroundColour(wx.Colour(200, 200, 200))
        sizer.Add(input_label, 0, wx.ALL, 8)

        self.input_entries = self.get_input_devices()
        input_labels = [self._device_label(d) for d in self.input_entries] or ["Default"]
        self.input_choice = wx.Choice(panel, choices=input_labels)
        self.input_choice.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.input_choice.SetForegroundColour(wx.Colour(255, 255, 255))
        self.input_choice.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Input device"))
        selected_input_index = audio_devices.resolve_selected_index(
            self.input_entries, config, "input_device_index", "input_device"
        )
        if selected_input_index is not None:
            for i, entry in enumerate(self.input_entries):
                if entry["index"] == selected_input_index:
                    self.input_choice.SetSelection(i)
                    break
            else:
                self.input_choice.SetSelection(0)
        else:
            self.input_choice.SetSelection(0)
        sizer.Add(self.input_choice, 0, wx.EXPAND | wx.ALL, 8)

        output_label = wx.StaticText(panel, label="Output Device (Speaker):")
        output_label.SetForegroundColour(wx.Colour(200, 200, 200))
        sizer.Add(output_label, 0, wx.ALL, 8)

        self.output_entries = self.get_output_devices()
        output_labels = [self._device_label(d) for d in self.output_entries] or ["Default"]
        self.output_choice = wx.Choice(panel, choices=output_labels)
        self.output_choice.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.output_choice.SetForegroundColour(wx.Colour(255, 255, 255))
        self.output_choice.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Output device"))
        selected_output_index = audio_devices.resolve_selected_index(
            self.output_entries, config, "output_device_index", "output_device"
        )
        if selected_output_index is not None:
            for i, entry in enumerate(self.output_entries):
                if entry["index"] == selected_output_index:
                    self.output_choice.SetSelection(i)
                    break
            else:
                self.output_choice.SetSelection(0)
        else:
            self.output_choice.SetSelection(0)
        sizer.Add(self.output_choice, 0, wx.EXPAND | wx.ALL, 8)

        test_btn = wx.Button(panel, label="&Test Audio")
        test_btn.SetBackgroundColour(wx.Colour(50, 50, 100))
        test_btn.SetForegroundColour(wx.Colour(255, 255, 255))
        test_btn.Bind(wx.EVT_BUTTON, self.on_test_audio)
        test_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Test Audio"))
        sizer.Add(test_btn, 0, wx.EXPAND | wx.ALL, 8)

        self.notebook.AddPage(panel, "Audio")

    def _build_theme_tab(self):
        panel, sizer = self._make_tab_panel()

        theme_label = wx.StaticText(panel, label="Sound Theme:")
        theme_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(theme_label, 0, wx.ALL, 10)

        themes = self.api.sounds.get_available_themes()
        self.theme_choice = wx.Choice(panel, choices=themes if themes else ["Modern"])
        self.theme_choice.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.theme_choice.SetForegroundColour(wx.Colour(255, 255, 255))
        self.theme_choice.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Sound theme"))
        current_theme = self.api.sounds.current_theme
        if current_theme in themes:
            self.theme_choice.SetSelection(themes.index(current_theme))
        else:
            self.theme_choice.SetSelection(0)
        self.theme_choice.Bind(wx.EVT_CHOICE, self.on_theme_preview)
        sizer.Add(self.theme_choice, 0, wx.EXPAND | wx.ALL, 8)

        music_label = wx.StaticText(panel, label="Background Music:")
        music_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(music_label, 0, wx.ALL, 10)

        self.music_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "music")
        if os.path.isdir(self.music_dir):
            self.music_files = sorted(
                [f for f in os.listdir(self.music_dir) if f.lower().endswith(('.wav', '.aif', '.mp3', '.ogg'))],
                key=str.lower,
            )
        else:
            self.music_files = []
        self.music_choice_values = ["None"] + self.music_files
        self.music_choice = wx.Choice(panel, choices=[self._music_choice_label(value) for value in self.music_choice_values])
        self.music_choice.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.music_choice.SetForegroundColour(wx.Colour(255, 255, 255))
        self.music_choice.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Background music"))
        self.music_choice.Bind(wx.EVT_CHOICE, self.on_music_changed)
        self.music_config_path = self.api.sounds.music_config_path
        selected_music = self.api.sounds.load_background_music()
        self._set_music_choice_value(selected_music)
        sizer.Add(self.music_choice, 0, wx.EXPAND | wx.ALL, 8)

        volume_box = wx.StaticBox(panel, label="Background Music Volume")
        volume_sizer = wx.StaticBoxSizer(volume_box, wx.VERTICAL)
        self.music_volume = wx.Slider(panel, value=50, minValue=0, maxValue=100, style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        self.music_volume.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.music_volume.SetValue(self._load_music_volume())
        self.music_volume.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak(f"Background music volume: {self.music_volume.GetValue()}"))
        volume_sizer.Add(self.music_volume, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(volume_sizer, 0, wx.EXPAND | wx.ALL, 10)

        self.notebook.AddPage(panel, "Theme")

    def _build_updates_tab(self):
        panel, sizer = self._make_tab_panel()

        info = wx.StaticText(panel, label="Check for updates to keep PyOS up to date.")
        info.SetForegroundColour(wx.Colour(200, 200, 200))
        sizer.Add(info, 0, wx.ALL, 15)

        self.update_output = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self.update_output.SetBackgroundColour(wx.Colour(20, 20, 20))
        self.update_output.SetForegroundColour(wx.Colour(180, 255, 180))
        self.update_output.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Update output"))
        sizer.Add(self.update_output, 1, wx.EXPAND | wx.ALL, 10)

        self.update_gauge = wx.Gauge(panel, range=100, size=(-1, 20))
        self.update_gauge.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.update_gauge.SetCanFocus(True)
        self.update_gauge.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak(f"Update progress: {self.update_gauge.GetValue()} percent"), e.Skip()))
        self.update_gauge.Hide()
        sizer.Add(self.update_gauge, 0, wx.EXPAND | wx.ALL, 10)

        self.update_btn = wx.Button(panel, label="&Check for Updates")
        self.update_btn.SetBackgroundColour(wx.Colour(50, 50, 50))
        self.update_btn.SetForegroundColour(wx.Colour(255, 255, 255))
        self.update_btn.Bind(wx.EVT_BUTTON, self.check_updates)
        self.update_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Check for Updates"))
        sizer.Add(self.update_btn, 0, wx.EXPAND | wx.ALL, 10)

        self.notebook.AddPage(panel, "Updates")

    def get_input_devices(self):
        """Get list of available input devices."""
        try:
            return audio_devices.list_input_devices()
        except Exception as e:
            print(f"Error querying input devices: {e}")
            return []

    def get_output_devices(self):
        """Get list of available output devices."""
        try:
            return audio_devices.list_output_devices()
        except Exception as e:
            print(f"Error querying output devices: {e}")
            return []

    def _device_label(self, device_entry):
        return f"{device_entry['name']}"

    def load_device_config(self):
        """Load device configuration from file."""
        return audio_devices.load_device_config(self.api.data_dir)

    def save_device_config(self, input_device, output_device):
        """Save device configuration to file."""
        try:
            audio_devices.save_device_config(self.api.data_dir, input_device, output_device)
        except Exception as e:
            print(f"Error saving device config: {e}")

    def on_test_audio(self, event):
        """Test audio output."""
        self.api.speak("Testing audio. You should hear a sound.")
        self.api.play_sound("startup")

    def on_theme_preview(self, event):
        theme_name = self.theme_choice.GetStringSelection()
        if theme_name:
            self.api.sounds.current_theme = theme_name
            theme_music = self.api.sounds.get_theme_background_music(theme_name)
            if theme_music is not None:
                self._set_music_choice_value(theme_music)
            self.api.play_sound("startup")
            if not self.api.is_enhanced_mode():
                self.api.speak(theme_name)

    def _music_choice_label(self, music_value):
        if not music_value or music_value == "None":
            return "None"
        if music_value in self.music_files:
            return music_value
        base_name = os.path.basename(music_value)
        return f"Custom: {base_name or music_value}"

    def _set_music_choice_value(self, music_value):
        normalized = music_value if music_value else "None"
        try:
            index = self.music_choice_values.index(normalized)
        except ValueError:
            self.music_choice_values.append(normalized)
            self.music_choice.Append(self._music_choice_label(normalized))
            index = len(self.music_choice_values) - 1
        self.music_choice.SetSelection(index)

    def _get_selected_music_value(self):
        selection = self.music_choice.GetSelection()
        if selection == wx.NOT_FOUND:
            return "None"
        if 0 <= selection < len(self.music_choice_values):
            return self.music_choice_values[selection]
        return "None"

    def _load_music_volume(self):
        try:
            with open(self.music_config_path, "r") as f:
                config = json.load(f)
            return config.get("volume", 50)
        except Exception:
            return 50

    def _save_music_volume(self):
        try:
            config = {}
            if os.path.exists(self.music_config_path):
                with open(self.music_config_path, "r") as f:
                    config = json.load(f)
            config["volume"] = self.music_volume.GetValue()
            with open(self.music_config_path, "w") as f:
                json.dump(config, f)
        except Exception:
            pass

    def on_music_changed(self, event):
        selected_music = self._get_selected_music_value()
        self.api.sounds.save_background_music(selected_music)
        self._save_music_volume()
        name = selected_music.replace(".aif", "").replace(".wav", "").replace("_", " ").strip()
        if not self.api.is_enhanced_mode():
            self.api.speak(f"Music: {name}" if name != "None" else "Music: None")

    def _apply_speech_mode_selection(self, announce=True):
        sel = self.speech_choice.GetSelection()
        if sel < 0 or sel >= len(self.speech_modes):
            return
        speech_mode = self.speech_modes[sel][1]
        mode_ok = getattr(self.api.engine, "set_mode", lambda _m: False)(speech_mode)
        if not announce:
            return
        if speech_mode == "nvda" and not getattr(self.api.engine, "use_nvda", False):
            self.api.speak("NVDA is not active, so speech is using the system voice until NVDA is available.", interrupt=False)
        elif mode_ok:
            spoken_name = next((label for label, value in self.speech_modes if value == speech_mode), speech_mode)
            self.api.speak(f"Speech mode switched to {spoken_name}.", interrupt=False)
        else:
            self.api.speak("Could not switch speech mode.", interrupt=False)

    def on_speech_mode_change(self, event):
        if not self.api.is_enhanced_mode():
            self._apply_speech_mode_selection(announce=True)

    def check_updates(self, event):
        self.update_btn.Disable()
        self.update_gauge.SetValue(0)
        self.update_gauge.Show()
        self.api.speak("Checking for updates...")
        threading.Thread(target=self._update_thread, daemon=True).start()

    def _update_thread(self):
        def set_gauge(val):
            wx.CallAfter(self.update_gauge.SetValue, val)
        def log(msg):
            wx.CallAfter(self.update_output.AppendText, msg + "\n")
            wx.CallAfter(self.api.speak, msg)
        def hide_gauge():
            wx.CallAfter(self.update_gauge.Hide)
            wx.CallAfter(self.update_btn.Enable)

        try:
            log("Setting remote URL...")
            set_gauge(10)
            subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/wasilewsk/py-os.git"],
                           check=True, capture_output=True, text=True)
            set_gauge(25)

            log("Checking branch...")
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, check=True,
            )
            branch_name = branch_result.stdout.strip() or "master"
            set_gauge(35)

            log("Pulling updates...")
            result = subprocess.run(
                ["git", "pull", "origin", branch_name],
                capture_output=True, text=True, check=True,
            )
            set_gauge(60)

            if "Already up to date" in result.stdout:
                log("System is already up to date.")
            else:
                log("Core updates downloaded.")

            log("Updating requirements...")
            set_gauge(75)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                check=True, capture_output=True, text=True,
            )
            set_gauge(95)
            self.cleanup_deprecated_sound_artifacts()
            set_gauge(100)
            update_state = {"phase": 1}
            state_path = self.api.get_data_path("update_state.json")
            try:
                with open(state_path, "w") as f:
                    json.dump(update_state, f)
            except Exception:
                pass
            # Save music config before restart so background music isn't reset
            sel_music = self._get_selected_music_value()
            music_cfg = {"music": sel_music, "volume": self.music_volume.GetValue()}
            try:
                with open(self.music_config_path, "w") as f:
                    json.dump(music_cfg, f)
            except Exception:
                pass
            log("Update complete. Restarting...")
            time.sleep(1)
            subprocess.Popen([sys.executable, os.path.join(os.getcwd(), "desktop.py")], cwd=os.getcwd())
            hide_gauge()
            wx.CallAfter(self.frame.Close)
            wx.CallAfter(wx.GetApp().ExitMainLoop)

        except subprocess.CalledProcessError as e:
            err = e.stderr if e.stderr else "Check your internet connection or git status"
            log(f"Update failed: {err}")
            hide_gauge()
        except Exception as e:
            log(f"Error during update: {e}")
            hide_gauge()

    def cleanup_deprecated_sound_artifacts(self):
        """Clean stale/legacy sound-theme artifacts without resetting active settings."""
        candidates = [
            self.api.get_data_path("sound_theme_app_state.json"),
            self.api.get_data_path("theme_creator_draft.json"),
            self.api.get_data_path("theme_creator_step.tmp"),
        ]
        for path in candidates:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    def on_close(self, event=None):
        """Save settings and close."""
        # Ensure speech selection is persisted even if user didn't change focus after selecting.
        self._apply_speech_mode_selection(announce=False)
        getattr(self.api.engine, "set_rate", lambda _rate: False)(self.speed_slider.GetValue())

        if HAS_SOUNDDEVICE:
            input_sel = self.input_choice.GetSelection()
            output_sel = self.output_choice.GetSelection()
            input_device = self.input_entries[input_sel] if 0 <= input_sel < len(self.input_entries) else {"index": None, "name": "Default"}
            output_device = self.output_entries[output_sel] if 0 <= output_sel < len(self.output_entries) else {"index": None, "name": "Default"}
            self.save_device_config(input_device, output_device)
            self.api.speak(
                f"Settings saved. Audio devices: input {input_device.get('name', 'Default')}, output {output_device.get('name', 'Default')}."
            )
        selected_theme = self.theme_choice.GetStringSelection()
        if selected_theme:
            self.api.sounds.save_theme_name(selected_theme)
            self.api.sounds.current_theme = selected_theme
        
        # Save music selection
        selected_music = self._get_selected_music_value()
        self.api.sounds.save_background_music(selected_music)
        self._save_music_volume()

        if self.frame:
            self.frame.Destroy()
        self.api.sounds.play("close")
        self.api.desktop.on_app_closed(self)

class FileExplorerApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "File Explorer"
        self.description = "Browse your files."
        self.category = "System"
        self.help_text = "Use Arrow keys to navigate, Enter to open, and Backspace to go up."
        self.docs = "File Explorer provides access to the PyOS Drive and the host file system."
        self.vfs_root = os.path.abspath(self.api.get_vfs().root_dir)
        self.current_dir = None
        self.current_source = None
        self.history = []
        self.items = []
        self.platform_name = platform.system()

    def run(self):
        self.frame = wx.Frame(None, title="File Explorer - This PC", size=(700, 500))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- Navigation Toolbar ---
        nav_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.back_button = wx.Button(panel, label="&Back")
        self.up_button = wx.Button(panel, label="&Up")
        self.address_bar = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.address_bar.SetBackgroundColour(wx.Colour(20, 20, 20))
        self.address_bar.SetForegroundColour(wx.Colour(255, 255, 255))
        self.address_bar.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak(f"Address: {self.address_bar.GetValue()}"), e.Skip()))

        nav_sizer.Add(self.back_button, 0, wx.ALL, 5)
        nav_sizer.Add(self.up_button, 0, wx.ALL, 5)
        nav_sizer.Add(self.address_bar, 1, wx.EXPAND | wx.ALL, 5)
        
        main_sizer.Add(nav_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # --- File List ---
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.SetBackgroundColour(wx.Colour(20, 20, 20))
        self.list.SetForegroundColour(wx.Colour(255, 255, 255))
        self.list.InsertColumn(0, "Name", width=400)
        self.list.InsertColumn(1, "Type", width=100)
        main_sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)
        
        # --- Buttons ---
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        refresh_btn = wx.Button(panel, label="&Refresh")
        close_btn = wx.Button(panel, label="&Close")
        
        button_sizer.Add(refresh_btn, 0, wx.ALL, 5)
        button_sizer.AddStretchSpacer(1)
        button_sizer.Add(close_btn, 0, wx.ALL, 5)
        
        main_sizer.Add(button_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        panel.SetSizer(main_sizer)
        
        # Bindings
        self.back_button.Bind(wx.EVT_BUTTON, self.go_back)
        self.back_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Back"))
        self.up_button.Bind(wx.EVT_BUTTON, self.go_up)
        self.up_button.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Up"))
        self.address_bar.Bind(wx.EVT_TEXT_ENTER, self.go_to_address)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_item_activated)
        self.list.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.on_item_focused)
        self.list.Bind(wx.EVT_KEY_DOWN, self.on_key_down)
        self.list.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("File list"))
        refresh_btn.Bind(wx.EVT_BUTTON, lambda e: self.refresh_files())
        refresh_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Refresh"))
        close_btn.Bind(wx.EVT_BUTTON, self.on_close)
        close_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Close"))
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        
        self.refresh_files()
        self.frame.Show()
        self.api.speak("File Explorer opened.")
        self.list.SetFocus()

    def refresh_files(self):
        self.list.DeleteAllItems()
        self.items = []
        try:
            if self.current_source is None:
                self._add_item("PyOS Drive", True, self.vfs_root, "vfs")
                self._add_item("Host Files", True, os.path.abspath(os.sep), "host")
                self.address_bar.SetValue("This PC")
                self.frame.SetTitle("File Explorer - This PC")
                self.back_button.Enable(bool(self.history))
                return

            raw_items = os.listdir(self.current_dir)
            # Sort: folders first, then files
            raw_items.sort(key=lambda x: (not os.path.isdir(os.path.join(self.current_dir, x)), x.lower()))
            
            for i, name in enumerate(raw_items):
                full_path = os.path.join(self.current_dir, name)
                is_dir = os.path.isdir(full_path)
                self._add_item(name, is_dir, full_path, self.current_source)
            
            location_name = "PyOS Drive" if self.current_source == "vfs" and self.current_dir == self.vfs_root else self.current_dir
            self.address_bar.SetValue(location_name)
            self.frame.SetTitle(f"File Explorer - {location_name}")
            self.back_button.Enable(len(self.history) > 0)
        except Exception as e:
            self.api.speak(f"Error: {e}")

    def _add_item(self, name, is_dir, full_path, source):
        index = self.list.GetItemCount()
        self.list.InsertItem(index, name)
        self.list.SetItem(index, 1, "Folder" if is_dir else "File")
        self.items.append((name, is_dir, full_path, source))

    def go_to_path(self, path, source=None):
        if os.path.isdir(path):
            self.history.append((self.current_source, self.current_dir))
            self.current_dir = os.path.abspath(path)
            self.current_source = source or ("vfs" if self.current_dir == self.vfs_root or self.current_dir.startswith(self.vfs_root + os.sep) else "host")
            self.refresh_files()
            if self.items:
                self.list.SetItemState(0, wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED, wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED)
            self.api.speak(f"Entered {'PyOS Drive' if self.current_source == 'vfs' and self.current_dir == self.vfs_root else os.path.basename(self.current_dir) or self.current_dir}")

    def go_to_drives(self):
        self.history.append((self.current_source, self.current_dir))
        self.current_source = None
        self.current_dir = None
        self.refresh_files()
        self.api.speak("This PC")

    def go_back(self, event):
        if self.history:
            self.current_source, self.current_dir = self.history.pop()
            self.refresh_files()
            if self.current_source is None:
                self.api.speak("Back to This PC")
            else:
                self.api.speak(f"Back to {os.path.basename(self.current_dir) or self.current_dir}")

    def go_up(self, event):
        if self.current_source is None:
            return
        if self.current_source == "vfs" and self.current_dir == self.vfs_root:
            self.go_to_drives()
            return
        parent = os.path.dirname(self.current_dir)
        if parent != self.current_dir:
            self.go_to_path(parent)

    def go_to_address(self, event):
        path = self.address_bar.GetValue()
        if path.lower() in {"this pc", "computer"}:
            self.go_to_drives()
            return
        if path.lower() in {"pyos drive", "py-os drive"}:
            self.go_to_path(self.vfs_root, "vfs")
            return
        if os.path.isdir(path):
            self.go_to_path(path)
        else:
            self.api.speak("Invalid path.")

    def on_item_focused(self, event):
        index = event.GetIndex()
        if not self.api.is_enhanced_mode() and 0 <= index < len(self.items):
            name, is_dir, _, source = self.items[index]
            item_type = "Folder" if is_dir else "File"
            location = " in PyOS Drive" if source == "vfs" else ""
            self.api.speak(f"{name}, {item_type}{location}", interrupt=False)

    def on_item_activated(self, event):
        index = event.GetIndex()
        name, is_dir, full_path, source = self.items[index]
        
        if is_dir:
            self.go_to_path(full_path, source)
        else:
            self.api.speak(f"Opening {name}", interrupt=False)
            lower = name.lower()
            if lower.endswith((".txt", ".md", ".log", ".json", ".py", ".csv")):
                self.api.launch_app("TextEditorApp", file_path=full_path)
            elif lower.endswith((".wav", ".mp3", ".ogg", ".flac")):
                self.api.launch_app("AudioRecorderApp", file_path=full_path)
            else:
                try:
                    open_external_file(full_path)
                except Exception as e:
                    self.api.speak(f"Could not open file: {e}")

    def on_key_down(self, event):
        keycode = event.GetKeyCode()
        if keycode == wx.WXK_BACK:
            self.go_up(None)
        elif keycode == wx.WXK_LEFT and event.AltDown():
            self.go_back(None)
        else:
            event.Skip()

    def on_close(self, event=None):
        if self.frame: self.frame.Destroy()
        self.api.sounds.play("close")
        self.api.desktop.on_app_closed(self)

class ClockApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "Clock"
        self.description = "Check the current time and date."
        self.category = "System"
        self.help_text = "This app announces the time and closes automatically."
        self.docs = "Clock provides current system time and date information."

    def run(self):
        now = datetime.datetime.now()
        time_str = now.strftime("%I:%M %p")
        date_str = now.strftime("%A, %B %d, %Y")
        msg = f"It is currently {time_str} on {date_str}."
        self.api.speak(msg)
        wx.CallLater(3000, self.on_close)

class CalculatorApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "Calculator"
        self.description = "Basic math calculator with calculation history."
        self.category = "System"
        self.help_text = "Type an expression like '2 + 2' and press Enter. Use Up/Down arrows to review past calculations."
        self.docs = "Calculator supports basic arithmetic: addition (+), subtraction (-), multiplication (*), and division (/)."
        self.history = []
        self.history_index = -1

    def run(self):
        self.frame = wx.Frame(None, title="Calculator", size=(400, 120))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.input_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.input_ctrl.SetBackgroundColour(wx.Colour(30, 30, 30))
        self.input_ctrl.SetForegroundColour(wx.Colour(255, 255, 255))
        self.input_ctrl.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak("Expression input"), e.Skip()))
        sizer.Add(self.input_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(sizer)
        self.input_ctrl.Bind(wx.EVT_TEXT_ENTER, self.on_calc)
        self.input_ctrl.Bind(wx.EVT_KEY_DOWN, self.on_input_key)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        
        self.frame.Show()
        self.api.speak("Calculator opened. Enter an expression. Use Up and Down arrows to browse history.")
        self.input_ctrl.SetFocus()

    def on_input_key(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_UP and self.history:
            if self.history_index < len(self.history) - 1:
                self.history_index += 1
                expr = self.history[self.history_index][0]
                self.input_ctrl.SetValue(expr)
                self.input_ctrl.SetInsertionPointEnd()
                self.api.speak(expr)
            return
        elif key == wx.WXK_DOWN:
            if self.history_index > 0:
                self.history_index -= 1
                expr = self.history[self.history_index][0]
                self.input_ctrl.SetValue(expr)
                self.input_ctrl.SetInsertionPointEnd()
                self.api.speak(expr)
            elif self.history_index == 0:
                self.history_index = -1
                self.input_ctrl.Clear()
                self.api.speak("New expression")
            return
        event.Skip()

    def on_calc(self, event):
        expr = self.input_ctrl.GetValue()
        self.input_ctrl.Clear()
        try:
            result = eval(expr, {"__builtins__": None}, {})
            msg = f"Result: {result}"
            self.history.insert(0, (expr, str(result)))
        except Exception:
            msg = "Error: Invalid expression."
            self.history.insert(0, (expr, "Error"))
        
        self.history_index = -1
        self.api.speak(msg)

class TextEditorApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "Text Editor"
        self.description = "A simple text editor."
        self.category = "System"
        self.help_text = "Use standard text editing shortcuts. Save or Open files. F2 reads current line. Ctrl+F to find text."
        self.docs = "The Text Editor allows you to create, open, edit, and save text files."
        self.frame = None
        self.text_ctrl = None
        self.current_file_path = None
        self.status_bar = None

    def run(self, file_path=None):
        self.frame = wx.Frame(None, title="Text Editor", size=(600, 500))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(25, 25, 25))
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.text_ctrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_RICH)
        self.text_ctrl.SetBackgroundColour(wx.Colour(30, 30, 30))
        self.text_ctrl.SetForegroundColour(wx.Colour(220, 220, 220))
        self.text_ctrl.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Text editor"))
        main_sizer.Add(self.text_ctrl, 1, wx.EXPAND | wx.ALL, 5)
        
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        new_btn = wx.Button(panel, label="&New")
        open_btn = wx.Button(panel, label="&Open")
        save_btn = wx.Button(panel, label="&Save")
        close_btn = wx.Button(panel, label="&Close")
        
        button_sizer.Add(new_btn, 0, wx.ALL, 5)
        button_sizer.Add(open_btn, 0, wx.ALL, 5)
        button_sizer.Add(save_btn, 0, wx.ALL, 5)
        button_sizer.AddStretchSpacer(1)
        button_sizer.Add(close_btn, 0, wx.ALL, 5)
        
        main_sizer.Add(button_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        self.status_bar = wx.StaticText(panel, label="")
        self.status_bar.SetForegroundColour(wx.Colour(180, 180, 180))
        main_sizer.Add(self.status_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        
        panel.SetSizer(main_sizer)
        
        new_btn.Bind(wx.EVT_BUTTON, self.on_new)
        new_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("New"))
        open_btn.Bind(wx.EVT_BUTTON, self.on_open)
        open_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Open"))
        save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        save_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Save"))
        close_btn.Bind(wx.EVT_BUTTON, self.on_close)
        close_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Close"))
        self.text_ctrl.Bind(wx.EVT_KEY_DOWN, self.on_text_key)
        self.frame.Bind(wx.EVT_CHAR_HOOK, self.on_frame_key)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        
        self.frame.Show()
        self.api.speak("Text Editor opened.", interrupt=False)
        self.text_ctrl.SetFocus()
        
        if file_path:
            self.load_file(file_path)

    def load_file(self, file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                self.text_ctrl.SetValue(content)
                self.current_file_path = file_path
                self.frame.SetTitle(f"Text Editor - {os.path.basename(file_path)}")
                self.api.speak(f"Loaded file: {os.path.basename(file_path)}", interrupt=False)
        except Exception as e:
            self.api.speak(f"Error loading file: {e}")

    def on_open(self, event):
        path = self.api.open_file(self.frame, "Open Text File",
                                  "Text files (*.txt)|*.txt|All files (*.*)|*.*")
        if path:
            self.load_file(path)

    def on_save(self, event):
        if self.current_file_path:
            try:
                with open(self.current_file_path, 'w', encoding='utf-8') as f:
                    f.write(self.text_ctrl.GetValue())
                self.api.speak(f"File saved: {os.path.basename(self.current_file_path)}")
            except Exception as e:
                self.api.speak(f"Error saving file: {e}")
        else:
            self.on_save_as(event)

    def on_save_as(self, event):
        path = self.api.save_file(self.frame, "Save Text File As",
                                  "Text files (*.txt)|*.txt|All files (*.*)|*.*")
        if path:
            self.current_file_path = path
            try:
                with open(self.current_file_path, 'w', encoding='utf-8') as f:
                    f.write(self.text_ctrl.GetValue())
                self.frame.SetTitle(f"Text Editor - {os.path.basename(self.current_file_path)}")
                self.api.speak(f"File saved as: {os.path.basename(self.current_file_path)}")
            except Exception as e:
                self.api.speak(f"Error saving file: {e}")

    def on_new(self, event):
        self.text_ctrl.SetValue("")
        self.current_file_path = None
        self.frame.SetTitle("Text Editor")
        self.status_bar.SetLabel("")
        self.text_ctrl.SetFocus()
        self.api.speak("New document created.")

    def on_text_key(self, event):
        key = event.GetKeyCode()
        nav_keys = {wx.WXK_UP, wx.WXK_DOWN, wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_PAGEUP, wx.WXK_PAGEDOWN, wx.WXK_HOME, wx.WXK_END}
        if key in nav_keys:
            wx.CallAfter(self._speak_cursor_position)
        event.Skip()

    def on_frame_key(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_F2:
            col, line = self.text_ctrl.PositionToXY(self.text_ctrl.GetInsertionPoint())
            if line >= 0:
                line_text = self.text_ctrl.GetLineText(line)
                if line_text.strip():
                    self.api.speak(f"Line {line + 1}: {line_text}")
                else:
                    self.api.speak(f"Line {line + 1} is empty")
            return
        elif key == ord('F') and event.ControlDown():
            self.on_find()
            return
        event.Skip()

    def _speak_cursor_position(self):
        col, line = self.text_ctrl.PositionToXY(self.text_ctrl.GetInsertionPoint())
        if col >= 0 and line >= 0:
            self.api.speak(f"Line {line + 1}, Column {col + 1}", interrupt=False)
            self.status_bar.SetLabel(f"Line {line + 1}, Col {col + 1}")

    def on_find(self):
        dialog = wx.TextEntryDialog(self.frame, "Enter search text:", "Find")
        if dialog.ShowModal() == wx.ID_OK:
            search_text = dialog.GetValue()
            if search_text:
                content = self.text_ctrl.GetValue()
                count = content.count(search_text)
                idx = content.find(search_text)
                if idx >= 0:
                    self.text_ctrl.SetSelection(idx, idx + len(search_text))
                    self.text_ctrl.SetFocus()
                self.api.speak(f"Found {count} matches")
        dialog.Destroy()

    def on_close(self, event=None):
        if self.frame: self.frame.Destroy()
        self.api.sounds.play("close")
        self.api.desktop.on_app_closed(self)
