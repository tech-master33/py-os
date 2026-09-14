"""What the AI Assistant knows about PyOS.

Ollama, Gemini and OpenRouter cannot be taught PyOS by training: the two hosted
providers are closed-weight, and a local fine-tune would need a dataset, a GPU
and a fresh run on every commit. So the assistant is *grounded* instead. This
module builds one curated reference document that is attached to every question
as the provider's own system instruction.

The pack is a mix of hand-written prose (concepts, the API table, conventions)
and facts read from the running system (the installed app list, the data folder,
the available host shells), so the parts that change most often cannot drift out
of date.

Like :mod:`ai_providers`, this module deliberately avoids importing wx so it can
be imported and tested without a GUI, and it lives at the repository root rather
than in ``apps/`` so the plugin loader does not treat it as an application.
"""

import hashlib

from app_paths import get_data_dir

KNOWLEDGE_TITLE = "PYOS REFERENCE"

# The finished pack is roughly 9,000 characters, which is about 2,300 tokens.
# Each provider carries its own budget in ``AIProvider.system_char_limit``; pass
# it to :func:`build_knowledge` as ``max_chars`` and the least important
# sections are dropped rather than overflowing a small context window.

DEFAULT_INCLUDE = (
    "overview",
    "desktop_and_keys",
    "apps",
    "terminal",
    "writing_apps",
    "ai_assistant",
)

PREAMBLE = (
    "You are the AI Assistant built into PyOS, an accessible operating system "
    "simulator for blind and visually impaired users. The reference below is the "
    "authoritative description of this simulator.\n"
    "Answer questions about PyOS from it, and never invent app names, menu items, "
    "shortcuts or commands. If the reference does not cover something, say so "
    "plainly and point the user at the Help Center app, where F1 reads help and "
    "Ctrl+D reads full documentation, or at the Platform Diagnostics app for "
    "questions about this machine.\n"
    "Answers are read aloud by a screen reader, so keep them short, plain and "
    "free of tables or long lists of punctuation.\n"
    "If the question is not about PyOS, just answer it normally."
)

_OVERVIEW = """PyOS is a Windows, macOS and Linux desktop program that simulates an operating system, written in Python and built with wxPython for a keyboard-driven, high-contrast interface.

PyOS is a simulator, not a real operating system. Shutting down or restarting the host computer is disabled on purpose, and PyOS never changes settings on the host.

PyOS deals with two kinds of files. The PyOS Drive is a sandboxed folder inside the PyOS data folder, shared by File Explorer and Terminal, so a user can practise file management safely. Host Files are the computer's real files, which File Explorer can browse and open.

{data_folder_line} config.json holds settings such as enhanced_mode, ai_config.json holds the AI provider choice and API keys, speech_config.json holds speech settings, music_config.json holds music settings, and the vfs folder is the PyOS Drive. Files from the vfs folder in the original repository are copied there on first launch."""

_DESKTOP_AND_KEYS = """The desktop is a single column of app buttons. Tab moves between them, and focusing an app speaks its name and a one-line description. Enter launches the focused app, and when an app closes, focus returns to its button on the desktop.

Keyboard shortcuts:
- F1: speak help for the app that is open.
- Ctrl+D: speak the full documentation written by that app's author.
- Ctrl+T: speak the current time.
- Ctrl+W: speak the current location, or path.
- Enter: run the typed command in Terminal, or activate the focused control.
- Backspace in File Explorer: go up one folder level.
- Alt+Left in File Explorer: go back in history.

Every app must be usable without a mouse. Controls are announced when focus reaches them, so there are deliberately few hidden shortcuts; a screen reader hears each control in turn.

Settings can switch enhanced mode on with the key enhanced_mode in config.json. Enhanced mode suppresses the extra focus chatter PyOS would otherwise speak, so a screen reader such as NVDA or VoiceOver announces controls in its own words instead of the user hearing everything twice."""

_APPS_INTRO = """PyOS discovers the apps in its apps folder at startup and lists every one on the desktop, so the exact catalogue depends on the installation. Apps that ship with PyOS include:
- Terminal: the command line for the PyOS Drive.
- File Explorer: browses This PC, with the PyOS Drive and Host Files.
- Text Editor: opens, edits and saves text files.
- Settings, Theme Creator and Sound Settings: speech mode, sound themes and volumes.
- Clock, Calculator, Timer, Reminders and Stopwatch.
- Messages: spoken messages supported by the message service.
- Audio Recorder: records and plays back audio when sounddevice and soundfile are installed.
- Encryption, YouTube Player and Music Player.
- Platform Diagnostics: which speech backends, host shells, and optional parts are available on this machine.
- Help Center: user and developer guides, read aloud.
- AI Assistant: this chat window."""

