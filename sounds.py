import subprocess
import threading
import json
import os
import time
import platform
import shutil
import io
import wave
import hashlib
import numpy as np
import audio_devices

try:
    import soundfile as sf
    HAS_SOUNDFILE = True
except ImportError:
    sf = None
    HAS_SOUNDFILE = False

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False

try:
    import winsound
except ImportError:
    winsound = None

# Characters that are invalid in Windows file/folder names. Theme names become
# folder names under the themes directory, so they must be valid on every OS.
INVALID_THEME_NAME_CHARS = set('\\/:*?"<>|')


def is_valid_theme_name(name):
    """Return (ok, error) for a proposed theme name."""
    if not isinstance(name, str):
        return False, "Theme name is required."
    name = name.strip()
    if not name:
        return False, "Theme name is required."
    if name in (".", ".."):
        return False, "That theme name is not allowed."
    bad = sorted({char for char in name if char in INVALID_THEME_NAME_CHARS})
    if bad:
        return False, "Theme name cannot contain " + ", ".join(bad) + "."
    if any(ord(char) < 32 for char in name):
        return False, "Theme name cannot contain control characters."
    return True, ""


def parse_tone_string(frequencies_text, durations_text):
    """Parse tone entry text into a notes list.

    frequencies_text: comma/space separated frequencies in Hz (0 = silence).
    durations_text: comma/space separated durations in milliseconds.

    Returns (notes, None) on success, where notes is [(freq, dur), ...],
    or (None, error_message) when the input is invalid.
    """
    def split_values(text):
        return [part.strip() for part in str(text).replace(",", " ").split() if part.strip()]

    freq_parts = split_values(frequencies_text)
    dur_parts = split_values(durations_text)
    if not freq_parts and not dur_parts:
        return None, "Enter at least one frequency and duration."
    if len(freq_parts) != len(dur_parts):
        return None, (
            f"You entered {len(freq_parts)} frequencies and {len(dur_parts)} durations. "
            "The counts must match."
        )
    notes = []
    for index, (freq_text, dur_text) in enumerate(zip(freq_parts, dur_parts), start=1):
        try:
            freq = float(freq_text)
        except ValueError:
            return None, f"Frequency {index} is not a number: {freq_text}"
        try:
            dur = int(round(float(dur_text)))
        except ValueError:
            return None, f"Duration {index} is not a number: {dur_text}"
        if freq < 0 or freq > 20000:
            return None, f"Frequency {index} must be between 0 and 20000 hertz."
        if dur <= 0 or dur > 10000:
            return None, f"Duration {index} must be between 1 and 10000 milliseconds."
        notes.append((freq, dur))
    return notes, None


def notes_to_tone_string(notes):
    """Render a notes list as the two tone-entry strings (frequencies, durations)."""
    if not notes:
        return "", ""
    frequencies = ", ".join(str(int(freq)) if float(freq).is_integer() else str(freq) for freq, _dur in notes)
    durations = ", ".join(str(int(dur)) for _freq, dur in notes)
    return frequencies, durations


