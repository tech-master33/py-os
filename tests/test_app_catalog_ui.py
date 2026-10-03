import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wx

from app_catalog_client import CatalogVerificationError
from apps.app_catalog import AppCatalogApp


class FakeAPI:
    def __init__(self):
        self.messages = []

    def speak(self, text, interrupt=True):
        self.messages.append(text)

    def notify(self, title, message, level="info"):
        self.messages.append((title, message, level))

    def is_enhanced_mode(self):
        return True


class FakeButton:
    def __init__(self):
        self.enabled = True

    def Enable(self, enabled=True):
        self.enabled = enabled

    def Disable(self):
        self.enabled = False


class FakeAppList:
    def GetSelection(self):
        return 0


class FakeDetails:
    def SetValue(self, value):
        self.value = value


class InlineThread:
    def __init__(self, target, daemon=False):
        self.target = target

    def start(self):
        self.target()


class CatalogUITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.api = FakeAPI()
        self.app = AppCatalogApp(self.api)
        self.app.apps = [
            {
                "id": "welcome",
                "name": "Welcome",
                "description": "A sample app.",
                "version": "1.2.0",
                "min_pyos_version": "1.0.0",
                "package_size": 120,
                "chunk_size": 1_048_576,
                "chunk_count": 1,
            }
        ]
        self.app.app_list = FakeAppList()
        self.app.install_button = FakeButton()
        self.app.uninstall_button = FakeButton()
        self.app.refresh_button = FakeButton()
        self.app.details = FakeDetails()
        self.app.frame = None
        self.app.client = None

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cancelled_install_does_not_download_or_write_files(self):
        client = type(
            "Client",
            (),
            {"download_verified": lambda *args, **kwargs: self.fail("unexpected download")},
        )()
        self.app.client = client
        with patch.dict(os.environ, {"PY_OS_DATA_DIR": str(self.data_dir)}), patch(
            "apps.app_catalog.wx.MessageBox", return_value=wx.ID_NO
        ):
            self.app.on_install()

        self.assertFalse((self.data_dir / "apps").exists())

    def test_confirmed_install_calls_verified_client_and_updates_ui(self):
        class Client:
            def download_verified(inner_self, app, destination):
                package = Path(destination) / app["id"]
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("pass\n", encoding="utf-8")
                return package

        self.app.client = Client()
        with (
            patch.dict(os.environ, {"PY_OS_DATA_DIR": str(self.data_dir)}),
            patch("apps.app_catalog.wx.MessageBox", return_value=wx.ID_YES),
            patch("apps.app_catalog.wx.CallAfter", side_effect=lambda callback, *args: callback(*args)),
            patch("apps.app_catalog.threading.Thread", InlineThread),
        ):
            self.app.on_install()

        self.assertTrue((self.data_dir / "apps" / "welcome" / "__init__.py").is_file())
        self.assertIn("Installed Welcome", self.app.details.value)

    def test_verification_failure_leaves_no_install_files(self):
        class Client:
            def download_verified(inner_self, app, destination):
                raise CatalogVerificationError("bad signature")

        self.app.client = Client()
        with (
            patch.dict(os.environ, {"PY_OS_DATA_DIR": str(self.data_dir)}),
            patch("apps.app_catalog.wx.MessageBox", return_value=wx.ID_YES),
            patch("apps.app_catalog.wx.CallAfter", side_effect=lambda callback, *args: callback(*args)),
            patch("apps.app_catalog.threading.Thread", InlineThread),
        ):
            self.app.on_install()

        self.assertFalse((self.data_dir / "apps" / "welcome").exists())
        self.assertTrue(any("installation failed" in str(message).lower() for message in self.api.messages))

    def test_uninstall_refuses_a_symlink_to_outside_user_apps(self):
        outside = self.data_dir / "outside"
        outside.mkdir()
        (outside / "important.txt").write_text("keep", encoding="utf-8")
        apps_dir = self.data_dir / "apps"
        apps_dir.mkdir()
        link = apps_dir / "welcome"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is not available")
        with patch.dict(os.environ, {"PY_OS_DATA_DIR": str(self.data_dir)}):
            self.app.on_uninstall()

        self.assertEqual((outside / "important.txt").read_text(encoding="utf-8"), "keep")
        self.assertTrue(link.is_symlink())

    def test_uninstall_removes_only_the_selected_catalog_package(self):
        apps_dir = self.data_dir / "apps"
        target = apps_dir / "welcome"
        other = apps_dir / "other"
        target.mkdir(parents=True)
        other.mkdir()
        (target / ".pyos-catalog.json").write_text(
            json.dumps({"id": "welcome", "version": "1.2.0"}),
            encoding="utf-8",
        )
        (target / "__init__.py").write_text("pass\n", encoding="utf-8")
        (other / "__init__.py").write_text("pass\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {"PY_OS_DATA_DIR": str(self.data_dir)}),
            patch("apps.app_catalog.wx.MessageBox", return_value=wx.ID_YES),
        ):
            self.app.on_uninstall()

        self.assertFalse(target.exists())
        self.assertTrue((other / "__init__.py").is_file())

    def test_refresh_failure_keeps_existing_catalog_entries(self):
        class Client:
            def list_apps(self):
                raise CatalogVerificationError("offline")

        self.app.client = Client()
        with (
            patch("apps.app_catalog.wx.CallAfter", side_effect=lambda callback, *args: callback(*args)),
            patch("apps.app_catalog.threading.Thread", InlineThread),
        ):
            self.app.on_refresh()

        self.assertEqual(self.app.apps[0]["id"], "welcome")
        self.assertTrue(self.app.refresh_button.enabled)


if __name__ == "__main__":
    unittest.main()