_TERMINAL_INTRO = """Terminal talks to the kernel that manages the PyOS Drive. Type a command and press Enter; the result is spoken and also written to the window. Commands are not case sensitive.

- help: list the available commands.
- list: speak the items in the current folder.
- open <name>: open a folder, or read a text file aloud.
- create <name>: create a new text file with placeholder text in it.
- delete <name>: delete a file, or an empty folder.
- where: speak the current folder path.
- time: speak the current time.
- shell <type>: start a host shell, then type exit inside it to return to PyOS.
- exit: close the Terminal.

shutdown and reboot are recognised commands, but both refuse on purpose to protect the host computer. The host shells available on this computer are: {shells}."""

_WRITING_APPS = """A PyOS app is a single Python file in the apps folder. PyOS scans that folder at startup and loads every class that inherits from BlindApp in api.py, so no registration step is needed. A minimal app looks like this:

import wx
from api import BlindApp

class MyApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "My App"
        self.description = "One line read out on the desktop."
        self.category = "Tools"
        self.help_text = "What F1 should say."
        self.docs = "What Ctrl+D should say."

    def run(self):
        self.frame = wx.Frame(None, title=self.name, size=(400, 300))
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        self.frame.Show()
        self.api.speak("My app is open.")

An app reaches the rest of PyOS through self.api, the system API:
- speak(text, interrupt=True): say something through the speech engine.
- play_sound(name): play a themed sound such as nav, launch, close, alert or startup.
- get_data_path(filename): a path inside the PyOS data folder.
- get_vfs(): the virtual filesystem kernel behind the PyOS Drive.
- open_file(parent, title, wildcard) and save_file(parent, title, wildcard): the shared PyOS file pickers, which return a path or None.
- notify(title, message, level='info'): send a spoken notification, where level is info, warning or error.
- launch_app(class_name): start another app by its class name.
- is_enhanced_mode() and set_enhanced_mode(enabled): read or change enhanced mode.
- get_support_report() and format_support_report(): what this machine can do.

Conventions worth following: bind wx.EVT_CLOSE to the app's on_close and call super().on_close(event) so focus returns to the desktop; speak something whenever focus lands on a control; call play_sound rather than playing audio directly so the user's chosen sound theme applies; use open_file and save_file for both the PyOS Drive and host files; and use Python's own os module for host file work rather than get_vfs, which manages the PyOS Drive only.

The full reference is in apps/DEVELOPER_GUIDE.md, which the Help Center reads aloud."""

_AI_ASSISTANT = """The AI Assistant app asks which provider to use every time it starts, then remembers the choice.
- Ollama: models running on this computer at localhost:11434. No API key is needed.
- Google Gemini: Google's Gemini models over the internet. Needs a Gemini API key.
- OpenRouter: many models from many companies through one service. Needs an OpenRouter API key.

Each provider keeps its own model and its own API key. The chosen provider, the models and the keys live in ai_config.json in the PyOS data folder, and the keys are stored as plain text on purpose, so they can be inspected, backed up or deleted in any text editor. The Provider button in the assistant switches provider or replaces a key without restarting, and Clear Saved Key in the picker forgets a stored key. Models are listed from the provider itself, so Gemini and OpenRouter only ever offer models the user's key can actually use."""


class KnowledgeSection:
    """One titled block of the reference, so it can be read out on its own."""

    def __init__(self, key, title, text, priority=0, essential=False):
        self.key = key
        self.title = title
        self.text = text.strip()
        self.priority = priority
        self.essential = essential

    def rendered(self):
        return f"== {self.title} ==\n{self.text}"

    def __repr__(self):
        return f"<KnowledgeSection {self.key} ({len(self.text)} chars)>"


class KnowledgePack:
    """The finished reference plus what was left out of it."""

    def __init__(self, text, sections, dropped=(), truncated=False):
        self.text = text
        self.sections = list(sections)
        self.dropped = list(dropped)
        self.truncated = truncated
        self.fingerprint = knowledge_fingerprint(text)

    @property
    def char_count(self):
        return len(self.text)

    @property
    def titles(self):
        return [section.title for section in self.sections]

    def section(self, key):
        for section in self.sections:
            if section.key == key:
                return section
        return None

    def summary(self):
        """One spoken sentence describing the pack."""
        summary = (
            f"PyOS knowledge, revision {self.fingerprint}. "
            f"{len(self.sections)} sections, {self.char_count} characters."
        )
        if self.dropped:
            summary += f" Left out to fit this model: {', '.join(self.dropped)}."
        if self.truncated:
            summary += " The reference was shortened to fit this model."
        return summary

    def __str__(self):
        return self.text

    def __repr__(self):
        return (
            f"<KnowledgePack {self.fingerprint} "
            f"{len(self.sections)} sections, {self.char_count} chars>"
        )


