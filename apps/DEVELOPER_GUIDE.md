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

The **AI Assistant** app (`apps/assistant.py`) talks to providers through `ai_providers.py`, which is importable without wxPython so it can be unit tested headless. To add another provider:

1. Subclass `AIProvider` in `ai_providers.py` and set `key`, `label`, `description`, `requires_api_key`, and `default_model`. Set `supports_web` when the provider can search the web, and override `can_research(model)` when only some of its models can.
2. Implement `list_models()` to return model names and `ask(prompt, model, system=None, web=False, history=None)` to return one reply as text. Implement `verify_key(api_key)` when the provider needs a key, and raise `ProviderError` with a kind of `no_key`, `bad_key`, `offline`, or `http` for anything the user should hear about. Give it a `history_char_limit` and a `web_tool_summary(model)` phrase, and pass `history` through to the request in the provider's own shape.
3. Implement `ask_stream(...)` to report what happens while the question is being answered, using the contract below. The base class already provides a working fallback that asks once and reports a single content event, so a provider can ship without streaming and gain it later.
4. Add the class to `PROVIDER_CLASSES` and to `PROVIDER_ORDER` so it appears in the picker. The picker's note and greeting are built from the provider list, so neither needs editing.

### The streaming contract

`ask_stream(prompt, model, system=None, on_event=None, cancel=None, web=False, history=None)` returns the finished reply and calls `on_event` with `AIEvent` objects as things happen:

- `status` — one of `Thinking`, `Generating response`, `Researching`, `Visiting website`. **A provider must only announce a phase it can actually observe.** Ollama learns "thinking" by receiving reasoning text; Gemini reads thought-flagged parts, `groundingMetadata` and `urlContextMetadata` out of its stream; Groq reads the built-in tools it really executed. A provider that observes nothing emits nothing, which is why a local Ollama model never reports research. The one deliberate exception is OpenRouter's web plugin, which always searches and reports nothing live, so `research_is_request_driven` announces that request's search up front.
- `reasoning` and `content` — text as it arrives. Split anything that arrives tagged `<think>...</think>` (some providers force reasoning back into the answer body) with `ThinkingTagSplitter` so it is narrated as reasoning rather than read out as the answer.
- `source` — a page the model really used: title in `text`, address in `detail`.

`cancel` is a `threading.Event`. When it is set, close the stream, keep whatever text arrived, and return it; the assistant keeps the partial answer and says it stopped. Cancellation is checked between chunks, so a model that has gone quiet may take up to the read timeout to actually stop. If a stream fails before emitting anything, the assistant retries once through the plain `ask()`; a failure after text arrived is never retried, so the user is not billed twice.

OpenRouter and Groq both speak OpenAI's chat-completions dialect, so `OpenAICompatibleProvider` implements the request and the server-sent-event reader once and each subclass supplies its address, headers, wording and payload extras. Gemini streams `:streamGenerateContent?alt=sse` (retrying once without `thinkingConfig` if a model refuses thought summaries) and Ollama streams NDJSON from `/api/chat`, falling back once to `/api/generate` — without the conversation — if the installed Ollama is too old for `/api/chat`, and setting `memory_note` so the assistant can explain why.

### Reading the bytes of a stream

`_iter_raw_lines` decodes the stream itself rather than letting `requests` choose. A `text/event-stream` response almost never carries a charset, and `requests` answers a charset-less `text/*` header by decoding it as ISO-8859-1, which turns every accented word, dash and emoji into mojibake. The bytes are pushed through an incremental UTF-8 decoder instead, which also keeps a multi-byte character whole across a chunk boundary, and lines are split on the newline only — never with `str.splitlines`, which treats a byte such as `0x85` inside a character as a line break and used to cut a JSON payload in half.

`_iter_sse` holds a payload that does not parse and retries it against the next `data:` line, both joined directly and joined with the newline the event-stream format specifies, so a JSON object split across several lines is recovered. Only a payload that still will not parse is set aside, and it is yielded as `{"_unreadable": ...}` rather than dropped, so a broken stream is visible in tests and logs instead of silently losing text. `MAX_UNPARSED_PAYLOAD` bounds how much can be held.

### Conversation memory

`normalize_history(history)` accepts `{"role", "text"}` dictionaries or `(role, text)` pairs and returns clean `(role, text)` pairs, dropping unknown roles and empty turns. `trim_history(history, max_chars)` keeps the newest turns that fit and never leaves a reply whose question was dropped, returning `(kept, dropped)` so the assistant can say when it has forgotten the earliest part.

Each provider maps that list into its own shape: Ollama's `messages`, Gemini's `contents` with `user`/`model` roles (`_contents` mends the alternation Gemini insists on, and joins the new question onto a trailing user turn), and the OpenAI dialect's `messages` with `system`, `user` and `assistant` roles. `history_char_limit` is per provider and much smaller for Ollama, because the reference, the conversation and the answer share one context window.

### Text a window and a voice can carry

