import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sounds
from sounds import is_valid_theme_name, notes_to_tone_string, parse_tone_string


class ParseToneStringTests(unittest.TestCase):
    def test_basic_sequence(self):
        notes, error = parse_tone_string("440, 660, 0", "200, 300, 100")
        self.assertIsNone(error)
        self.assertEqual(notes, [(440.0, 200), (660.0, 300), (0.0, 100)])

    def test_space_separated_and_loose_formatting(self):
        notes, error = parse_tone_string(" 440  660 ", "200,300")
        self.assertIsNone(error)
        self.assertEqual(notes, [(440.0, 200), (660.0, 300)])

    def test_count_mismatch_is_rejected(self):
        notes, error = parse_tone_string("440, 660", "200")
        self.assertIsNone(notes)
        self.assertIn("match", error)

    def test_bad_frequency_is_rejected(self):
        notes, error = parse_tone_string("440, abc", "200, 300")
        self.assertIsNone(notes)
        self.assertIn("not a number", error)

    def test_bad_duration_is_rejected(self):
        notes, error = parse_tone_string("440", "0")
        self.assertIsNone(notes)
        self.assertIn("Duration", error)

    def test_frequency_out_of_range(self):
        notes, error = parse_tone_string("30000", "200")
        self.assertIsNone(notes)
        self.assertIn("hertz", error)

    def test_empty_input(self):
        notes, error = parse_tone_string("", "   ")
        self.assertIsNone(notes)
        self.assertTrue(error)

    def test_notes_to_tone_string_round_trip(self):
        notes = [(440.0, 200), (660.5, 300)]
        frequencies, durations = notes_to_tone_string(notes)
        reparsed, error = parse_tone_string(frequencies, durations)
        self.assertIsNone(error)
        self.assertEqual(reparsed, notes)


class ThemeNameTests(unittest.TestCase):
    def test_accepts_normal_names(self):
        ok, error = is_valid_theme_name("My Cool Theme")
        self.assertTrue(ok, error)

    def test_rejects_windows_invalid_characters(self):
        for char in '\\/:*?"<>|':
            ok, error = is_valid_theme_name(f"bad{char}name")
            self.assertFalse(ok)
            self.assertTrue(error)

    def test_rejects_empty_and_dots(self):
        for name in ("", "   ", ".", ".."):
            ok, error = is_valid_theme_name(name)
            self.assertFalse(ok)

    def test_rejects_control_characters(self):
        ok, error = is_valid_theme_name("bad\nname")
        self.assertFalse(ok)


class SoundManagerMusicConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.manager = sounds.SoundManager(self.data_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _music_config(self):
        with open(self.manager.music_config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_saving_music_preserves_volume(self):
        with open(self.manager.music_config_path, "w", encoding="utf-8") as f:
            json.dump({"music": "None", "volume": 73}, f)
        self.manager.save_background_music("loop.ogg")
        config = self._music_config()
        self.assertEqual(config["music"], "loop.ogg")
        self.assertEqual(config["volume"], 73)

    def test_saving_music_creates_defaults_when_missing(self):
        self.manager.save_background_music("loop.ogg")
        config = self._music_config()
        self.assertEqual(config["music"], "loop.ogg")
        self.assertEqual(config.get("volume", 50), 50)

    def test_music_less_theme_returns_none(self):
        # Built-in Modern/Retro/Classic define no background_music key.
        self.assertIsNone(self.manager.get_theme_background_music("Modern"))
        self.assertIsNone(self.manager.get_theme_background_music("Retro"))
        self.assertIsNone(self.manager.get_theme_background_music("Classic"))

    def test_theme_with_music_returns_path(self):
        self.manager.themes["FakeXP"] = {"background_music": "loop.ogg"}
        value = self.manager.get_theme_background_music("FakeXP")
        self.assertTrue(value and value != "None")

    def test_save_theme_name_keeps_user_music_for_music_less_theme(self):
        self.manager.save_background_music("loop.ogg")
        self.manager.save_theme_name("Modern")
        self.assertEqual(self._music_config()["music"], "loop.ogg")

    def test_switching_music_preserves_volume_end_to_end(self):
        with open(self.manager.music_config_path, "w", encoding="utf-8") as f:
            json.dump({"music": "loop.ogg", "volume": 20}, f)
        self.manager.save_theme_name("Retro")  # music-less theme
        self.manager.save_background_music("BK_music.ogg")
        config = self._music_config()
        self.assertEqual(config["music"], "BK_music.ogg")
        self.assertEqual(config["volume"], 20)


class SoundManagerValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = sounds.SoundManager(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_theme_data_types_survive_manager(self):
        # Tone sequences (lists) must remain lists through theme storage so the
        # playback chain treats them as tones, not file paths.
        self.manager.themes["ToneTheme"] = {"startup": [(440, 200)], "nav": [(600, 50)]}
        data = self.manager.themes["ToneTheme"]
        self.assertIsInstance(data["startup"], list)

    def test_play_preview_rejects_missing_file(self):
        self.assertFalse(self.manager.play_preview("Z:/nope/missing.wav"))

    def test_play_preview_rejects_empty_values(self):
        self.assertFalse(self.manager.play_preview(None))
        self.assertFalse(self.manager.play_preview(""))
        self.assertFalse(self.manager.play_preview([]))

    def test_resolve_music_path_handles_none(self):
        self.assertIsNone(self.manager.resolve_music_path("None"))
        self.assertIsNone(self.manager.resolve_music_path(None))


if __name__ == "__main__":
    unittest.main()
