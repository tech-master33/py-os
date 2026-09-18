"""Checks for the engine's answer to "is a screen reader the voice?".

The AI Assistant hands the model's thinking over a sentence at a time only when
this is true, because on a machine whose only voice is PyOS's own engine the same
text would come out fragment by fragment. Getting it wrong either way is quiet: a
screen reader user would stop hearing the thinking, or a user without one would
start hearing it in pieces. Hence its own test, on the real class rather than a
fake.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speech


class ScreenReaderVoiceTests(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get("PY_OS_DATA_DIR")
        self.tmpdir = tempfile.mkdtemp()
        os.environ["PY_OS_DATA_DIR"] = self.tmpdir
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._previous is None:
            os.environ.pop("PY_OS_DATA_DIR", None)
        else:
            os.environ["PY_OS_DATA_DIR"] = self._previous

    def build_engine(self, platform_name="Windows"):
        """An engine with its start-up side effects skipped."""
        with mock.patch.object(speech, "get_platform_name", return_value=platform_name), \
                mock.patch.object(speech.SpeechEngine, "_load_config"), \
                mock.patch.object(speech.SpeechEngine, "_load_nvda_if_available"), \
                mock.patch.object(speech.SpeechEngine, "_apply_mode"):
            return speech.SpeechEngine()

    def test_nvda_speaking_is_a_screen_reader_voice(self):
        engine = self.build_engine("Windows")
        engine.use_nvda = True

        self.assertTrue(engine.uses_screen_reader())

    def test_a_system_voice_on_windows_is_not(self):
        engine = self.build_engine("Windows")
        engine.use_nvda = False

        self.assertFalse(engine.uses_screen_reader())

    def test_the_macos_system_voice_is_one(self):
        # PyOS's own mode list describes this voice as the one that works with
        # VoiceOver, so thinking is still read out on macOS.
        engine = self.build_engine("Darwin")
        engine.use_nvda = False

        self.assertTrue(engine.uses_screen_reader())

    def test_a_linux_system_voice_is_not(self):
        engine = self.build_engine("Linux")
        engine.use_nvda = False

        self.assertFalse(engine.uses_screen_reader())


if __name__ == "__main__":
    unittest.main()
