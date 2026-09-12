import fnmatch
import os

import wx


class PyOSFileDialog(wx.Dialog):
    """Shared Open/Save dialog for the PyOS Drive and host files."""

    def __init__(self, parent, api, mode="open", title=None, wildcard="All files (*.*)|*.*"):
        super().__init__(parent, title=title or ("Open File" if mode == "open" else "Save File"),
                         size=(650, 500))
        self.api = api
        self.mode = mode
        self.patterns = self._parse_patterns(wildcard)
        self.vfs_root = os.path.abspath(api.get_vfs().root_dir)
        self.current_dir = None
        self.items = []
        self.result = None

        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.location = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.location.Bind(wx.EVT_TEXT_ENTER, self._go_to_location)
        layout.Add(self.location, 0, wx.EXPAND | wx.ALL, 8)

        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "Name", width=480)
        self.list.InsertColumn(1, "Type", width=100)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._activate)
        layout.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.filename = wx.TextCtrl(panel) if mode == "save" else None
        if self.filename:
            self.filename.Bind(wx.EVT_TEXT_ENTER, self._accept)
            layout.Add(self.filename, 0, wx.EXPAND | wx.ALL, 8)

        buttons = wx.StdDialogButtonSizer()
        up = wx.Button(panel, wx.ID_UP, "Up")
        cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        accept = wx.Button(panel, wx.ID_OK, "Open" if mode == "open" else "Save")
        up.Bind(wx.EVT_BUTTON, self._go_up)
        accept.Bind(wx.EVT_BUTTON, self._accept)
        buttons.AddButton(up)
        buttons.AddButton(cancel)
        buttons.AddButton(accept)
        buttons.Realize()
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(layout)
        self.refresh()

    @staticmethod
    def _parse_patterns(wildcard):
        patterns = []
        parts = wildcard.split("|")
        for index, part in enumerate(parts):
            if index % 2 == 1:
                patterns.extend(value.lower() for value in part.split(";") if value)
        if not patterns and len(parts) == 2:
            patterns.append(parts[1].lower())
        return patterns or ["*"]

    def refresh(self):
        self.list.DeleteAllItems()
        self.items = []
        if self.current_dir is None:
            self._add("PyOS Drive", True, self.vfs_root)
            self._add("Host Files", True, os.path.abspath(os.sep))
            self.location.SetValue("This PC")
            return
        try:
            names = sorted(os.listdir(self.current_dir),
                           key=lambda name: (not os.path.isdir(os.path.join(self.current_dir, name)),
                                              name.lower()))
            for name in names:
                path = os.path.join(self.current_dir, name)
                is_dir = os.path.isdir(path)
                if is_dir or self._matches(name):
                    self._add(name, is_dir, path)
            self.location.SetValue(self.current_dir)
        except OSError as error:
            self.api.speak(f"Could not read folder: {error}")

    def _matches(self, name):
        return any(fnmatch.fnmatch(name.lower(), pattern) for pattern in self.patterns)

    def _add(self, name, is_dir, path):
        index = self.list.GetItemCount()
        self.list.InsertItem(index, name)
        self.list.SetItem(index, 1, "Folder" if is_dir else "File")
        self.items.append((name, is_dir, path))

    def _activate(self, event):
        name, is_dir, path = self.items[event.GetIndex()]
        if is_dir:
            self.current_dir = path
            self.refresh()
        elif self.mode == "open":
            self.result = path
            self.EndModal(wx.ID_OK)
        elif self.filename:
            self.filename.SetValue(name)

    def _go_to_location(self, event):
        value = self.location.GetValue().strip()
        if value.lower() in ("this pc", "computer"):
            self.current_dir = None
        elif value.lower() in ("pyos drive", "py-os drive"):
            self.current_dir = self.vfs_root
        elif os.path.isdir(value):
            self.current_dir = os.path.abspath(value)
        else:
            self.api.speak("Invalid folder.")
            return
        self.refresh()

    def _go_up(self, event):
        if self.current_dir is None:
            return
        if os.path.abspath(self.current_dir) == self.vfs_root:
            self.current_dir = None
        else:
            parent = os.path.dirname(self.current_dir)
            self.current_dir = parent if parent != self.current_dir else None
        self.refresh()

    def _accept(self, event=None):
        if self.mode == "open":
            self.api.speak("Select a file.")
            return
        name = self.filename.GetValue().strip() if self.filename else ""
        if not name:
            self.api.speak("Enter a file name.")
            return
        if self.current_dir is None:
            self.api.speak("Choose a folder first.")
            return
        self.result = os.path.join(self.current_dir, name)
        self.EndModal(wx.ID_OK)


def choose_file(parent, api, mode="open", title=None, wildcard="All files (*.*)|*.*"):
    dialog = PyOSFileDialog(parent, api, mode, title, wildcard)
    try:
        return dialog.result if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