class SoundManager:
    def __init__(self, data_dir):
        # Normalize the path initially
        self.data_dir = os.path.normpath(data_dir)
        self.platform_name = platform.system()
        self.ffplay_path = shutil.which("ffplay")
        self.afplay_path = shutil.which("afplay") if self.platform_name == "Darwin" else None
        self.aplay_path = shutil.which("aplay") if self.platform_name == "Linux" else None
        self.paplay_path = shutil.which("paplay") if self.platform_name == "Linux" else None
        self.generated_tones_dir = os.path.join(self.data_dir, "generated_tones")
        self.repo_root = os.path.dirname(os.path.abspath(__file__))
        self.repo_music_dir = os.path.join(self.repo_root, "music")
        print(f"Normalized data_dir: {repr(self.data_dir)}")

        # Paths for configuration and custom themes
        self.config_path = os.path.join(self.data_dir, "sound_theme.json")
        self.music_config_path = os.path.join(self.data_dir, "music_config.json")
        self.custom_themes_dir = os.path.join(self.data_dir, "themes")
        self.repo_themes_dir = os.path.join(self.repo_root, "themes")
        print(f"Custom themes directory: {repr(self.custom_themes_dir)}")
        print(f"Repo themes directory: {repr(self.repo_themes_dir)}")

        # Default themes are hardcoded
        self.default_themes = {
            "Modern": {
                "startup": [(349, 150), (440, 150), (523, 150), (698, 300)],
                "nav": [(600, 50)],
                "launch": [(440, 100), (880, 100)],
                "close": [(880, 100), (440, 100)],
                "shutdown": [(698, 300), (523, 200), (349, 400)],
                "alert": [(1000, 200), (800, 200)],
                "alarm": [(1000, 200), (800, 200)],
                "timer": [(1000, 200), (800, 200)],
            },
            "Retro": {
                "startup": [(100, 100), (200, 100), (300, 100)],
                "nav": [(150, 30)],
                "launch": [(200, 50), (400, 50), (600, 50)],
                "close": [(600, 50), (400, 50), (200, 50)],
                "shutdown": [(500, 150), (300, 200), (100, 400)],
                "alert": [(400, 100), (400, 100), (400, 100)],
                "alarm": [(400, 100), (400, 100), (400, 100)],
                "timer": [(400, 100), (400, 100), (400, 100)],
            },
            "Classic": {
                "startup": [(523, 400)],
                "nav": [(400, 20)],
                "launch": [(523, 100)],
                "close": [(261, 100)],
                "shutdown": [(261, 400), (196, 600)],
                "alert": [(1000, 500)],
                "alarm": [(1000, 500)],
                "timer": [(1000, 500)],
            }
        }
        self.themes = self.default_themes.copy()

        # Load custom themes and merge them
        custom_themes_data = self._load_all_custom_themes()
        self.themes.update(custom_themes_data)

        self.current_theme = self.load_theme_name()
        self._audio_cache = {}

    def _normalize_theme_asset(self, value):
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            return ""
        if normalized.lower() == "none":
            return "None"
        if os.path.isabs(normalized):
            return normalized

        repo_candidate = os.path.abspath(os.path.join(self.repo_root, normalized))
        if os.path.exists(repo_candidate):
            return repo_candidate

        music_candidate = os.path.abspath(os.path.join(self.repo_music_dir, normalized))
        if os.path.exists(music_candidate):
            return music_candidate

        return repo_candidate

    def _load_all_custom_themes(self):
        """Loads all custom themes from the theme directory."""
        custom_themes_data = {}
        
        # Helper to load from a directory
        def load_from_dir(directory):
            if not os.path.exists(directory):
                return
            for item in os.listdir(directory):
                theme_dir = os.path.join(directory, item)
                if os.path.isdir(theme_dir):
                    theme_name = item
                    theme_config_path = os.path.join(theme_dir, 'theme.json')
                    if os.path.exists(theme_config_path):
                        try:
                            with open(theme_config_path, "r", encoding='utf-8') as f:
                                theme_config = json.load(f)
                                for key, value in theme_config.items():
                                    theme_config[key] = self._normalize_theme_asset(value)
                                merged_theme = dict(custom_themes_data.get(theme_name, {}))
                                merged_theme.update(theme_config)
                                custom_themes_data[theme_name] = merged_theme
                        except Exception as e:
                            print(f"Warning: Error loading theme config from {theme_config_path}: {e}")

        load_from_dir(self.repo_themes_dir)
        load_from_dir(self.custom_themes_dir)
        
        return custom_themes_data

    def save_custom_themes(self):
        """Saves all custom themes to their respective directories."""
        if not os.path.exists(self.custom_themes_dir):
            os.makedirs(self.custom_themes_dir)

        for theme_name, theme_data in self.themes.items():
            default_theme = self.default_themes.get(theme_name)
            should_persist = theme_name not in self.default_themes or theme_data != default_theme
            if not should_persist:
                continue

            theme_dir = os.path.join(self.custom_themes_dir, theme_name)
            os.makedirs(theme_dir, exist_ok=True)
            theme_config_path = os.path.join(theme_dir, 'theme.json')
            try:
                with open(theme_config_path, "w", encoding='utf-8') as f:
                    json.dump(theme_data, f, indent=4)
            except Exception as e:
                print(f"Error saving custom theme '{theme_name}': {e}")

    def load_theme_name(self):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
            
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding='utf-8') as f:
                    config_data = json.load(f)
                    theme_name = config_data.get("theme", "Modern")
                    if theme_name not in self.themes:
                        return "Modern"
                    return theme_name
            except Exception:
                return "Modern"
        return "Modern"

    def save_theme_name(self, name):
        if name in self.themes:
            self.current_theme = name
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump({"theme": name}, f)
            except Exception as e:
                print(f"Error saving theme name: {e}")
            # Only themes that actually define background music change the user's
            # music selection; music-less themes leave it untouched.
            theme_music = self.get_theme_background_music(name)
            if theme_music is not None:
                self.save_background_music(theme_music)

    def load_background_music(self):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        if os.path.exists(self.music_config_path):
            try:
                with open(self.music_config_path, "r", encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("music", "None")
            except Exception:
                return "None"
        return "None"

    def save_background_music(self, music_value):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        stored_value = music_value if music_value else "None"
        # Read-modify-write so the saved volume survives music changes.
        config = {}
        if os.path.exists(self.music_config_path):
            try:
                with open(self.music_config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                config = {}
        if isinstance(config, dict):
            config["music"] = stored_value
        else:
            config = {"music": stored_value}
        try:
            with open(self.music_config_path, "w", encoding="utf-8") as f:
                json.dump(config, f)
        except Exception as e:
            print(f"Error saving background music: {e}")

    def resolve_music_path(self, music_value):
        if not music_value or music_value == "None":
            return None
        normalized = self._normalize_theme_asset(music_value)
        if isinstance(normalized, str) and os.path.isabs(normalized):
            return normalized
        return None

    def get_theme_background_music(self, theme_name=None):
        """Return the theme's background music setting.

        Returns None when the theme does not define background music, so callers
        can distinguish "theme has no music" from a literal "None" selection.
        """
        selected_theme = theme_name or self.current_theme
        theme_data = self.themes.get(selected_theme)
        if theme_data is None or "background_music" not in theme_data:
            return None
        music_value = self._normalize_theme_asset(theme_data.get("background_music"))
        if not music_value:
            return None
        return music_value

    def _resolve_sound_data(self, sound_type):
        if self.current_theme not in self.themes:
            self.current_theme = "Modern"
        theme_data = self.themes.get(self.current_theme, self.themes["Modern"])
        fallback_map = {
            "timer": "alert",
            "alarm": "alert",
        }
        data = theme_data.get(sound_type)
        if not data and sound_type in fallback_map:
            data = theme_data.get(fallback_map[sound_type])
        # Unassigned slots fall back to the Modern theme so partial themes work.
        if not data and sound_type != "background_music":
            fallback = self.themes["Modern"].get(sound_type)
            if not fallback and sound_type in fallback_map:
                fallback = self.themes["Modern"].get(fallback_map[sound_type])
            data = fallback
        return data

    def play(self, sound_type):
        data = self._resolve_sound_data(sound_type)
        if not data: return

        if self.platform_name == "Windows" and winsound is not None and not isinstance(data, str):
            if self._play_notes_with_winsound_file(data, async_play=sound_type != "startup"):
                return

        if sound_type == "startup":
            if isinstance(data, str):
                self._play_file_sync(data)
            else:
                self._play_notes_sync(data)
        else:
            if isinstance(data, str):
                threading.Thread(target=self._play_file, args=(data,), daemon=True).start()
            else:
                threading.Thread(target=self._play_notes, args=(data,), daemon=True).start()

    def play_sync(self, sound_type):
        data = self._resolve_sound_data(sound_type)
        if not data: return
        if isinstance(data, str):
            self._play_file_sync(data)
        else:
            self._play_notes_sync(data)

    def _play_notes(self, notes):
        """Play a sequence of notes using the best available backend."""
        self._run_notes_chain(notes)

    def _play_notes_sync(self, notes):
        """Play notes synchronously."""
        self._run_notes_chain(notes)

    def _run_notes_chain(self, notes):
        if self._play_notes_with_winsound_audio(notes):
            return
        if self._play_notes_with_sounddevice(notes):
            return
        if self._play_notes_with_winsound(notes):
            return
        if self._play_notes_with_system_player(notes):
            return
        self._play_notes_ffplay(notes)

    def _play_notes_with_system_player(self, notes):
        """Render tones to a WAV file and play it with a system player.

        This is the macOS/Linux fallback for tone sequences when sounddevice is
        unavailable: afplay on macOS, aplay/paplay on Linux. Returns False when
        no system player can handle the rendered file so the caller can fall
        back to ffplay.
        """
        if not notes:
            return False
        try:
            wav_path = self._get_or_create_tone_file(notes)
        except Exception as e:
            print(f"Failed to render tones to file: {e}")
            return False
        if not wav_path:
            return False
        for player in (self.afplay_path, self.paplay_path, self.aplay_path):
            if not player:
                continue
            try:
                subprocess.run(
                    [player, wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                continue
        return False

    def play_preview(self, value):
        """Play a single theme slot assignment without changing the current theme.

        `value` is either a notes list (tone sequence) or an audio file path.
        Returns True when playback was started, False when the value was empty
        or the file could not be found.
        """
        if not value:
            return False
        if isinstance(value, str):
            path = self._normalize_theme_asset(value)
            if not os.path.exists(path):
                print(f"Preview file not found: {path}")
                return False
            threading.Thread(target=self._play_file, args=(path,), daemon=True).start()
        else:
            threading.Thread(target=self._play_notes, args=(value,), daemon=True).start()
        return True

    def _build_notes_filter(self, notes):
        """Builds a lavfi filter string for a sequence of sine waves."""
        if not notes: return None
        
        parts = []
        for i, (freq, dur) in enumerate(notes):
            dur_sec = dur / 1000.0
            parts.append(f"sine=f={freq}:d={dur_sec}[v{i}]")
        
        # Concat all sine waves and add a small pad
        inputs = "".join([f"[v{i}]" for i in range(len(notes))])
        concat = f"{inputs}concat=n={len(notes)}:v=0:a=1,apad=pad_dur=0.3[out]"
        
        return ";".join(parts) + ";" + concat

    def _play_file(self, path):
        if os.path.exists(path):
            if self._play_file_with_sounddevice(path):
                return
            try:
                self._play_file_with_system_player(path)
            except Exception as e:
                print(f"Error playing file: {e}")

    def _play_file_sync(self, path):
        if os.path.exists(path):
            if self._play_file_with_sounddevice(path):
                return
            try:
                self._play_file_with_system_player(path)
            except Exception as e:
                print(f"Error playing file sync: {e}")

    def _play_file_with_system_player(self, path):
        if self._play_file_with_winsound(path):
            return
        if self.afplay_path:
            subprocess.run(
                [self.afplay_path, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        if self.paplay_path:
            subprocess.run(
                [self.paplay_path, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        if self.aplay_path and path.lower().endswith(".wav"):
            subprocess.run(
                [self.aplay_path, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        if self.ffplay_path:
            clean_path = path.replace(os.sep, "/")
            subprocess.run(
                [self.ffplay_path, "-nodisp", "-autoexit", "-af", "apad=pad_dur=0.3", clean_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        raise RuntimeError("No system audio player is available.")

    def _play_file_with_winsound(self, path):
        if winsound is None:
            return False
        if not path.lower().endswith(".wav"):
            return False
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME)
            return True
        except Exception:
            return False

    def _selected_output_device_index(self):
        config = audio_devices.load_device_config(self.data_dir)
        outputs = audio_devices.list_output_devices()
        return audio_devices.resolve_selected_index(
            outputs, config, "output_device_index", "output_device"
        )

    def _play_notes_with_sounddevice(self, notes):
        if not HAS_SOUNDDEVICE or not notes:
            return False
        try:
            sample_rate = 44100
            parts = []
            for freq, dur in notes:
                duration_seconds = max(dur, 1) / 1000.0
                t = np.linspace(0, duration_seconds, int(sample_rate * duration_seconds), endpoint=False, dtype=np.float32)
                wave = 0.20 * np.sin(2 * np.pi * float(freq) * t)
                parts.append(wave)
            audio = np.concatenate(parts) if parts else np.array([], dtype=np.float32)
            if audio.size == 0:
                return False
            device_index = self._selected_output_device_index()
            sd.play(audio, samplerate=sample_rate, device=device_index, blocking=True)
            return True
        except Exception as e:
            print(f"Sounddevice notes playback failed, falling back to ffplay: {e}")
            return False

    def _play_notes_with_winsound(self, notes):
        if winsound is None or not notes:
            return False
        try:
            for freq, dur in notes:
                winsound.Beep(int(freq), max(int(dur), 1))
            return True
        except Exception:
            return False

    def _play_notes_with_winsound_audio(self, notes):
        if winsound is None or not notes:
            return False
        try:
            sample_rate = 44100
            parts = []
            for freq, dur in notes:
                duration_seconds = max(int(dur), 1) / 1000.0
                frame_count = max(int(sample_rate * duration_seconds), 1)
                if int(freq) <= 0:
                    wave_data = np.zeros(frame_count, dtype=np.float32)
                else:
                    t = np.linspace(0, duration_seconds, frame_count, endpoint=False, dtype=np.float32)
                    wave_data = 0.20 * np.sin(2 * np.pi * float(freq) * t)
                parts.append(wave_data)

            audio = np.concatenate(parts) if parts else np.array([], dtype=np.float32)
            if audio.size == 0:
                return False

            pcm = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm.tobytes())

            winsound.PlaySound(
                buffer.getvalue(),
                winsound.SND_MEMORY | winsound.SND_SYNC,
            )
            return True
        except Exception:
            return False

    def _render_notes_to_pcm(self, notes, sample_rate=44100):
        parts = []
        for freq, dur in notes:
            duration_seconds = max(int(dur), 1) / 1000.0
            frame_count = max(int(sample_rate * duration_seconds), 1)
            if int(freq) <= 0:
                wave_data = np.zeros(frame_count, dtype=np.float32)
            else:
                t = np.linspace(0, duration_seconds, frame_count, endpoint=False, dtype=np.float32)
                wave_data = 0.20 * np.sin(2 * np.pi * float(freq) * t)
            parts.append(wave_data)
        audio = np.concatenate(parts) if parts else np.array([], dtype=np.float32)
        return np.clip(audio * 32767, -32768, 32767).astype(np.int16), sample_rate

    def _get_or_create_tone_file(self, notes):
        if not notes:
            return None
        os.makedirs(self.generated_tones_dir, exist_ok=True)
        notes_key = json.dumps(notes, separators=(",", ":"), ensure_ascii=True)
        file_hash = hashlib.sha1(notes_key.encode("utf-8")).hexdigest()
        wav_path = os.path.join(self.generated_tones_dir, f"{file_hash}.wav")
        if os.path.exists(wav_path):
            return wav_path

        pcm, sample_rate = self._render_notes_to_pcm(notes)
        with wave.open(wav_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm.tobytes())
        return wav_path

    def _play_notes_with_winsound_file(self, notes, async_play):
        if winsound is None or not notes:
            return False
        try:
            wav_path = self._get_or_create_tone_file(notes)
            if not wav_path:
                return False
            flags = winsound.SND_FILENAME | winsound.SND_NODEFAULT
            flags |= winsound.SND_ASYNC if async_play else winsound.SND_SYNC
            winsound.PlaySound(wav_path, flags)
            return True
        except Exception:
            return False

    def _play_file_with_sounddevice(self, path):
        if not HAS_SOUNDDEVICE or not HAS_SOUNDFILE:
            return False
        try:
            audio, sample_rate = self._get_cached_audio(path)
            if isinstance(audio, np.ndarray) and audio.size == 0:
                return False
            device_index = self._selected_output_device_index()
            sd.play(audio, samplerate=sample_rate, device=device_index, blocking=True)
            return True
        except Exception as e:
            print(f"Sounddevice file playback failed, falling back to ffplay: {e}")
            return False

    def _get_cached_audio(self, path):
        if not HAS_SOUNDFILE:
            raise RuntimeError("soundfile is not installed")
        abs_path = os.path.abspath(path)
        mtime = os.path.getmtime(abs_path)
        cached = self._audio_cache.get(abs_path)
        if cached and cached.get("mtime") == mtime:
            return cached["audio"], cached["rate"]
        audio, sample_rate = sf.read(abs_path, dtype="float32", always_2d=False)
        self._audio_cache[abs_path] = {
            "mtime": mtime,
            "audio": audio,
            "rate": sample_rate,
        }
        return audio, sample_rate

    def _play_notes_ffplay(self, notes):
        filter_str = self._build_notes_filter(notes)
        if filter_str and self.ffplay_path:
            try:
                subprocess.run([self.ffplay_path, "-nodisp", "-autoexit", "-f", "lavfi", filter_str],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"Error playing notes: {e}")

    def get_available_themes(self):
        return list(self.themes.keys())
