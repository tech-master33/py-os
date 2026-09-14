"""Tests for the PyOS reference that grounds every AI provider.

The reference is what stops a model inventing features this simulator does not
have, so the checks here are mostly about accuracy: the facts a user can act on
must be present, the live app list must come from the installed apps, and the
pack must stay inside each provider's budget.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kernel
import pyos_knowledge
from ai_providers import GeminiProvider, OllamaProvider, OpenRouterProvider
from pyos_knowledge import (
    build_knowledge,
    knowledge_fingerprint,
    knowledge_sections,
    render_knowledge,
)

FakeApp = namedtuple("FakeApp", "name description category")

APPS = [
    FakeApp("Terminal", "Command-line interface to the system kernel.", "Tools"),
    FakeApp("AI Assistant", "Chat with a local or online model.", "Tools"),
    {"name": "Help and Documentation", "description": "Read the guides.", "category": "Tools"},
]


class TempDataDirTestCase(unittest.TestCase):
    """Point PyOS at a throwaway data directory for every test."""

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


class PackContentTests(TempDataDirTestCase):
    def test_every_section_is_present_by_default(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])

        self.assertEqual(
            [section.key for section in pack.sections], list(pyos_knowledge.DEFAULT_INCLUDE)
        )
        for key in pyos_knowledge.DEFAULT_INCLUDE:
            self.assertIsNotNone(pack.section(key), f"missing section {key}")

    def test_sections_can_be_chosen(self):
        sections = knowledge_sections(include=["overview"])
        self.assertEqual([section.key for section in sections], ["overview"])

    def test_unknown_section_keys_are_ignored(self):
        sections = knowledge_sections(include=["overview", "trade-secrets"])
        self.assertEqual([section.key for section in sections], ["overview"])

    def test_pack_is_plain_text_with_titled_sections(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])

        self.assertTrue(pack.text.startswith(pyos_knowledge.KNOWLEDGE_TITLE))
        self.assertIn("== What PyOS is ==", pack.text)
        self.assertIn("== Terminal commands ==", pack.text)
        self.assertTrue(pack.text.endswith("\n"))

    def test_the_model_is_told_not_to_invent_features(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])

        self.assertIn("never invent app names, menu items, shortcuts or commands", pack.text)
        self.assertIn("just answer it normally", pack.text)

    def test_the_model_is_told_the_answer_is_read_aloud(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])
        self.assertIn("read aloud", pack.text)

    def test_every_terminal_command_the_kernel_reports_is_documented(self):
        help_text = kernel.VirtualOS().execute("help")
        commands = help_text.split("Available commands: ")[1].split(". ")[0].split(", ")

        pack = build_knowledge(apps=APPS, shells=["bash"])

        self.assertIn("shutdown", commands)
        for command in commands:
            self.assertIn(command, pack.text, f"{command} is missing from the reference")
        self.assertIn("refuse on purpose", pack.text)

    def test_shortcut_facts_are_present(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])

        for fact in ("F1", "Ctrl+D", "Ctrl+T", "Ctrl+W", "Alt+Left", "Backspace"):
            self.assertIn(fact, pack.text, f"{fact} is missing from the reference")

    def test_app_authoring_facts_are_present(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])

        self.assertIn("from api import BlindApp", pack.text)
        self.assertIn("self.api.speak", pack.text)
        self.assertIn("apps/DEVELOPER_GUIDE.md", pack.text)
        self.assertIn("super().on_close(event)", pack.text)

    def test_the_reference_survives_json_encoding(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])

        # The render has to survive JSON encoding, which is how it reaches
        # Gemini and OpenRouter.
        self.assertEqual(json.loads(json.dumps(pack.text)), pack.text)


class LiveFactTests(TempDataDirTestCase):
    def test_the_data_folder_is_named_as_it_really_is(self):
        pack = build_knowledge(apps=APPS)

        self.assertIn(self.tmpdir, pack.text)
        self.assertIn("PY_OS_DATA_DIR", pack.text)

    def test_installed_apps_are_listed_from_the_running_system(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])

        self.assertIn("Apps installed in this copy of PyOS right now:", pack.text)
        self.assertIn("- Terminal — Command-line interface to the system kernel. [Tools]", pack.text)
        self.assertIn("- AI Assistant — Chat with a local or online model. [Tools]", pack.text)
        self.assertIn("- Help and Documentation — Read the guides. [Tools]", pack.text)

    def test_app_metadata_may_be_dicts_objects_or_tuples(self):
        pack = build_knowledge(apps=APPS)
        self.assertIn("Help and Documentation", pack.text)
        self.assertIn("Terminal", pack.text)

    def test_without_live_apps_the_curated_catalogue_stands_alone(self):
        for empty in (None, []):
            pack = build_knowledge(apps=empty, shells=["bash"])
            self.assertNotIn("right now", pack.text)
            self.assertIn("Apps that ship with PyOS include:", pack.text)
            self.assertIn("File Explorer", pack.text)

    def test_apps_without_names_are_skipped(self):
        pack = build_knowledge(apps=[{"description": "no name here"}], shells=["bash"])
        self.assertNotIn("no name here", pack.text)

    def test_host_shells_come_from_the_platform_layer(self):
        pack = build_knowledge(apps=APPS, shells={"zsh": "/bin/zsh", "bash": "/bin/bash"})
        self.assertIn("bash, zsh", pack.text)

    def test_no_shells_is_stated_plainly(self):
        pack = build_knowledge(apps=APPS, shells={})
        self.assertIn("none found", pack.text)

    def test_shell_detection_never_raises(self):
        pack = build_knowledge(apps=APPS)
        self.assertIn("host shells available on this computer", pack.text)


class BudgetTests(TempDataDirTestCase):
    def test_the_whole_pack_fits_the_local_budget(self):
        pack = build_knowledge(apps=APPS)

        self.assertEqual(pack.dropped, [])
        self.assertFalse(pack.truncated)
        self.assertLessEqual(pack.char_count, OllamaProvider.system_char_limit)

    def test_cloud_providers_get_more_room_than_ollama(self):
        self.assertLess(
            OllamaProvider.system_char_limit, GeminiProvider.system_char_limit
        )
        self.assertEqual(
            GeminiProvider.system_char_limit, OpenRouterProvider.system_char_limit
        )

    def test_a_tight_budget_drops_the_least_important_sections_first(self):
        pack = build_knowledge(apps=APPS, shells=["bash"], max_chars=6000)

        keys = [section.key for section in pack.sections]
        self.assertEqual(pack.dropped, ["The AI Assistant itself", "Writing an app for PyOS"])
        self.assertIn("overview", keys)
        self.assertIn("desktop_and_keys", keys)
        self.assertIn("terminal", keys)

    def test_essential_sections_survive_an_extreme_budget(self):
        pack = build_knowledge(apps=APPS, shells=["bash"], max_chars=1200)

        self.assertEqual(
            [section.key for section in pack.sections], ["overview", "desktop_and_keys"]
        )
        self.assertTrue(pack.truncated)
        self.assertLessEqual(pack.char_count, 1200)
        self.assertFalse(pack.text.endswith("\n\n"))

    def test_a_budget_that_is_not_reached_changes_nothing(self):
        full = build_knowledge(apps=APPS, shells=["bash"])
        roomy = build_knowledge(apps=APPS, shells=["bash"], max_chars=100000)

        self.assertEqual(full.text, roomy.text)
        self.assertEqual(full.dropped, [])


class SummaryTests(TempDataDirTestCase):
    def test_summary_describes_the_pack(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])
        summary = pack.summary()

        self.assertIn(pack.fingerprint, summary)
        self.assertIn(f"{pack.char_count} characters", summary)
        self.assertIn(f"{len(pack.sections)} sections", summary)

    def test_summary_reports_what_was_left_out(self):
        pack = build_knowledge(apps=APPS, shells=["bash"], max_chars=6000)
        self.assertIn("The AI Assistant itself", pack.summary())

    def test_fingerprint_tracks_the_content(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])
        another = build_knowledge(apps=APPS, shells=["powershell"])

        self.assertNotEqual(pack.fingerprint, another.fingerprint)
        self.assertEqual(pack.fingerprint, knowledge_fingerprint(pack.text))
        self.assertEqual(knowledge_fingerprint("same"), knowledge_fingerprint("same"))

    def test_titles_list_the_sections_in_order(self):
        pack = build_knowledge(apps=APPS, shells=["bash"])
        self.assertEqual(pack.titles[0], "What PyOS is")
        self.assertEqual(len(pack.titles), len(pack.sections))


class RenderingTests(TempDataDirTestCase):
    def test_render_keeps_the_sections_in_order(self):
        sections = knowledge_sections(include=["overview", "terminal"], shells=["bash"])
        text = render_knowledge(sections)

        self.assertLess(text.index("What PyOS is"), text.index("Terminal commands"))

    def test_the_module_loads_without_wxpython(self):
        """The assistant's grounding must be testable headless, like ai_providers."""
        script = (
            "import sys\n"
            "class Blocker:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'wx' or name.startswith('wx.'):\n"
            "            raise ImportError('wx is blocked for this test')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "import pyos_knowledge\n"
            "pack = pyos_knowledge.build_knowledge(\n"
            "    apps=[('Terminal', 'Command line.', 'Tools')], shells=['bash']\n"
            ")\n"
            "assert pack.section('writing_apps') is not None\n"
            "assert 'wx' not in sys.modules\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