def knowledge_fingerprint(text):
    """Return a short stable revision id for a rendered pack."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _data_folder_line():
    return (
        f"The PyOS data folder on this computer is {get_data_dir()}, or the folder "
        "named by the PY_OS_DATA_DIR environment variable."
    )


def _app_field(app, field, default=""):
    """Read one field from an app object, dict or tuple."""
    if isinstance(app, dict):
        value = app.get(field, default)
    elif isinstance(app, (tuple, list)):
        index = {"name": 0, "description": 1, "category": 2}.get(field)
        value = app[index] if index is not None and len(app) > index else default
    else:
        value = getattr(app, field, default)
    return str(value or "").strip()


def _app_lines(apps):
    """Render live app metadata as readable lines."""
    lines = []
    for app in apps or ():
        name = _app_field(app, "name")
        if not name:
            continue
        line = f"- {name}"
        description = _app_field(app, "description")
        if description:
            line += f" — {description}"
        category = _app_field(app, "category")
        if category:
            line += f" [{category}]"
        lines.append(line)
    return lines


def _detect_shells():
    """Ask the platform layer which host shells exist, tolerating failures."""
    try:
        from platform_support import get_shells

        return sorted(str(name) for name in get_shells())
    except Exception:
        return []


def _shell_names(shells):
    if shells is None:
        return _detect_shells()
    if isinstance(shells, dict):
        return sorted(str(name) for name in shells)
    return [str(name) for name in shells]


def _terminal_text(shells):
    names = _shell_names(shells)
    return _TERMINAL_INTRO.format(shells=", ".join(names) if names else "none found")


def _apps_text(apps):
    text = _APPS_INTRO
    live = _app_lines(apps)
    if live:
        text += "\n\nApps installed in this copy of PyOS right now:\n" + "\n".join(live)
    return text


def knowledge_sections(apps=None, shells=None, include=None):
    """Build the reference sections, most important first.

    ``apps`` is the metadata of the apps that are actually installed, used to
    answer "what apps do I have?" accurately. When it is missing or empty the
    curated catalogue stands on its own. ``include`` limits the section keys
    that are built at all.
    """
    wanted = tuple(include) if include is not None else DEFAULT_INCLUDE
    builders = {
        "overview": ("What PyOS is", lambda: _OVERVIEW.format(data_folder_line=_data_folder_line()), 100, True),
        "desktop_and_keys": ("The desktop and the keyboard", lambda: _DESKTOP_AND_KEYS, 90, True),
        "apps": ("The apps that ship with PyOS", lambda: _apps_text(apps), 80, False),
        "terminal": ("Terminal commands", lambda: _terminal_text(shells), 70, False),
        "writing_apps": ("Writing an app for PyOS", lambda: _WRITING_APPS, 60, False),
        "ai_assistant": ("The AI Assistant itself", lambda: _AI_ASSISTANT, 50, False),
    }

    sections = []
    for key in DEFAULT_INCLUDE:
        if key not in wanted or key not in builders:
            continue
        title, build, priority, essential = builders[key]
        sections.append(KnowledgeSection(key, title, build(), priority, essential))
    return sections


def render_knowledge(sections):
    """Render sections, starting with the standing instructions, as plain text."""
    blocks = [KNOWLEDGE_TITLE + "\n" + "=" * len(KNOWLEDGE_TITLE)]
    blocks.append(PREAMBLE)
    for section in sections:
        blocks.append(section.rendered())
    return "\n\n".join(blocks).strip() + "\n"


def fit_knowledge(sections, max_chars):
    """Drop the least important sections until the pack fits the budget.

    Returns ``(kept_sections, dropped_titles, truncated)``. Essential sections
    are never dropped, but a very small budget can still force the rendered text
    to be cut back on a line boundary.
    """
    kept = list(sections)
    dropped = []
    if not max_chars or len(render_knowledge(kept)) <= max_chars:
        return kept, dropped, False

    droppable = sorted(
        (section for section in kept if not section.essential),
        key=lambda section: section.priority,
    )
    for section in droppable:
        kept.remove(section)
        dropped.append(section.title)
        if len(render_knowledge(kept)) <= max_chars:
            return kept, dropped, False

    return kept, dropped, True


def build_knowledge(apps=None, shells=None, include=None, max_chars=None):
    """Return the :class:`KnowledgePack` that is sent to a provider."""
    sections = knowledge_sections(apps=apps, shells=shells, include=include)
    kept, dropped, truncated = fit_knowledge(sections, max_chars)
    text = render_knowledge(kept)
    if truncated and max_chars:
        # Keep whole lines only, so the reference never ends mid-sentence.
        text = text[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n"
    return KnowledgePack(text, kept, dropped, truncated)
