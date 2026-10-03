import json
import re
import shutil
import threading
from pathlib import Path

import wx

from api import BlindApp
from app_catalog_client import CatalogClient, CatalogError
from app_paths import get_user_apps_dir

CATALOG_API_URL = "https://pyos-app-catalog.tech-chat.workers.dev"
CATALOG_PUBLIC_KEY = "5MXwukzUkk7iWuIZo9xvbhYwf9Yeg+/3A0up1p41isY="
INSTALL_WARNING = (
    "Apps run locally with PyOS's permissions and are not sandboxed. "
    "Install only apps from a publisher you trust."
)


def read_install_metadata(package_dir):
    try:
        with (Path(package_dir) / ".pyos-catalog.json").open("rb") as metadata_file:
            raw = metadata_file.read(4_097)
        if len(raw) > 4_096:
            return None
        metadata = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("id"), str)
        or not isinstance(metadata.get("version"), str)
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", metadata["id"]) is None
        or re.fullmatch(
            r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
            r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
            metadata["version"],
        ) is None
    ):
        return None
    return metadata


class AppCatalogApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "PyOS App Catalog"
        self.description = "Browse and manage trusted PyOS app packages."
        self.category = "Tools"
        self.help_text = (
            "Use Refresh Catalog to browse apps. Select an app and choose Install or Update "
            "after reviewing its details. Catalog apps run with PyOS permissions."
        )
        self.docs = (
            "The catalog downloads maintainer-signed packages over HTTPS, verifies their "
            "signatures and file hashes, and installs them in your per-user apps folder."
        )
        self.apps = []
        self.client = None
        self.frame = None
        self.app_list = None
        self.details = None
        self.refresh_button = None
        self.install_button = None
        self.uninstall_button = None

    def _make_client(self):
        if self.client is not None:
            return self.client
        if not CATALOG_API_URL or not CATALOG_PUBLIC_KEY:
            raise CatalogError("The PyOS App Catalog server has not been configured.")
        self.client = CatalogClient(CATALOG_API_URL, CATALOG_PUBLIC_KEY)
        return self.client

    def run(self):
        self.frame = wx.Frame(None, title=self.name, size=(760, 560))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=(
                "Browse signed PyOS apps. Installed apps execute locally and are not sandboxed."
            ),
        )
        intro.SetForegroundColour(wx.Colour(230, 230, 230))
        sizer.Add(intro, 0, wx.ALL, 12)

        self.app_list = wx.ListBox(panel, choices=[], style=wx.LB_SINGLE)
        self.app_list.SetBackgroundColour(wx.Colour(20, 20, 20))
        self.app_list.SetForegroundColour(wx.Colour(255, 255, 255))
        self.app_list.Bind(wx.EVT_SET_FOCUS, lambda event: self.api.speak("Available apps"))
        self.app_list.Bind(wx.EVT_LISTBOX, self.on_select)
        self.app_list.Bind(wx.EVT_LISTBOX_DCLICK, self.on_install)
        sizer.Add(self.app_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.details = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.details.SetMinSize((-1, 120))
        self.details.SetBackgroundColour(wx.Colour(20, 20, 20))
        self.details.SetForegroundColour(wx.Colour(235, 235, 235))
        self.details.SetValue(INSTALL_WARNING)
        sizer.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.refresh_button = wx.Button(panel, label="&Refresh Catalog")
        self.install_button = wx.Button(panel, label="&Install / Update")
        self.uninstall_button = wx.Button(panel, label="&Uninstall")
        close_button = wx.Button(panel, label="&Close")
        for button, label in (
            (self.refresh_button, "Refresh catalog"),
            (self.install_button, "Install or update app"),
            (self.uninstall_button, "Uninstall app"),
            (close_button, "Close catalog"),
        ):
            button.Bind(wx.EVT_SET_FOCUS, lambda event, text=label: self.api.speak(text))
        self.refresh_button.Bind(wx.EVT_BUTTON, self.on_refresh)
        self.install_button.Bind(wx.EVT_BUTTON, self.on_install)
        self.uninstall_button.Bind(wx.EVT_BUTTON, self.on_uninstall)
        close_button.Bind(wx.EVT_BUTTON, self.on_close)
        buttons.Add(self.refresh_button, 0, wx.RIGHT, 8)
        buttons.Add(self.install_button, 0, wx.RIGHT, 8)
        buttons.Add(self.uninstall_button, 0, wx.RIGHT, 8)
        buttons.AddStretchSpacer(1)
        buttons.Add(close_button, 0)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)

        panel.SetSizer(sizer)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        self.frame.Show()
        self.refresh_button.SetFocus()
        self.api.speak(
            "App Catalog opened. Installed apps are executable code and are not sandboxed."
        )
        self.on_refresh()

    def _selected_app(self):
        selection = self.app_list.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self.apps):
            return None
        return self.apps[selection]

    def on_select(self, event=None):
        app = self._selected_app()
        if app is None:
            return
        installed = read_install_metadata(Path(get_user_apps_dir()) / app["id"])
        state = (
            f"Installed version: {installed['version']}"
            if installed
            else "Not installed"
        )
        self.details.SetValue(
            f"{app['name']} ({app['id']})\n"
            f"Version: {app['version']}\n"
            f"Requires PyOS: {app['min_pyos_version']} or newer\n"
            f"Package size: {app['package_size']:,} bytes\n"
            f"{state}\n\n{app['description']}\n\n{INSTALL_WARNING}"
        )
        self.install_button.Enable(True)
        self.uninstall_button.Enable(installed is not None)
        if not self.api.is_enhanced_mode():
            self.api.speak(
                f"{app['name']}, version {app['version']}. {app['description']} {state}."
            )

    def on_refresh(self, event=None):
        self.refresh_button.Disable()
        self.install_button.Disable()
        self.uninstall_button.Disable()

        def refresh():
            try:
                apps = self._make_client().list_apps()
            except CatalogError as error:
                wx.CallAfter(self._refresh_failed, str(error))
                return
            wx.CallAfter(self._refresh_succeeded, apps)

        threading.Thread(target=refresh, daemon=True).start()

    def _refresh_failed(self, message):
        self.refresh_button.Enable()
        self._restore_selection_actions()
        self._notify_error(f"Could not refresh the app catalog: {message}")

    def _refresh_succeeded(self, apps):
        self.apps = apps
        self.app_list.Set([f"{app['name']} — {app['description']}" for app in apps])
        self.refresh_button.Enable()
        self.install_button.Disable()
        self.uninstall_button.Disable()
        self.details.SetValue(
            f"{len(apps)} apps available.\n\n{INSTALL_WARNING}"
        )
        self.api.speak(f"Catalog refreshed. {len(apps)} apps available.")
        if apps:
            self.app_list.SetSelection(0)
            self.on_select()

    def _restore_selection_actions(self):
        app = self._selected_app()
        if app is None:
            self.install_button.Disable()
            self.uninstall_button.Disable()
            return
        metadata = read_install_metadata(Path(get_user_apps_dir()) / app["id"])
        self.install_button.Enable()
        self.uninstall_button.Enable(metadata is not None and metadata["id"] == app["id"])

    def on_install(self, event=None):
        app = self._selected_app()
        if app is None:
            self._notify_error("Select an app first.")
            return
        result = wx.MessageBox(
            f"Install or update {app['name']} version {app['version']}?\n\n"
            f"{INSTALL_WARNING}",
            "Confirm app installation",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self.frame,
        )
        if result != wx.ID_YES:
            self.api.speak("Installation cancelled.")
            return

        self.install_button.Disable()
        self.uninstall_button.Disable()

        def install():
            try:
                destination = self._make_client().download_verified(
                    app,
                    get_user_apps_dir(),
                )
            except (CatalogError, OSError) as error:
                wx.CallAfter(self._install_failed, app, str(error))
                return
            wx.CallAfter(self._install_succeeded, app, destination)

        threading.Thread(target=install, daemon=True).start()

    def _install_failed(self, app, message):
        self.install_button.Enable()
        installed = read_install_metadata(Path(get_user_apps_dir()) / app["id"])
        self.uninstall_button.Enable(
            installed is not None and installed["id"] == app["id"]
        )
        self._notify_error(f"App installation failed: {message}")

    def _install_succeeded(self, app, destination):
        self.install_button.Enable()
        self.uninstall_button.Enable()
        self.details.SetValue(
            f"Installed {app['name']} version {app['version']}.\n"
            f"Location: {destination}\n\n{INSTALL_WARNING}"
        )
        self.api.speak(f"{app['name']} was installed successfully.")

    def on_uninstall(self, event=None):
        app = self._selected_app()
        if app is None:
            self._notify_error("Select an app first.")
            return
        root = Path(get_user_apps_dir()).resolve()
        target = root / app["id"]
        metadata = read_install_metadata(target)
        if target.is_symlink() or metadata is None or metadata["id"] != app["id"]:
            self._notify_error("This catalog app is not installed in the user apps folder.")
            return
        try:
            target.resolve().relative_to(root)
        except ValueError:
            self._notify_error("The app path is outside the user apps folder.")
            return
        result = wx.MessageBox(
            f"Uninstall {app['name']} and remove its files?",
            "Confirm app removal",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self.frame,
        )
        if result != wx.ID_YES:
            self.api.speak("Uninstall cancelled.")
            return
        try:
            shutil.rmtree(target)
        except OSError as error:
            self._notify_error(f"Could not uninstall {app['name']}: {error}")
            return
        self.uninstall_button.Disable()
        self.api.speak(f"{app['name']} was uninstalled.")
        self.on_select()

    def _notify_error(self, message):
        self.api.notify("PyOS App Catalog", message, level="error")
