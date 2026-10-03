import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_paths import get_user_apps_dir
from desktop import discover_user_packages, load_app_package, split_quoted_args


class SplitQuotedArgsTests(unittest.TestCase):
    def test_plain_words(self):
        self.assertEqual(split_quoted_args("copy a.txt b.txt"), ["copy", "a.txt", "b.txt"])

    def test_quoted_path_with_spaces(self):
        result = split_quoted_args('copy "C:\\my folder\\a.txt" out.txt')
        self.assertEqual(result, ["copy", "C:\\my folder\\a.txt", "out.txt"])

    def test_multiple_quoted_segments(self):
        result = split_quoted_args('"first part" "second part" tail')
        self.assertEqual(result, ["first part", "second part", "tail"])

    def test_empty_and_whitespace(self):
        self.assertEqual(split_quoted_args(""), [])
        self.assertEqual(split_quoted_args("   "), [])

    def test_unclosed_quote_keeps_text(self):
        result = split_quoted_args('copy "C:\\my folder')
        self.assertEqual(result, ["copy", "C:\\my folder"])

    def test_collapses_runs_of_spaces(self):
        result = split_quoted_args("a    b")
        self.assertEqual(result, ["a", "b"])

    def test_quoted_spaces_preserved_exactly(self):
        result = split_quoted_args('"two  spaces"')
        self.assertEqual(result, ["two  spaces"])

    def test_windows_path_without_quotes(self):
        result = split_quoted_args(r"C:\Users\name\file.txt")
        self.assertEqual(result, [r"C:\Users\name\file.txt"])


class UserAppPackageTests(unittest.TestCase):
    def test_user_app_path_uses_data_directory_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"PY_OS_DATA_DIR": directory}):
                apps_dir = Path(get_user_apps_dir())

            self.assertEqual(apps_dir, Path(directory) / "apps")
            self.assertFalse(apps_dir.exists())

    def test_discovers_packages_and_imports_sibling_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "bundled"
            user_apps = root / "user_apps"
            package = user_apps / "welcome"
            bundled.mkdir()
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "from api import BlindApp\n"
                "from .helper import MESSAGE\n"
                "class WelcomeApp(BlindApp):\n"
                "    def __init__(self, api):\n"
                "        super().__init__(api)\n"
                "        self.greeting = MESSAGE\n",
                encoding="utf-8",
            )
            (package / "helper.py").write_text(
                'MESSAGE = "hello from a sibling module"\n',
                encoding="utf-8",
            )

            packages = discover_user_packages(bundled, user_apps)
            instances = load_app_package(packages[0], object())

            self.assertEqual(packages, [package])
            self.assertEqual(instances[0].greeting, "hello from a sibling module")

    def test_user_package_cannot_replace_a_bundled_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "bundled"
            user_apps = root / "user_apps"
            bundled.mkdir()
            user_apps.mkdir()
            (bundled / "platform_diagnostics.py").write_text("", encoding="utf-8")
            (user_apps / "platform-diagnostics").mkdir()
            (user_apps / "platform-diagnostics" / "__init__.py").write_text(
                "", encoding="utf-8"
            )

            self.assertEqual(discover_user_packages(bundled, user_apps), [])

    def test_package_loader_enforces_the_user_apps_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "user_apps"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (outside / "__init__.py").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "outside its allowed directory"):
                load_app_package(outside, object(), allowed_root=allowed)

    def test_ignores_symlinked_user_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "bundled"
            user_apps = root / "user_apps"
            external = root / "external"
            bundled.mkdir()
            user_apps.mkdir()
            external.mkdir()
            (external / "__init__.py").write_text("", encoding="utf-8")
            try:
                (user_apps / "linked").symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is not available")

            self.assertEqual(discover_user_packages(bundled, user_apps), [])


if __name__ == "__main__":
    unittest.main()
