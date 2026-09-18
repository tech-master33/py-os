import ctypes
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
import time

from app_paths import get_data_dir
from platform_support import command_path, get_speech_backends, get_platform_name
from text_integrity import for_speech

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None


class SpeechEngine:
    def __init__(self):
        self.platform_name = get_platform_name()
        self.prefers_native_screen_reader = self.platform_name == "Darwin"
        self.nvda_dll = None
        self.use_nvda = False
        self.speech_queue = queue.Queue()
        self.tts_thread = None
        self.backend = "unknown"
        self.current_process = None
        self.process_lock = threading.Lock()
        self.config_dir = get_data_dir()
        self.config_path = os.path.join(self.config_dir, "speech_config.json")
        self.default_rate = 180 if self.platform_name == "Darwin" else 200
        self.rate = self.default_rate

        self.mode = "auto"
        self._load_config()
        self._load_nvda_if_available()
        self._apply_mode()

    def _available_modes(self):
        if self.platform_name == "Windows":
            return {"auto", "nvda", "system"}
        return {"auto", "system", "pyttsx3"}

    def get_available_modes(self):
        if self.platform_name == "Windows":
            return [
                ("Auto (NVDA when available)", "auto"),
                ("NVDA", "nvda"),
                ("System voice", "system"),
            ]
        if self.platform_name == "Darwin":
            return [
                ("Auto", "auto"),
                ("System voice (works with VoiceOver)", "system"),
                ("pyttsx3 fallback", "pyttsx3"),
            ]
        return [
            ("Auto", "auto"),
            ("System voice", "system"),
            ("pyttsx3 fallback", "pyttsx3"),
        ]

    def _load_nvda_if_available(self):
        if self.platform_name != "Windows":
            return

        dll_name = "nvdaControllerClient64.dll" if ctypes.sizeof(ctypes.c_void_p) == 8 else "nvdaControllerClient32.dll"
        dll_path = os.path.join(os.getcwd(), dll_name)
        if not os.path.exists(dll_path):
            return

        try:
            self.nvda_dll = ctypes.windll.LoadLibrary(dll_path)
            self.nvda_dll.nvdaController_testIfRunning.restype = ctypes.c_int
            self.nvda_dll.nvdaController_speakText.argtypes = [ctypes.c_wchar_p]
            self.nvda_dll.nvdaController_speakText.restype = ctypes.c_int
            self.nvda_dll.nvdaController_cancelSpeech.restype = ctypes.c_int
        except Exception as error:
            print(f"Failed to load NVDA DLL: {error}")
            self.nvda_dll = None

    def _load_config(self):
        config = {}
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as handle:
                    config = json.load(handle)
        except Exception:
            config = {}

        raw_mode = config.get("speech_mode", "auto")
        mode_aliases = {
            "force_nvda": "nvda",
            "force_sapi": "system",
        }
        normalized_mode = mode_aliases.get(raw_mode, raw_mode)
        self.mode = normalized_mode if normalized_mode in self._available_modes() else "auto"

        raw_rate = config.get("speech_rate", self.default_rate)
        if isinstance(raw_rate, int):
            self.rate = max(80, min(400, raw_rate))

    def _save_config(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "speech_mode": self.mode,
                        "speech_rate": self.rate,
                    },
                    handle,
                    indent=2,
                )
        except Exception:
            pass

    def _nvda_available(self):
        if not self.nvda_dll:
            return False
        try:
            return self.nvda_dll.nvdaController_testIfRunning() == 0
        except Exception:
            return False

    def _ensure_tts_thread(self):
        if self.tts_thread is None or not self.tts_thread.is_alive():
            self.tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
            self.tts_thread.start()

    def _apply_mode(self):
        if self.platform_name == "Windows":
            nvda_ok = self._nvda_available()
            if self.mode == "nvda":
                self.use_nvda = nvda_ok
                self.backend = "nvda" if nvda_ok else "pyttsx3"
                if not nvda_ok:
                    self._ensure_tts_thread()
            elif self.mode == "system":
                self.use_nvda = False
                self.backend = "pyttsx3"
                self._ensure_tts_thread()
            else:
                self.use_nvda = nvda_ok
                self.backend = "nvda" if nvda_ok else "pyttsx3"
                if not self.use_nvda:
                    self._ensure_tts_thread()
        elif self.mode == "pyttsx3":
            self.use_nvda = False
            self.backend = "pyttsx3"
            self._ensure_tts_thread()
        elif self.platform_name == "Darwin":
            self.use_nvda = False
            self.backend = "say"
            self._ensure_tts_thread()
        else:
            self.use_nvda = False
            self.backend = self._pick_non_windows_backend()
            self._ensure_tts_thread()

    def _pick_non_windows_backend(self):
        available = {name for name, _ in get_speech_backends()}
        if self.mode == "system":
            for preferred in ("say", "spd-say", "espeak-ng", "espeak"):
                if preferred in available:
                    return preferred
        for preferred in ("spd-say", "espeak-ng", "espeak", "pyttsx3"):
            if preferred in available:
                return preferred
        return "pyttsx3"

    def _cancel_nvda_if_possible(self):
        try:
            if self.nvda_dll:
                self.nvda_dll.nvdaController_cancelSpeech()
        except Exception:
            pass

    def _stop_current_process(self):
        with self.process_lock:
            process = self.current_process
            self.current_process = None

        if not process:
            return

        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _clear_queue(self):
        while not self.speech_queue.empty():
            try:
                self.speech_queue.get_nowait()
            except queue.Empty:
                break

    def set_mode(self, mode):
        if mode not in self._available_modes():
            return False
        self.mode = mode
        self._save_config()
        self._apply_mode()
        return True

    def get_mode(self):
        return self.mode

    def set_rate(self, rate):
        if not isinstance(rate, int):
            return False
        self.rate = max(80, min(400, rate))
        self._save_config()
        return True

    def get_rate(self):
        return self.rate

    def uses_screen_reader(self):
        """Whether the voice in use belongs to a screen reader.

        True when NVDA is doing the talking on Windows, and on macOS, where the
        system voice is the one VoiceOver users hear. This is a fact about the
        current voice rather than a setting: an app that would have to read text
        out fragment by fragment can hand whole sentences to the reader instead and
        let its own queue do the pacing.
        """
        return bool(self.use_nvda or self.prefers_native_screen_reader)

    def speak(self, text, interrupt=True):
        # The last gate before a voice sees anything. Every caller is meant to
        # hand over speakable text already, but a character no voice can say is
        # worse than useless: an unpaired surrogate or an emoji that has been
        # through an ANSI boundary turns into a question mark and is read out
        # that way. Cleaning here means no caller can leak one into the engine.
        text = for_speech(text)
        if not text:
            return

        if self.mode == "nvda":
            self.use_nvda = self._nvda_available()
            self.backend = "nvda" if self.use_nvda else "pyttsx3"
            if not self.use_nvda:
                self._ensure_tts_thread()
        elif self.mode in {"system", "pyttsx3"}:
            self.use_nvda = False
            self._apply_mode()
        else:
            self._apply_mode()

        if self.use_nvda:
            if interrupt:
                self._cancel_nvda_if_possible()
            self.nvda_dll.nvdaController_speakText(ctypes.c_wchar_p(text))
            return

        self._ensure_tts_thread()
        if interrupt:
            self._clear_queue()
            self._stop_current_process()
        self.speech_queue.put(text)

    def _speak_with_say(self, text):
        say_path = command_path("say")
        if not say_path:
            print("Speech backend error: macOS `say` command was not found.")
            return

        with self.process_lock:
            self.current_process = subprocess.Popen(
                [say_path, "-r", str(self.rate), text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process = self.current_process

        try:
            process.wait()
        finally:
            with self.process_lock:
                if self.current_process is process:
                    self.current_process = None

    def _speak_with_command(self, command, args, text):
        exe_path = command_path(command)
        if not exe_path:
            print(f"Speech backend error: `{command}` was not found.")
            return

        with self.process_lock:
            self.current_process = subprocess.Popen(
                [exe_path, *args, text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process = self.current_process

        try:
            process.wait()
        finally:
            with self.process_lock:
                if self.current_process is process:
                    self.current_process = None

    def _speak_with_pyttsx3(self, text):
        if pyttsx3 is None:
            print("Speech backend error: pyttsx3 is not installed.")
            return

        engine = pyttsx3.init()
        try:
            engine.setProperty("rate", self.rate)
        except Exception:
            pass
        engine.say(text)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:
            pass

    def _tts_worker(self):
        while True:
            try:
                text = self.speech_queue.get()
                if text:
                    if self.backend == "say":
                        self._speak_with_say(text)
                    elif self.backend == "spd-say":
                        self._speak_with_command("spd-say", [], text)
                    elif self.backend == "espeak-ng":
                        self._speak_with_command("espeak-ng", ["-s", str(self.rate)], text)
                    elif self.backend == "espeak":
                        self._speak_with_command("espeak", ["-s", str(self.rate)], text)
                    else:
                        self._speak_with_pyttsx3(text)
                self.speech_queue.task_done()
            except Exception as error:
                print(f"TTS worker error: {error}")
            time.sleep(0.05)

    def stop(self):
        if self.use_nvda:
            self._cancel_nvda_if_possible()
            return
        self._clear_queue()
        self._stop_current_process()


engine = SpeechEngine()
