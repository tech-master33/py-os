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

## 6. AI Providers

The **AI Assistant** app (`.assistant.py`) talks to providers through `ai_providers.py`, which is importable without wxPython so it can be unit tested headless. To add a fourth provider:

1. Subclass `AIProvider` in `ai_providers.py` and set `key`, `label`, `description`, `requires_api_key`, and `default_model`.
2. Implement `list_models()` to return model names and `ask(prompt, model)` to return one reply as text. Implement `verify_key(api_key)` when the provider needs a key, and raise `ProviderError` with a kind of `no_key`, `bad_key`, `offline`, or `http` for anything the user should hear about.
3. Add the class to `PROVIDER_CLASSES` and to `PROVIDER_ORDER` so it appears in the picker.

The picker dialog and the API-key dialog live in `ai_provider_dialog.py`. Chosen provider, per-provider models, and API keys are stored in `ai_config.json` via the helpers in `ai_providers.py` (keys are plain text on purpose, so users can inspect or delete them).

## 7. Text Editor App
A basic `TextEditorApp` is available for creating and editing text files. It can be launched via the application menu.

## 8. Notifications
Applications can now send notifications using `self.api.notify(title, message, level='info')`. This currently triggers a spoken notification. The `level` parameter can be used to indicate the severity ('info', 'warning', 'error'). Future enhancements may include visual notifications.

## 9. Teaching the AI about PyOS

The **AI Assistant** sends a reference document with every question, so a model can answer
accurately about the desktop, the apps and the Terminal instead of inventing features.
It is assembled by `pyos_knowledge.py`, which is importable without wxPython.

- Curated prose lives in that module as section constants. Each section registers a key, a
  title, a priority and whether it is essential.
- Live facts are read from the running system: the data folder from `app_paths`, the
  installed app list from the desktop, and the host shells from `platform_support.get_shells()`.
- `build_knowledge(apps=..., shells=..., include=..., max_chars=...)` returns a `KnowledgePack`
  with `.text`, `.sections`, `.dropped`, `.fingerprint`, `.char_count` and `.summary()`.

Providers receive the reference through `AIProvider.ask(prompt, model, system=...)`, and each
one places it in its own field: Ollama's `system`, Gemini's `systemInstruction`, and a system
message for OpenRouter. `AIProvider.system_char_limit` is the per-provider budget, so if the
reference grows past it the lowest-priority non-essential sections are dropped rather than
overrunning a small local model, and the Knowledge dialog reports what was left out.

If you add, rename or remove an app or a Terminal command, update the matching section in
`pyos_knowledge.py`. The installed-app list looks after itself, but the curated prose does not,
and a model told about an app that no longer exists will happily describe it.
`tests/test_pyos_knowledge.py` checks the Terminal commands against the kernel's own help
output, so a new command fails the suite until the reference mentions it.
