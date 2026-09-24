# py-os Simulator

An accessible operating system simulator for blind and visually impaired users.

## Features
- **Cross-platform Speech Engine**: Uses NVDA on Windows when available, macOS `say` on Mac, and Speech Dispatcher, eSpeak, or `pyttsx3` on Linux depending on what is installed.
- **High Contrast GUI**: Built with `wxPython`, optimized for screen readers and low-vision users.
- **Virtual File System**: A safe PyOS drive stored in PyOS's data directory to practice file management.
- **VoiceOver-friendly navigation on macOS**: Focus changes avoid excessive duplicate announcements so VoiceOver can read controls naturally.
- **AI Assistant with four providers**: Ollama on this computer, Google Gemini, OpenRouter, or Groq. The assistant asks which provider to use each time it starts, remembers your choice, and saves any API key locally so you only type it once. Every answer carries a short reference about PyOS itself, so the model knows how the desktop, the apps and the Terminal really work, and the assistant reports what the model is actually doing while it works — thinking, generating, researching, or reading a page — instead of assuming.
- **Live reasoning and web research**: models that think out loud have each finished sentence handed to your screen reader as it arrives, and providers that can search the web can be asked to, with the pages they used listed in the transcript.
- **Platform Diagnostics app**: Reports available speech backends, host shells, file-open helpers, and optional dependencies on the current machine.
- **Keyboard Shortcuts**:
  - `Ctrl + T`: Speak current time.
  - `Ctrl + W`: Speak current location (path).
  - `Enter`: Execute command.

## Commands
- `help`: List available commands.
- `list`: Speak items in the current directory.
- `open <name>`: Open a folder or read a text file.
- `create <name>`: Create a new text file.
- `delete <name>`: Delete a file or empty folder.
- `time`: Speak the current time, to the second.
- `where`: Speak current directory.
- `exit`: Close the simulator.
- `shell <type>`: Open a host shell such as `zsh`, `bash`, `sh`, `cmd`, or `powershell`, depending on your platform.

File names keep the case you type, so `open Report.txt` finds `Report.txt`. Put double quotes
around any name with spaces, for example `open "My Report.txt"`. Text files are read as
UTF-8; a file that is not UTF-8 text is reported instead of being read out as gibberish.

## AI Assistant

The **AI Assistant** app can talk to four providers:

- **Ollama** — local models served by Ollama on this computer. No API key needed.
- **Google Gemini** — Google's Gemini models over the internet. Needs a Gemini API key.
- **OpenRouter** — many models from many companies through one service. Needs an OpenRouter API key.
- **Groq** — fast open models, including Compound systems that search the web. Needs a Groq API key.

Every time the assistant starts it asks which provider you want to use. Pick one and
press Continue:

- Ollama starts right away.
- Gemini, OpenRouter and Groq ask for an API key the first time you choose them. The key
  is checked against the provider, then saved, so next time you can pick the provider and
  start asking questions straight away.

Each provider keeps its own model, its own API key and its own web research setting. The
**Provider...** button in the assistant switches provider or replaces a key mid-session
without restarting, and **Clear Saved Key** in the picker forgets a stored key. Models are
listed from the provider itself, so you are always shown what your key can actually use.

The settings live in `ai_config.json` in the PyOS data folder (`~/.py-os/` by default,
or `PY_OS_DATA_DIR` if you changed it). The last provider and model you used are stored
there too.

### Hearing what the model is doing

Pressing Enter says **"Waiting for Ollama..."** and nothing more, and every other status
comes from the model itself rather than being guessed at:

- **Thinking...** when the model really starts reasoning.
- **Generating response...** when the answer text starts arriving.
- **Researching...** when the provider reports that it searched, with the search query
  written to the transcript.
- **Visiting website...** when the provider reports that it read a page.

A model that never reasons never says "Thinking", and a question that fails — a rejected
key, no connection — is reported on its own without a misleading status before it. A local
Ollama model cannot search the web, so it never claims to.

Model reasoning is written to the conversation as **Thinking** lines as it arrives, so it
can be read as it comes and read back afterwards. The **Narrate reasoning** box stops the
thinking being read out without hiding the text.

