# PyOS Developer Guide

PyOS is a modular, accessible operating system simulator for the blind. This guide explains how to create your own applications (plugins).

## 1. Getting Started
Create a new file in `apps/` (e.g., `my_app.py`). PyOS automatically discovers and loads any class that inherits from `BlindApp`.

## 2. Basic App Structure
```python
import wx
from api import BlindApp

class MyApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "My Application"
        self.description = "A short description for the screen reader."
        self.help_text = "Press Enter to perform the main action."
        self.docs = "This app demonstrates the basic structure of a PyOS plugin."

    def run(self):
        self.frame = wx.Frame(None, title=self.name, size=(400, 300))
        panel = wx.Panel(self.frame)
        btn = wx.Button(panel, label="Click Me")
        btn.Bind(wx.EVT_BUTTON, self.on_click)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        self.frame.Show()
        self.api.speak("My application is now open.")

    def on_click(self, event):
        self.api.speak("You clicked the button!")
```

## 3. The System API (`self.api`)
| Method | Description |
| :--- | :--- |
| `speak(text, interrupt=True)` | Speaks text via the system's speech engine. |
| `play_sound(sound_type)` | Plays a themed sound (`nav`, `launch`, `close`, `alert`, `startup`, etc.). |
| `get_data_path(filename)` | Returns a path to a file in py-os's data directory. |
| `get_vfs()` | Returns the Virtual File System kernel for PyOS Drive operations. |
| `open_file(parent, title, wildcard)` | Opens the shared PyOS file picker for the PyOS Drive or host files. Returns a path or `None`. |
| `save_file(parent, title, wildcard)` | Opens the shared PyOS save picker for the PyOS Drive or host files. Returns a path or `None`. |
| `notify(title: str, message: str, level: str = 'info')` | Sends a notification to the user. Currently supports spoken notifications. `level` can be 'info', 'warning', or 'error'. |

## 4. Special Features
- **F1 (Help)**: PyOS automatically reads your app's `self.help_text` when the user presses **F1**.
- **Ctrl+D (Docs)**: PyOS reads your app's `self.docs` when the user presses **Ctrl+D**.
- **Sound Themes**: Users can create custom sounds in the **Theme Creator** app. Use `self.api.play_sound()` to stay consistent with the user's chosen theme.

## 5. File System Access
For applications needing to interact with the host file system (e.g., reading/writing files, browsing directories), use Python's built-in `os` module directly. Avoid using `self.api.get_vfs()` for host file system operations.

## 6. Text Editor App
A basic `TextEditorApp` is available for creating and editing text files. It can be launched via the application menu.

## 7. Notifications
Applications can now send notifications using `self.api.notify(title, message, level='info')`. This currently triggers a spoken notification. The `level` parameter can be used to indicate the severity ('info', 'warning', 'error'). Future enhancements may include visual notifications.
