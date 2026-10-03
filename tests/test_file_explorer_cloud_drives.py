import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wx

from apps.system_apps import FileExplorerApp


class FakeKernel:
    def __init__(self, root):
        self.root_dir = str(root)


class FakeAPI:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.messages = []
        self.launched = []

    def get_data_path(self, filename):
        return str(self.data_dir / filename)

    def get_vfs(self):
        return FakeKernel(self.data_dir / "vfs")

    def speak(self, text, interrupt=True):
        self.messages.append(text)

    def launch_app(self, app_name, **kwargs):
        self.launched.append((app_name, kwargs))

    def is_enhanced_mode(self):
        return True


class FakeList:
    def __init__(self):
        self.items = []
        self.selected = -1

    def DeleteAllItems(self):
        self.items.clear()

    def GetItemCount(self):
        return len(self.items)

    def InsertItem(self, index, name):
        self.items.insert(index, [name, ""])

    def SetItem(self, index, column, value):
        self.items[index][column] = value

    def GetFirstSelected(self):
        return self.selected

    def SetItemState(self, index, state, mask):
        self.selected = index


class FakeControl:
    def __init__(self):
        self.enabled = True
        self.value = ""

    def Enable(self, value=True):
        self.enabled = value

    def SetValue(self, value):
        self.value = value


class FakeFrame:
    def SetTitle(self, title):
        self.title = title


class FakeDriveClient:
    def __init__(self):
        self.drive = {
            "id": "A" * 32,
            "name": "Shared",
            "quota_bytes": 1_048_576,
            "used_bytes": 0,
        }
        self.entries = []
        self.calls = []

    def get_drive(self, drive_id):
        self.calls.append(("get", drive_id))
        if drive_id != self.drive["id"]:
            raise ValueError("unknown drive")
        return dict(self.drive)

    def list_entries(self, drive_id, path=""):
        self.calls.append(("list", drive_id, path))
        return list(self.entries)

    def create_directory(self, drive_id, path):
        self.calls.append(("mkdir", drive_id, path))

    def delete_entry(self, drive_id, path):
        self.calls.append(("delete", drive_id, path))
        return {"status": "deleted", "cleanup_pending": False}

    def delete_empty_drive(self, drive_id):
        self.calls.append(("delete_drive", drive_id))
        return {"status": "deleted"}

    def create_drive(self, name, quota_bytes):
        self.calls.append(("create", name, quota_bytes))
        return {**self.drive, "name": name, "quota_bytes": quota_bytes}

    def upload_file(self, drive_id, local_path, remote_path):
        self.calls.append(("upload", drive_id, local_path, remote_path))
        return {"status": "complete", "cleanup_pending": False}


class InlineThread:
    def __init__(self, target, daemon=False):
        self.target = target

    def start(self):
        self.target()


class FileExplorerCloudDriveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.api = FakeAPI(self.temporary.name)
        self.app = FileExplorerApp(self.api)
        self.app.cloud_client = FakeDriveClient()
        self.app.list = FakeList()
        self.app.frame = FakeFrame()
        self.app.address_bar = FakeControl()
        self.app.back_button = FakeControl()
        self.app.upload_button = FakeControl()
        self.app.new_folder_button = FakeControl()
        self.app.delete_entry_button = FakeControl()
        self.app.remove_connection_button = FakeControl()
        self.app.delete_drive_button = FakeControl()
        self.app.current_source = None
        self.app.current_dir = None
        self.app.cloud_connections = []

    def test_saved_connection_contains_only_drive_id_and_display_name(self):
        self.app.cloud_connections = [{"id": "A" * 32, "name": "Shared"}]
        self.app._save_cloud_connections()

        saved = json.loads(Path(self.app.cloud_connections_path).read_text(encoding="utf-8"))
        self.assertEqual(saved, [{"id": "A" * 32, "name": "Shared"}])

    def test_malformed_connections_are_not_overwritten(self):
        original = b'{"not": "a drive list"}'
        Path(self.app.cloud_connections_path).write_bytes(original)
        self.app._load_cloud_connections()

        self.assertTrue(self.app.cloud_connections_load_error)
        with self.assertRaises(OSError):
            self.app._save_cloud_connections()
        self.assertEqual(Path(self.app.cloud_connections_path).read_bytes(), original)
        self.assertTrue(any("invalid format" in message for message in self.api.messages))

    def test_this_pc_lists_saved_drives_and_connects_by_id(self):
        self.app.cloud_connections = [{"id": "A" * 32, "name": "Shared"}]
        self.app.refresh_files()
        self.assertIn(
            ("Shared Drive: Shared", True, "A" * 32, "cloud_drive"),
            self.app.items,
        )

        self.app._connect_shared_drive("A" * 32)
        self.assertEqual(self.app.current_source, "cloud")
        self.assertEqual(self.app.cloud_drive_id, "A" * 32)
        self.assertEqual(self.app.address_bar.value, "Shared:/")
        self.assertTrue(self.app.upload_button.enabled)

    def test_remote_navigation_uses_posix_paths_and_never_host_paths(self):
        self.app.cloud_connections = [{"id": "A" * 32, "name": "Shared"}]
        self.app._connect_shared_drive("A" * 32)
        self.app.cloud_client.entries = [
            {
                "name": "notes",
                "type": "directory",
                "size_bytes": 0,
                "updated_at": "2026-10-03T00:00:00Z",
            }
        ]
        self.app.refresh_files()
        self.app.items = [("notes", True, "notes", "cloud")]
        self.app.on_item_activated(type("Event", (), {"GetIndex": lambda _self: 0})())

        self.assertEqual(self.app.current_dir, "notes")
        self.assertIn(("list", "A" * 32, "notes"), self.app.cloud_client.calls)
        self.assertFalse(any(call[0] in {"os.listdir", "host"} for call in self.app.cloud_client.calls))

    def test_add_drive_persists_verified_connection(self):
        self.app._run_cloud_task = lambda _action, task, done: done(task())
        dialog = type(
            "Dialog",
            (),
            {
                "ShowModal": lambda _self: wx.ID_OK,
                "GetValue": lambda _self: "A" * 32,
                "Destroy": lambda _self: None,
            },
        )
        with patch("apps.system_apps.wx.TextEntryDialog", return_value=dialog()):
            self.app.on_add_shared_drive()

        self.assertEqual(self.app.cloud_connections, [{"id": "A" * 32, "name": "Shared"}])
        self.assertTrue(Path(self.app.cloud_connections_path).exists())

    def test_create_drive_warns_and_reserves_selected_mib_quota(self):
        dialogs = [
            type(
                "Dialog",
                (),
                {
                    "ShowModal": lambda _self: wx.ID_OK,
                    "GetValue": lambda _self: "Shared",
                    "Destroy": lambda _self: None,
                },
            )(),
            type(
                "Dialog",
                (),
                {
                    "ShowModal": lambda _self: wx.ID_OK,
                    "GetValue": lambda _self: "2",
                    "Destroy": lambda _self: None,
                },
            )(),
        ]
        messages = []
        self.app._run_cloud_task = lambda _action, task, done: done(task())
        with (
            patch("apps.system_apps.wx.TextEntryDialog", side_effect=dialogs),
            patch(
                "apps.system_apps.wx.MessageBox",
                side_effect=lambda *args, **kwargs: messages.append(args) or wx.YES,
            ),
        ):
            self.app.on_create_shared_drive()

        self.assertIn(("create", "Shared", 2 * 1024 * 1024), self.app.cloud_client.calls)
        self.assertIn("read, upload, overwrite, and delete", messages[0][0])
        self.assertIn("A" * 32, messages[-1][0])

    def test_upload_uses_current_remote_folder_and_selected_local_file(self):
        self.app.current_source = "cloud"
        self.app.current_dir = "notes"
        self.app.cloud_drive_id = "A" * 32
        self.app.cloud_drive_name = "Shared"
        local_file = Path(self.temporary.name) / "report.txt"
        local_file.write_text("content", encoding="utf-8")
        dialog = type(
            "Dialog",
            (),
            {
                "ShowModal": lambda _self: wx.ID_OK,
                "GetPath": lambda _self: str(local_file),
                "Destroy": lambda _self: None,
            },
        )()
        self.app._run_cloud_task = lambda _action, task, done: done(task())

        with patch("apps.system_apps.wx.FileDialog", return_value=dialog):
            self.app.on_upload_to_shared_drive()

        self.assertIn(
            ("upload", "A" * 32, str(local_file), "notes/report.txt"),
            self.app.cloud_client.calls,
        )

    def test_remove_connection_does_not_delete_remote_drive(self):
        self.app.cloud_connections = [{"id": "A" * 32, "name": "Shared"}]
        self.app.items = [("Shared Drive: Shared", True, "A" * 32, "cloud_drive")]
        self.app.list.selected = 0
        self.app.on_remove_drive_connection()

        self.assertEqual(self.app.cloud_connections, [])
        self.assertFalse(any(call[0] == "delete_drive" for call in self.app.cloud_client.calls))

    def test_cancelled_remote_delete_leaves_entry_unchanged(self):
        self.app.current_source = "cloud"
        self.app.cloud_drive_id = "A" * 32
        self.app.items = [("notes.txt", False, "notes.txt", "cloud")]
        self.app.list.selected = 0
        with patch("apps.system_apps.wx.MessageBox", return_value=wx.ID_NO):
            self.app.on_delete_cloud_entry()

        self.assertFalse(any(call[0] == "delete" for call in self.app.cloud_client.calls))


if __name__ == "__main__":
    unittest.main()