### Remembering the conversation

The assistant remembers what has been said while it is open, so follow-up questions work:
ask "what does `list` do?" and then "and `open`?", and the second question is sent with the
first exchange attached. Each provider has its own memory, and switching provider starts
fresh rather than showing one model another model's answers.

Memory lives in memory only. It is never written to disk, and it is forgotten when the
assistant closes, when you switch provider, or when you press **Clear conversation** — which
reports how many exchanges are remembered when you focus it. A local model keeps less of it
than a hosted one, because the reference, the conversation and the answer all have to fit in
the same context window; if the oldest part is dropped to make room, the assistant says so.

### Text the model writes

Answers are shown exactly as the model wrote them, including accents and emoji. Two things
are still put right on the way in. If a streamed reply was decoded as the wrong codepage,
the mangling is repaired rather than displayed or read out (`cafÃ©` becomes `café`, an em dash
becomes a dash). And an emoji half, a control character or any other mark a voice cannot
pronounce is left out of the spoken form only, with a short note in the transcript — so what
you hear never turns into rows of question marks.

### Nothing talks over what you are reading

PyOS keeps no speech queue and does no pacing of its own. It hands each thing over the
moment it exists, and it never cuts into what is already being read, so the screen reader's
own queue does the rest: status words, the model's thinking and the finished answer are
read in the order they arrive, at your own speed, and the reader keeps going after the
model has stopped. Reasoning goes over one sentence at a time, as soon as each sentence is
complete — there is no batching, no waiting and no "thinking" delay anywhere in between.

A computer with no screen reader has no queue to hand that thinking to, and reading it
fragment by fragment would be worse than not hearing it, so there the thinking stays as
text in the transcript and only the finished answer is spoken.

### Web research

The **Web research** box, when the chosen model supports it, asks the provider to search
the web as well as answer from its own knowledge. Gemini searches Google and can read
pages, OpenRouter adds its web plugin, and Groq needs a Compound model such as
`groq/compound` (or a GPT OSS model). Searching costs extra money on the hosted providers,
and a searched answer takes longer.

Both boxes tell you their state before anything else: focusing one begins with "Web
research, currently on" or "currently off", and turning one on or off says the new state
straight away — "Web research, currently on. Gemini is able to search Google and read the
pages you find." The box always shows the setting that is really stored. A model that cannot
research has its box greyed out with the reason given, and if a setting is left on for a
model that cannot use it, the assistant says exactly that rather than quietly ignoring it or
showing something different. Switching to a model that cannot research does not undo your
choice; it tells you the research will not be used until you pick a model that can.

Each question tells the model who it is and whether research is on for that question, so a
model asked to look something up either searches or says plainly that research is switched
off — rather than suggesting you go and check which providers exist. If you ask a model to
research while the box is off, the assistant says so before sending the question. And if
research was on but the model never reported a search or a page, the assistant says that
too, instead of leaving you to assume it looked anything up.

Pages the model actually used are counted out loud after the answer and listed in the
transcript under **Sources**, with their titles and addresses.

**Escape** stops an answer that is being written and keeps the part that already arrived.
It is the one thing the assistant does that interrupts speech, and only because you asked
for it.

### What the AI knows about PyOS

Every question is sent together with a short reference about PyOS itself, so the
model knows how this simulator actually works: the desktop and its keyboard
shortcuts, the apps installed on this copy, the Terminal commands, and how to
write an app. Without it a model would guess at menus and commands that do not
exist.

Press **Knowledge...** in the assistant to hear that reference section by section.
**Speak All** reads the whole thing, and **Copy All** puts it on the clipboard so you
can paste it into another AI tool. The reference is rebuilt every time the assistant
starts, which means the apps and host shells described in it always match this
computer. Its size is trimmed automatically for a provider with a smaller model, and
the dialog says so when that happens.

The dialog also shows the second half of what is sent: a short section called **You, right
now** that states which provider and model is answering, whether web research is on for the
next question, what the model can use, and that it is the model being talked to rather than a
list of options. It changes with your settings, so what you read there is what the model
receives.

### About your API keys

