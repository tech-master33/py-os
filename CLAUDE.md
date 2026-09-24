# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands for Development

### Running the Application
```bash
# Run the simulator
python desktop.py

# Run with virtual environment (recommended)
.\venv\Scripts\activate  # Windows
source venv/bin/activate  # macOS/Linux
python desktop.py
```

### Installing Dependencies
```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows: .\venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Running Tests
```bash
# Run all tests
python -m unittest discover tests

# Run specific test file
python -m unittest tests.test_kernel_filenames

# Run tests with verbose output
python -m unittest discover tests -v
```

### Development Workflow
1. Make changes to code
2. Run relevant tests to ensure nothing is broken
3. Test manually by running `python desktop.py` and verifying functionality
4. For UI changes, verify accessibility features work correctly

## Project Architecture

### Core Components
- **desktop.py**: Main application entry point and GUI frame using wxPython
- **kernel.py**: Virtual operating system implementing file system, terminal commands, and shell integration
- **speech.py**: Cross-platform text-to-speech engine supporting NVDA, platform-native APIs, and pyttsx3
- **ai_providers.py**: AI assistant backend supporting Ollama (local), Gemini, OpenRouter, and Groq providers
- **app_paths.py**: Utilities for locating application resources and data directories
- **platform_support.py**: Platform-specific utilities for detecting available backends and commands

### Applications (apps/)
- **assistant.py**: AI assistant interface
- **terminal.py**: Command-line interface within the simulator
- **platform_diagnostics.py**: Reports system capabilities and available backends
- **help_app.py**: Documentation and help system
- **audio_recorder.py**: Voice recording functionality
- **productivity_apps.py**: Calculator, calendar, and other utilities
- **encryption.py**: File encryption tools
- **sound_settings.py**: Audio configuration
- **youtube_player.py**: Media playback
- **text_editor.py**: Basic text editor application (refer to apps/DEVELOPER_GUIDE.md for plugin development)

### Virtual File System
- Located in `vfs/` directory (copied to user data directory on first launch)
- Provides isolated file system for safe practice
- Commands like `list`, `open`, `create`, `delete` operate within this VFS
- Host file access available through special paths or shell integration

### Key Features
- **Accessibility**: Optimized for screen readers with minimal speech queue interference
- **Cross-platform**: Windows (NVDA support), macOS (VoiceOver), Linux (speech-dispatcher/espeak)
- **AI Assistant**: Local and cloud providers with streaming responses and reasoning transparency
- **Terminal Integration**: Host shell access (`shell cmd`, `shell powershell`, etc.) and virtual terminal
- **Keyboard Shortcuts**: Ctrl+T (time), Ctrl+W (location), Enter (execute)

## Important Files and Directories

- `requirements.txt`: Python dependencies
- `tests/`: Unit test suite using Python unittest framework
- `vfs/`: Initial virtual file system content (welcome.txt)
- `ai_config.json`: AI assistant configuration (created in user data directory)
- `nvdaControllerClient64.dll`: Optional NVDA integration component
- `apps/DEVELOPER_GUIDE.md`: Guide for developing PyOS applications/plugins

## Testing Guidelines
- Tests use Python's unittest framework
- Test files are named `test_*.py` and located in the `tests/` directory
- Tests often use temporary directories to isolate file system operations
- Mocking is used for external dependencies when appropriate
- Run tests before and after making changes to ensure regressions are caught

## Accessibility Considerations
When modifying code, especially in desktop.py or speech.py:
- Ensure speech output is interruptible and doesn't queue excessively
- Maintain keyboard navigation compatibility
- Consider screen reader announcements when changing UI elements
- Test with actual screen readers when possible (NVDA on Windows, VoiceOver on macOS)
- Keep speech feedback informative but concise to avoid overwhelming users

## Common Development Tasks

### Adding a New Terminal Command
1. Add command handling in `kernel.py`'s `execute()` method
2. Consider edge cases (missing arguments, invalid inputs)
3. Add corresponding tests in `tests/test_kernel_filenames.py` or new test file
4. Update help text in `apps/help_app.py` if needed

### Adding a New Application (Plugin)
Following the structure in `apps/DEVELOPER_GUIDE.md`:
1. Create new Python file in `apps/` directory
2. Inherit from `BlindApp` (imported from `api`)
3. Implement required properties: `name`, `description`, `help_text`, `docs`
4. Implement `run()` method to create and show the application UI
5. Use `self.api` for system interactions (speech, sounds, file access, notifications)
6. PyOS automatically discovers and loads any class that inherits from `BlindApp`
7. Special features:
   - F1 (Help): PyOS automatically reads your app's `self.help_text`
   - Ctrl+D (Docs): PyOS reads your app's `self.docs`
   - Sound Themes: Use `self.api.play_sound()` to stay consistent with user's chosen theme

### Modifying Speech Functionality
1. Changes to `speech.py` should maintain cross-platform compatibility
2. Test with available backends (pyttsx3 is available on all platforms)
3. Consider interruptibility and queue management
4. Test actual speech output when possible

### AI Provider Changes
1. Modifications to `ai_providers.py` should maintain the provider abstraction
2. Ensure API keys are handled securely (stored in plain text but user-accessible only)
3. Test streaming vs non-streaming responses
4. Verify event reporting (thinking, generating, researching) aligns with actual provider behavior
5. Refer to apps/DEVELOPER_GUIDE.md for detailed instructions on adding providers

## Application Development Reference

Based on `apps/DEVELOPER_GUIDE.md`, key aspects of PyOS application development:

### System API (`self.api`)
- `speak(text, interrupt=True)`: Speaks text via the system's speech engine
- `play_sound(sound_type)`: Plays themed sounds (nav, launch, close, alert, startup, etc.)
- `get_data_path(filename)`: Returns path to file in py-os data directory
- `get_vfs()`: Returns Virtual File System kernel for PyOS Drive operations
- `open_file(parent, title, wildcard)`: Opens shared PyOS file picker
- `save_file(parent, title, wildcard)`: Opens shared PyOS save picker
- `notify(title, message, level='info')`: Sends notification (spoken currently)

### Special Features
- **F1 (Help)**: Automatic reading of app's `help_text`
- **Ctrl+D (Docs)**: Automatic reading of app's `docs`
- **Sound Themes**: Custom sounds in Theme Creator app, use `play_sound()` for consistency

### File System Access
- For PyOS Drive operations: use `self.api.get_vfs()`
- For host file system: use Python's built-in `os` module directly
- Avoid using `self.api.get_vfs()` for host file system operations

### Notifications
Applications can send notifications using `self.api.notify(title, message, level='info')` with levels: 'info', 'warning', 'error'

### AI Integration
The AI Assistant sends a reference document with every question so models can answer accurately about PyOS instead of inventing features. This is assembled by `pyos_knowledge.py`.