`text_integrity.py` is importable without wxPython and has three jobs. `repair_mojibake` undoes UTF-8 text that was read as Latin-1 or cp1252, keeping the repair only when the round trip is unambiguous. `for_display` joins split surrogate pairs, drops lone surrogates and control characters and collapses replacement runs, so the transcript keeps what the model wrote. `for_speech` removes the categories no voice can pronounce and tidies the spacing, and `SpeechEngine.speak` calls it before anything reaches a voice, so no caller can leak a character that would come out as a question mark.

Hosted web tools are only sent when the user has switched research on: Gemini gets `google_search` and `url_context`, OpenRouter gets its `web` plugin, and Groq gets `compound_custom.tools.enabled_tools`, or a `browser_search` tool for its GPT OSS models.

The picker dialog and the API-key dialog live in `ai_provider_dialog.py`. Chosen provider, per-provider models and API keys are stored in `ai_config.json` via the helpers in `ai_providers.py` (keys are plain text on purpose, so users can inspect or delete them). `get_web` and `set_web` remember web research per provider, and `get_speak_reasoning` and `set_speak_reasoning` remember whether reasoning is narrated.

### Reading text out without a queue

The assistant owns no speech queue, no timer and no speech rate of its own. Text goes to the window as it arrives — `_EventBuffer` hands each event straight over from the provider thread, with `apply_events` doing the writing on the main thread — and speech is handed to whatever is already reading:

- **Automatic speech never interrupts.** Status words, the finished answer and failures are spoken with `interrupt=False`, so they wait in the reader's queue rather than cutting off the sentence being read out. `show_response(..., interrupt=True)` is the default, and callers that arrive while an answer is still being read pass `False`.
- **Thinking goes over a sentence at a time**, split by `_complete_sentences` and spoken the moment each sentence is complete. The screen reader's own queue paces them, and it keeps reading after the model has stopped. There is no batching or waiting in between.
- **Only a screen reader is given the thinking.** `SpeechEngine.uses_screen_reader()` reports whether the current voice belongs to one (NVDA on Windows, the system voice on macOS). On a machine whose only voice is PyOS's own engine, thinking would come out fragment by fragment, so it stays as text in the transcript and only the finished answer is spoken. `AssistantApp._voice_reasoning()` is that check, and it is what the **Narrate reasoning** box turns off.

Everything that reaches the window goes through `AssistantApp._clean`, which repairs text and counts the pieces that needed repairing, so the assistant can say once per answer that something could not be shown as written. Every utterance goes through `for_speech` inside `SpeechEngine.speak`.

The two capability boxes are deliberately state-first: `on_web_focus`, `on_reasoning_focus`, `_web_state_speech` and `_reasoning_state_speech` all begin with whether the thing is on, because that is the question a screen reader user is asking when focus lands. A click is also the only thing that changes the stored setting — `_update_capability_controls` greys a box out or enables it but never changes its value — which is what stopped a model list arriving seconds later from quietly undoing a click.

Escape is the only deliberate interruption, and the only interruption at all. `tests/test_assistant_flow.py` covers this with a fake API that records `(text, interrupt)` for every utterance and a fake engine whose `screen_reader` flag can be flipped inside a test; every AI test module calls `tests/netguard.py` from `setUpModule`, so a stubbed provider can never reach the real network.

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

Providers receive the reference through `AIProvider.ask(prompt, model, system=...)` and
`ask_stream(...)`, and each one places it in its own field: the first message for Ollama,
Gemini's `systemInstruction`, and a system message for OpenRouter and Groq.
`AIProvider.system_char_limit` is the per-provider budget, so if the
reference grows past it the lowest-priority non-essential sections are dropped rather than
overrunning a small local model, and the Knowledge dialog reports what was left out.

Two documents travel together, and they answer different questions. `pyos_knowledge.py` is
the facts about PyOS and changes when PyOS changes. `ai_capability.py` is *how to behave on
this question* — who the model is, whether research is switched on right now, which tools
that means for this provider, and that it is the model being spoken to rather than a
catalogue of providers. That block is rebuilt per request, because the answer changes with
your settings, and it is the reason a model asked to research searches instead of answering
with advice about checking providers. `capability_block(provider, model, web_on,
remembered_turns)` treats `web_on` as a request *and* a capability: a model that cannot
research is told research is off however the caller called it, because promising a tool
that was not sent is the one thing this block must never do.

The two share one system budget, so `AssistantApp._build_knowledge` measures the room the
longest possible block could need — research on, with memory in play — and holds it back
when building the pack. `tests/test_pyos_knowledge.py` checks that the reference and the
block still fit every provider together; if you grow either one, that test is what tells you
before a provider starts dropping sections. `KnowledgeViewerDialog` is handed the pack plus
the live block, so the dialog shows what is really sent rather than an older snapshot.

If you add, rename or remove an app or a Terminal command, update the matching section in
`pyos_knowledge.py`. The installed-app list looks after itself, but the curated prose does not,
and a model told about an app that no longer exists will happily describe it.
`tests/test_pyos_knowledge.py` checks the Terminal commands against the kernel's own help
output, so a new command fails the suite until the reference mentions it.