API keys are stored in **plain text** in that file, so anyone who can read it can use
your key. That keeps the file easy to back up, inspect, or delete with any text editor.
On Windows and macOS the data folder is inside your user profile, which is only readable
by you. Get a key from:

- Gemini: <https://aistudio.google.com/app/apikey>
- OpenRouter: <https://openrouter.ai/keys>
- Groq: <https://console.groq.com/keys>

## macOS Notes

- On macOS, py-os uses the built-in `say` command for spoken feedback.
- The desktop reduces automatic focus chatter on macOS so VoiceOver can announce buttons and controls more clearly.
- File Explorer uses the native `open` command to launch files with their default Mac app.
- By default, app data and the PyOS drive are stored in `~/.py-os/`. Set `PY_OS_DATA_DIR` if you want them elsewhere.

## File Storage

File Explorer opens to **This PC**, with separate locations for the **PyOS Drive** and
**Host Files**. The PyOS Drive is the simulator's own filesystem and is shared with
Terminal. Host Files provides access to normal files on the computer. Existing files
from the legacy repository `vfs` folder are copied to the PyOS data directory on first
launch.

## Paths and file encoding

PyOS finds its own program files, the `music` folder and the NVDA DLL from the folder the
simulator is installed in, never from the working directory it happens to be started from.
Shortcuts, scripts and the update wizard can therefore launch it from anywhere without
losing speech, sounds or apps.

All configuration files are written as UTF-8 JSON, so names with accents or symbols survive
being saved and read back on any platform.

## Support Matrix

- Windows: Best with `wxPython`, optional NVDA Controller DLL, and optional `sounddevice` plus `soundfile` for recording.
- macOS: Best with `wxPython`; speech works with built-in `say`, and recording works when `sounddevice` plus `soundfile` are installed.
- Linux: Best with `wxPython`; speech can use `spd-say`, `espeak-ng`, `espeak`, or `pyttsx3`, depending on what is installed.

Open the `Platform Diagnostics` app after launch to see the exact support level on the current machine.

## NVDA Integration (Optional)
To enable direct NVDA support:
1. Download `nvdaControllerClient64.dll` (for 64-bit Python) or `nvdaControllerClient32.dll` (for 32-bit Python) from the [NVDA GitHub Repository](https://github.com/nvaccess/nvda/tree/master/extras/controllerClient).
2. Place the DLL in the same folder as `desktop.py`.

## Installation Guide for GitHub (Manual)

1.  **Clone the repository:**
    First, clone the proj
    ```bash
    git clone https://github.com/tech-master33/py-os
    cd py-os 
    ```

2.  **Set up a virtual environment (Recommended):**
    Using a virtual environment is highly recommended to manage project dependencies without conflicts.
    ```bash
    # Create a virtual environment (e.g., named 'venv')
    python -m venv venv
    
    # Activate the virtual environment:
    # On Windows:
    .\venv\Scripts\activate
    # On macOS/Linux:
    # source venv/bin/activate
    ```

3.  **Install FFmpeg (Optional but recommended):**
    The system can use FFmpeg tools such as `ffplay` for audio playback fallbacks.
    ```bash
    # Windows
    winget install ffmpeg

    # macOS
    brew install ffmpeg

    # Linux (Debian/Ubuntu example)
    sudo apt install ffmpeg
    ```

4.  **Install dependencies:**
    Install all required Python packages using the `requirements.txt` file.
    ```bash
    pip install -r requirements.txt
    ```

5.  **NVDA Controller Client DLL (for direct NVDA integration):**
    If you intend to use the direct NVDA integration feature, follow these steps:
    a. Download the appropriate DLL file: `nvdaControllerClient64.dll` (for 64-bit Python) or `nvdaControllerClient32.dll` (for 32-bit Python) from the [NVDA GitHub Repository extras page](https://github.com/nvaccess/nvda/tree/master/extras/controllerClient).
    b. Copy the downloaded DLL file into the main project directory (the same folder where `desktop.py` is located).

5.  **Run the application:**
    Execute the main application script to launch the simulator.
    ```bash
    python desktop.py
    ```

---
