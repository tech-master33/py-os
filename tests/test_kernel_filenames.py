"""Filename handling in the Terminal kernel.

The kernel used to lowercase the whole command line and split it on spaces,
which corrupted typed file names: ``open Report.txt`` looked for
``report.txt`` and ``open My File.txt`` only opened ``My``. The command word
is still matched case-insensitively, but arguments keep their case and
double quotes hold a name with spaces together.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arg_split import split_quoted_args
from kernel import VirtualOS


class KernelFilenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.os_kernel = VirtualOS(root_dir=self.tmp.name)

    def _make_file(self, name, content="hello"):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    # -- case is preserved in arguments ------------------------------------
    def test_open_keeps_mixed_case_name(self):
        self._make_file("Report.txt")
        result = self.os_kernel.execute("open Report.txt")
        self.assertIn("Reading Report.txt", result)
        self.assertIn("hello", result)

    def test_create_keeps_mixed_case_name(self):
        result = self.os_kernel.execute("create Notes.txt")
        self.assertIn("Notes.txt", result)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "Notes.txt")))

    def test_delete_keeps_mixed_case_name(self):
        self._make_file("Important.txt")
        result = self.os_kernel.execute("delete Important.txt")
        self.assertIn("Deleted Important.txt", result)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "Important.txt")))

    def test_command_word_still_case_insensitive(self):
        self._make_file("plain.txt")
        self.assertIn("Reading plain.txt", self.os_kernel.execute("OPEN plain.txt"))
        self.assertIn("Reading plain.txt", self.os_kernel.execute("Open plain.txt"))

    # -- quoted names with spaces ------------------------------------------
    def test_open_quoted_name_with_spaces(self):
        self._make_file("My File.txt", "spaced content")
        result = self.os_kernel.execute('open "My File.txt"')
        self.assertIn("Reading My File.txt", result)
        self.assertIn("spaced content", result)

    def test_create_quoted_name_with_spaces(self):
        result = self.os_kernel.execute('create "New Document.txt"')
        self.assertIn("New Document.txt", result)
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp.name, "New Document.txt"))
        )

    def test_delete_quoted_name_with_spaces(self):
        self._make_file("Old Report.txt")
        result = self.os_kernel.execute('delete "Old Report.txt"')
        self.assertIn("Deleted Old Report.txt", result)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "Old Report.txt")))

    # -- text encoding ------------------------------------------------------
    def test_open_reads_utf8_content(self):
        self._make_file("cafe.txt", "café and ☕")
        result = self.os_kernel.execute("open cafe.txt")
        self.assertIn("café and ☕", result)

    def test_open_reports_non_utf8_file(self):
        path = os.path.join(self.tmp.name, "binary.txt")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe\x00\x01")
        result = self.os_kernel.execute("open binary.txt")
        self.assertIn("not a UTF-8 text file", result)

    def test_welcome_file_still_created_utf8(self):
        fresh = os.path.join(self.tmp.name, "fresh")
        os_kernel = VirtualOS(root_dir=fresh)
        welcome = os.path.join(fresh, "welcome.txt")
        self.assertTrue(os.path.exists(welcome))
        with open(welcome, "r", encoding="utf-8") as handle:
            self.assertIn("Welcome", handle.read())

    # -- paths stay inside the drive ----------------------------------------
    def test_relative_paths_resolve_inside_root_on_windows(self):
        # normpath yields backslashes on Windows; the resolved path must still
        # land inside the drive rather than at the volume root.
        real = self.os_kernel.get_real_path("Report.txt")
        self.assertTrue(
            os.path.abspath(real).lower().startswith(
                os.path.abspath(self.tmp.name).lower()
            )
        )
        self.assertTrue(
            self.os_kernel.get_real_path("/Report.txt").startswith(self.tmp.name)
        )

    def test_open_from_subdirectory_after_change(self):
        self._make_file(os.path.join("documents", "Inside.txt"), "deep")
        self.os_kernel.execute("open documents")
        result = self.os_kernel.execute("open Inside.txt")
        self.assertIn("Reading Inside.txt", result)
        self.assertIn("deep", result)

    # -- time gains seconds -------------------------------------------------
    def test_time_speaks_seconds(self):
        result = self.os_kernel.execute("time")
        self.assertRegex(result, r"\d{2}:\d{2}:\d{2}")

    # -- error paths still behave -------------------------------------------
    def test_open_without_argument(self):
        self.assertIn("Please specify a file name", self.os_kernel.execute("open"))

    def test_empty_command(self):
        self.assertIn("No command entered", self.os_kernel.execute("   "))


class SplitQuotedArgsParityTests(unittest.TestCase):
    """kernel and desktop must share one implementation of the splitter."""

    def test_desktop_reexports_kernel_implementation(self):
        import desktop  # noqa: F401  (imports wx; fine on the dev machine)

        from desktop import split_quoted_args as desktop_split
        self.assertIs(desktop_split, split_quoted_args)

    def test_splitter_basics(self):
        self.assertEqual(
            split_quoted_args('copy "my file.txt" out.txt'),
            ["copy", "my file.txt", "out.txt"],
        )
        self.assertEqual(split_quoted_args("  a   b  "), ["a", "b"])
        self.assertEqual(split_quoted_args('"unclosed quote'), ["unclosed quote"])


if __name__ == "__main__":
    unittest.main()
