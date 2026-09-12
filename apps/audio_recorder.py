import os
import subprocess
import threading
import shutil
from datetime import datetime

import numpy as np
import wx

import audio_devices
from api import BlindApp
from platform_support import open_external_file

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    sd = None
    HAS_SOUNDDEVICE = False

try:
    import soundfile as sf
    HAS_SOUNDFILE = True
except ImportError:
    sf = None
    HAS_SOUNDFILE = False


class AudioRecorderApp(BlindApp):
    """Combined recorder and audio player app."""

    def __init__(self, api):
        super().__init__(api)
        self.name = "Media Studio"
        self.description = "Record and play audio files."
        self.category = "Media"
        self.help_text = "Use Start/Stop to record, Save to store clips, and Play/Pause/Stop to listen."
        self.docs = "Media Studio records from your selected microphone and plays recordings or chosen audio files."
        self.sample_rate = 44100
        self.recordings_dir = os.path.join(self.api.get_data_path(""), "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)

        self.frame = None
        self.filename_input = None
        self.duration_label = None
        self.status_label = None
        self.recordings_list = None
        self.path_input = None

        self.is_recording = False
        self.recording_data = []
        self.recording_time = 0
        self.recording_thread = None

        self.current_playback_path = None
        self.is_playing = False
        self.is_paused = False
        self.playback_position = 0
        self.playback_data = None
        self.playback_rate = None
        self.playback_channels = None
        self.playback_lock = threading.Lock()
        self.external_playback_process = None
        self.afplay_path = shutil.which("afplay")
        self.ffplay_path = shutil.which("ffplay")

    def run(self, file_path=None):
        self.frame = wx.Frame(None, title="Media Studio", size=(700, 560))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        sizer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(panel, label="Media Studio")
        title.SetForegroundColour(wx.Colour(255, 255, 255))
        title.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(title, 0, wx.ALL | wx.CENTER, 12)

        filename_label = wx.StaticText(panel, label="Recording filename (optional):")
        filename_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(filename_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.filename_input = wx.TextCtrl(panel)
        self.filename_input.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak("Recording filename"), e.Skip()))
        sizer.Add(self.filename_input, 0, wx.EXPAND | wx.ALL, 10)

        self.duration_label = wx.StaticText(panel, label="Duration: 0 seconds")
        self.duration_label.SetForegroundColour(wx.Colour(150, 255, 150))
        sizer.Add(self.duration_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.status_label = wx.StaticText(panel, label="Status: Ready")
        self.status_label.SetForegroundColour(wx.Colour(255, 255, 100))
        sizer.Add(self.status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        rec_btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.start_btn = wx.Button(panel, label="&Start Recording")
        self.stop_btn = wx.Button(panel, label="S&top Recording")
        self.save_btn = wx.Button(panel, label="&Save Recording")
        self.stop_btn.Enable(False)
        self.save_btn.Enable(False)
        self.start_btn.Bind(wx.EVT_BUTTON, self.on_start_recording)
        self.start_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Start Recording"))
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop_recording)
        self.stop_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Stop Recording"))
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save_recording)
        self.save_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Save Recording"))
        rec_btn_row.Add(self.start_btn, 1, wx.EXPAND | wx.ALL, 5)
        rec_btn_row.Add(self.stop_btn, 1, wx.EXPAND | wx.ALL, 5)
        rec_btn_row.Add(self.save_btn, 1, wx.EXPAND | wx.ALL, 5)
        sizer.Add(rec_btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        files_label = wx.StaticText(panel, label="Audio file path:")
        files_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(files_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        file_row = wx.BoxSizer(wx.HORIZONTAL)
        self.path_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.path_input.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak("Audio file path"), e.Skip()))
        self.path_input.Bind(wx.EVT_TEXT_ENTER, self.on_load_path)
        browse_btn = wx.Button(panel, label="&Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self.on_browse_file)
        browse_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Browse"))
        load_btn = wx.Button(panel, label="&Load Path")
        load_btn.Bind(wx.EVT_BUTTON, self.on_load_path)
        load_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Load Path"))
        file_row.Add(self.path_input, 1, wx.EXPAND | wx.RIGHT, 8)
        file_row.Add(browse_btn, 0, wx.RIGHT, 6)
        file_row.Add(load_btn, 0)
        sizer.Add(file_row, 0, wx.EXPAND | wx.ALL, 10)

        playback_row = wx.BoxSizer(wx.HORIZONTAL)
        self.play_btn = wx.Button(panel, label="&Play")
        self.pause_btn = wx.Button(panel, label="P&ause/Resume")
        self.stop_playback_btn = wx.Button(panel, label="Stop &Playback")
        self.play_btn.Bind(wx.EVT_BUTTON, self.on_play)
        self.play_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Play"))
        self.pause_btn.Bind(wx.EVT_BUTTON, self.on_pause_resume)
        self.pause_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Pause/Resume"))
        self.stop_playback_btn.Bind(wx.EVT_BUTTON, self.on_stop_playback)
        self.stop_playback_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Stop Playback"))
        playback_row.Add(self.play_btn, 1, wx.EXPAND | wx.ALL, 5)
        playback_row.Add(self.pause_btn, 1, wx.EXPAND | wx.ALL, 5)
        playback_row.Add(self.stop_playback_btn, 1, wx.EXPAND | wx.ALL, 5)
        sizer.Add(playback_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        list_label = wx.StaticText(panel, label="Saved recordings:")
        list_label.SetForegroundColour(wx.Colour(255, 255, 255))
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.recordings_list = wx.ListBox(panel)
        self.recordings_list.Bind(wx.EVT_LISTBOX_DCLICK, self.on_open_selected_recording)
        self.recordings_list.Bind(wx.EVT_LISTBOX, self.on_recording_selected)
        self.recordings_list.Bind(wx.EVT_KEY_DOWN, self.on_recordings_key)
        self.recordings_list.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Saved recordings"))
        sizer.Add(self.recordings_list, 1, wx.EXPAND | wx.ALL, 10)
        self.refresh_recordings_list()

        panel.SetSizer(sizer)
        self.frame.Bind(wx.EVT_CHAR_HOOK, self.on_key_press)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        self.frame.Show()
        self.api.play_sound("launch")
        self.api.speak("Media Studio opened.")

        if file_path:
            self.path_input.SetValue(file_path)
            self.load_playback_file(file_path)

    def on_key_press(self, event):
        if event.GetKeyCode() == wx.WXK_SPACE:
            if self.is_recording:
                self.on_stop_recording()
            else:
                self.on_start_recording()
        else:
            event.Skip()

    def on_start_recording(self, event=None):
        if not HAS_SOUNDDEVICE or not HAS_SOUNDFILE:
            self.api.speak("Recording needs the sounddevice and soundfile Python packages on this system.")
            return
        if self.is_recording:
            self.api.speak("Already recording.")
            return
        self.is_recording = True
        self.recording_data = []
        self.recording_time = 0
        self.start_btn.Enable(False)
        self.stop_btn.Enable(True)
        self.filename_input.Enable(False)
        self.status_label.SetLabel("Status: Recording...")
        self.status_label.SetForegroundColour(wx.Colour(255, 0, 0))
        self.recording_thread = threading.Thread(target=self._record_audio, daemon=True)
        self.recording_thread.start()
        self._update_duration()
        self.api.speak("Recording started.")

    def _record_audio(self):
        try:
            config = audio_devices.load_device_config(self.api.data_dir)
            input_devices = audio_devices.list_input_devices()
            selected_input_index = audio_devices.resolve_selected_index(
                input_devices, config, "input_device_index", "input_device"
            )
            frames = []
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                device=selected_input_index,
                callback=lambda indata, frames_count, time_info, status: frames.append(indata.copy())
            ):
                while self.is_recording:
                    pass
            if frames:
                self.recording_data = np.concatenate(frames, axis=0)
        except Exception:
            self.api.speak("Recording failed on selected mic. Trying default input.")
            try:
                frames = []
                with sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    callback=lambda indata, frames_count, time_info, status: frames.append(indata.copy())
                ):
                    while self.is_recording:
                        pass
                if frames:
                    self.recording_data = np.concatenate(frames, axis=0)
            except Exception as fallback_error:
                self.api.speak(f"Recording error: {fallback_error}")

    def _update_duration(self):
        if self.is_recording:
            self.recording_time += 1
            wx.CallAfter(self.duration_label.SetLabel, f"Duration: {self.recording_time} seconds")
            if self.recording_time % 10 == 0:
                wx.CallAfter(self.api.speak, f"Recording, {self.recording_time} seconds")
            threading.Timer(1.0, self._update_duration).start()

    def on_stop_recording(self, event=None):
        if not self.is_recording:
            self.api.speak("Not recording.")
            return
        self.is_recording = False
        self.start_btn.Enable(True)
        self.stop_btn.Enable(False)
        self.save_btn.Enable(True)
        self.status_label.SetLabel("Status: Ready to save recording")
        self.status_label.SetForegroundColour(wx.Colour(0, 255, 0))
        self.api.speak(f"Recording stopped. {self.recording_time} seconds.")

    def on_save_recording(self, event=None):
        if not HAS_SOUNDFILE:
            self.api.speak("Saving recordings needs the soundfile Python package.")
            return
        if len(self.recording_data) == 0:
            self.api.speak("No recording to save.")
            return
        filename = self.filename_input.GetValue().strip()
        if not filename:
            filename = f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if not filename.endswith(".wav"):
            filename += ".wav"
        filepath = os.path.join(self.recordings_dir, filename)
        counter = 1
        base = filepath[:-4]
        while os.path.exists(filepath):
            filepath = f"{base}_{counter}.wav"
            counter += 1
        try:
            sf.write(filepath, self.recording_data, self.sample_rate)
            self.api.speak(f"Saved {os.path.basename(filepath)}.")
            self.recording_data = []
            self.recording_time = 0
            self.filename_input.Clear()
            self.filename_input.Enable(True)
            self.save_btn.Enable(False)
            self.duration_label.SetLabel("Duration: 0 seconds")
            self.status_label.SetLabel("Status: Ready")
            self.status_label.SetForegroundColour(wx.Colour(255, 255, 100))
            self.refresh_recordings_list()
        except Exception as e:
            self.api.speak(f"Save error: {e}")

    def refresh_recordings_list(self):
        self.recordings_list.Clear()
        try:
            files = [f for f in os.listdir(self.recordings_dir) if f.lower().endswith((".wav", ".mp3", ".ogg", ".flac"))]
            files.sort(reverse=True)
            for name in files:
                self.recordings_list.Append(name)
        except Exception:
            pass

    def on_recording_selected(self, event):
        selected = self.recordings_list.GetStringSelection()
        if selected and not self.api.is_enhanced_mode():
            self.api.speak(selected)

    def on_recordings_key(self, event):
        if event.GetKeyCode() == wx.WXK_DELETE:
            selected = self.recordings_list.GetStringSelection()
            if selected:
                full = os.path.join(self.recordings_dir, selected)
                try:
                    os.remove(full)
                    self.refresh_recordings_list()
                    self.api.speak(f"Deleted {selected}")
                except Exception as e:
                    self.api.speak(f"Could not delete: {e}")
        else:
            event.Skip()

    def on_open_selected_recording(self, event=None):
        selected = self.recordings_list.GetStringSelection()
        if not selected:
            self.api.speak("No recording selected.")
            return
        full = os.path.join(self.recordings_dir, selected)
        self.path_input.SetValue(full)
        self.load_playback_file(full)

    def on_browse_file(self, event=None):
        wildcard = "Audio files (*.wav;*.mp3;*.ogg;*.flac)|*.wav;*.mp3;*.ogg;*.flac"
        path = self.api.choose_file(self.frame, "open", "Choose audio file", wildcard)
        if path:
            self.path_input.SetValue(path)
            self.load_playback_file(path)

    def on_load_path(self, event=None):
        self.load_playback_file(self.path_input.GetValue().strip())

    def load_playback_file(self, path):
        if not path:
            self.api.speak("Provide a file path first.")
            return
        if not os.path.exists(path):
            self.api.speak("File does not exist.")
            return
        self.on_stop_playback()
        try:
            if HAS_SOUNDFILE:
                data, rate = sf.read(path, dtype="float32")
                if data.ndim == 1:
                    data = data[:, np.newaxis]
                with self.playback_lock:
                    self.current_playback_path = path
                    self.playback_data = data
                    self.playback_rate = rate
                    self.playback_channels = data.shape[1]
                    self.playback_position = 0
                    self.is_paused = False
            else:
                with self.playback_lock:
                    self.current_playback_path = path
                    self.playback_data = None
                    self.playback_rate = None
                    self.playback_channels = None
                    self.playback_position = 0
                    self.is_paused = False
            self.api.speak(f"Loaded {os.path.basename(path)}.")
        except Exception as e:
            self.api.speak(f"Could not load audio: {e}")

    def _selected_output(self):
        config = audio_devices.load_device_config(self.api.data_dir)
        outputs = audio_devices.list_output_devices()
        return audio_devices.resolve_selected_index(outputs, config, "output_device_index", "output_device")

    def on_play(self, event=None):
        if not HAS_SOUNDDEVICE or not HAS_SOUNDFILE:
            self._play_with_system_player()
            return
        with self.playback_lock:
            if self.playback_data is None:
                self.api.speak("Load a file first.")
                return
            if self.is_playing and not self.is_paused:
                self.api.speak("Already playing.")
                return
            segment = self.playback_data[self.playback_position :]
            if segment.size == 0:
                self.playback_position = 0
                segment = self.playback_data
            self.is_playing = True
            self.is_paused = False
        try:
            sd.play(segment, samplerate=self.playback_rate, device=self._selected_output())
            self.api.speak("Playing audio.")
        except Exception as e:
            self.api.speak(f"Playback error: {e}")

    def _play_with_system_player(self):
        with self.playback_lock:
            path = self.current_playback_path
        if not path:
            self.api.speak("Load a file first.")
            return
        if self.external_playback_process and self.external_playback_process.poll() is None:
            self.api.speak("Already playing.")
            return
        try:
            if self.afplay_path:
                self.external_playback_process = subprocess.Popen(
                    [self.afplay_path, path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif self.ffplay_path:
                self.external_playback_process = subprocess.Popen(
                    [self.ffplay_path, "-nodisp", "-autoexit", path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                open_external_file(path)
                self.external_playback_process = None
            self.is_playing = True
            self.is_paused = False
            self.api.speak("Playing audio.")
        except Exception as error:
            self.api.speak(f"Playback error: {error}")

    def on_pause_resume(self, event=None):
        if not HAS_SOUNDDEVICE or not HAS_SOUNDFILE:
            self.api.speak("Pause and resume are unavailable in basic playback mode on this system.")
            return
        with self.playback_lock:
            if self.playback_data is None:
                self.api.speak("Load a file first.")
                return
            if not self.is_playing:
                self.api.speak("Nothing is playing.")
                return
            if not self.is_paused:
                sd.stop()
                self.is_paused = True
                self.api.speak("Paused.")
            else:
                self.is_paused = False
                self.playback_position = 0
        if not self.is_paused:
            self.api.speak("Resuming from start.")
            self.on_play()

    def on_stop_playback(self, event=None):
        if not HAS_SOUNDDEVICE or not HAS_SOUNDFILE:
            if self.external_playback_process and self.external_playback_process.poll() is None:
                try:
                    self.external_playback_process.terminate()
                except Exception:
                    pass
            self.external_playback_process = None
            self.is_playing = False
            self.is_paused = False
            return
        with self.playback_lock:
            if self.playback_data is None:
                return
            sd.stop()
            self.playback_position = 0
            self.is_playing = False
            self.is_paused = False
        self.api.speak("Playback stopped.")

    def on_close(self, event=None):
        self.is_recording = False
        try:
            if HAS_SOUNDDEVICE:
                sd.stop()
        except Exception:
            pass
        if self.external_playback_process and self.external_playback_process.poll() is None:
            try:
                self.external_playback_process.terminate()
            except Exception:
                pass
        if self.frame:
            self.frame.Destroy()
        self.api.play_sound("close")
        self.api.desktop.on_app_closed(self)
