import wx
import os
import json
import importlib.util
import speech
import kernel
import sounds
import threading
import traceback
import sys
import time
from api import SystemAPI
from app_paths import get_data_dir
import platform_support
from file_dialogs import choose_file

class RecoveryConsole(wx.Dialog):
    def __init__(self, parent, missing_files):
        super().__init__(parent, title="PyOS Recovery Console", size=(600, 400))
        self.missing_files = missing_files
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.output = wx.TextCtrl(self.panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH)
        self.output.SetBackgroundColour(wx.Colour(0, 0, 0))
        self.output.SetForegroundColour(wx.Colour(255, 255, 255))
        self.output.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.sizer.Add(self.output, 1, wx.EXPAND | wx.ALL, 5)
        
        self.input = wx.TextCtrl(self.panel, style=wx.TE_PROCESS_ENTER)
        self.input.SetBackgroundColour(wx.Colour(0, 0, 0))
        self.input.SetForegroundColour(wx.Colour(255, 255, 255))
        self.input.Bind(wx.EVT_TEXT_ENTER, self.on_command)
        self.sizer.Add(self.input, 0, wx.EXPAND | wx.ALL, 5)
        
        self.panel.SetSizer(self.sizer)
        self.write_line("PyOS Recovery Console")
        self.write_line("Type 'HELP' for a list of commands.")
        self.write_line("")
        self.input.SetFocus()
        
        from speech import engine
        engine.speak("Recovery Console started. Type commands to repair your system.")

    def write_line(self, text):
        self.output.AppendText(text + "\n")

    def on_command(self, event):
        cmd_full = self.input.GetValue().strip().lower()
        self.input.Clear()
        if not cmd_full: return
        
        self.write_line(f"> {cmd_full}")
        parts = cmd_full.split()
        cmd = parts[0]
        args = parts[1:]
        
        from speech import engine
        
        if cmd == "help":
            msg = "Commands: DIR, COPY, FIXBOOT, EXIT, HELP"
            self.write_line(msg)
            engine.speak(msg)
        elif cmd == "dir":
            files = os.listdir(os.getcwd())
            self.write_line("\n".join(files))
            engine.speak(f"Listed {len(files)} files.")
        elif cmd == "exit":
            self.EndModal(wx.ID_OK)
        elif cmd == "fixboot":
            self.write_line("Scanning for backups...")
            count = 0
            for f in self.missing_files[:]:
                bak = f + ".bak"
                if os.path.exists(bak):
                    import shutil
                    shutil.copy(bak, f)
                    self.write_line(f"Restored {f} from backup.")
                    self.missing_files.remove(f)
                    count += 1
            if count > 0:
                engine.speak(f"Restored {count} files. Restart the system to complete repair.")
            else:
                engine.speak("No automated fixes found.")
        elif cmd == "copy" or cmd == "cp":
            if len(args) < 2:
                self.write_line("Usage: COPY [source] [destination]")
            else:
                try:
                    import shutil
                    shutil.copy(args[0], args[1])
                    self.write_line("File copied.")
                    if args[1] in self.missing_files:
                        self.missing_files.remove(args[1])
                    engine.speak("Copy successful.")
                except Exception as e:
                    self.write_line(f"Error: {e}")
                    engine.speak("Copy failed.")
        else:
            self.write_line(f"Unknown command: {cmd}")
            engine.speak("Unknown command.")

class RepairFrame(wx.Frame):
    def __init__(self, missing_files, api=None):
        super().__init__(None, title="PyOS System Recovery", size=(500, 450), style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
        self.missing_files = missing_files
        self.api = api
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(wx.Colour(128, 0, 0)) # Recovery Maroon
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        
        text = wx.StaticText(self.panel, label="System Integrity Error")
        text.SetFont(wx.Font(18, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.sizer.Add(text, 0, wx.ALL | wx.CENTER, 20)
        
        msg = f"The following critical files are missing:\n" + "\n".join([f"- {f}" for f in self.missing_files])
        self.file_list_lbl = wx.StaticText(self.panel, label=msg)
        self.file_list_lbl.SetForegroundColour(wx.Colour(255, 255, 255))
        self.sizer.Add(self.file_list_lbl, 0, wx.ALL | wx.CENTER, 10)
        
        self.btn = wx.Button(self.panel, label="Locate Missing Files")
        self.btn.Bind(wx.EVT_BUTTON, self.on_repair)
        self.sizer.Add(self.btn, 0, wx.ALL | wx.CENTER, 10)
        
        self.console_btn = wx.Button(self.panel, label="Boot to Recovery Console")
        self.console_btn.Bind(wx.EVT_BUTTON, self.on_console)
        self.sizer.Add(self.console_btn, 0, wx.ALL | wx.CENTER, 10)
        
        self.panel.SetSizer(self.sizer)
        self.Centre()
        
        from speech import engine
        engine.speak("System Integrity Error. Please use the recovery screen or boot to the console to fix issues.")

    def on_console(self, event):
        console = RecoveryConsole(self, self.missing_files)
        console.ShowModal()
        # Update label after console exit
        msg = f"The following critical files are missing:\n" + "\n".join([f"- {f}" for f in self.missing_files])
        self.file_list_lbl.SetLabel(msg)
        if not self.missing_files:
            wx.MessageBox("Repair finished. Please restart PyOS.", "Success")
            sys.exit(0)

    def on_repair(self, event):
        import shutil
        for file_name in self.missing_files[:]:
            if not self.api:
                wx.MessageBox("File selection is unavailable during recovery.", "Repair")
                continue
            path = choose_file(self, self.api, "open", f"Locate {file_name}",
                               f"{file_name}|{file_name}")
            if not path:
                continue
            try:
                shutil.copy(path, os.path.join(os.getcwd(), file_name))
                self.missing_files.remove(file_name)
                wx.MessageBox(f"Successfully restored {file_name}.", "Repair", wx.OK | wx.ICON_INFORMATION)
            except Exception as e:
                wx.MessageBox(f"Failed to copy {file_name}: {e}", "Error", wx.OK | wx.ICON_ERROR)
                    
        if not self.missing_files:
            wx.MessageBox("System repaired successfully. Please restart PyOS.", "Repair Complete", wx.OK | wx.ICON_INFORMATION)
            self.Destroy()
            sys.exit(0)
        else:
            # Refresh list
            msg = f"The following critical files are still missing:\n" + "\n".join([f"- {f}" for f in self.missing_files])
            # Just a simple way to update for this demo
            wx.MessageBox(msg, "Remaining Issues", wx.OK | wx.ICON_WARNING)

class LoginFrame(wx.Frame):
    def __init__(self, api, on_success):
        super().__init__(None, title="PyOS Login", size=(500, 400), style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
        self.api = api
        self.on_success = on_success
        
        config_path = self.api.get_data_path("config.json")
        with open(config_path, "r") as f:
            self.config = json.load(f)
            
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(wx.Colour(0, 51, 153))
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Simulated User List (XP style)
        self.sizer.AddStretchSpacer()
        
        text = wx.StaticText(self.panel, label=self.config.get("user_name", "User"))
        text.SetFont(wx.Font(24, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.sizer.Add(text, 0, wx.ALL | wx.CENTER, 10)
        
        lbl = wx.StaticText(self.panel, label="Type your password to log in")
        lbl.SetForegroundColour(wx.Colour(200, 200, 200))
        self.sizer.Add(lbl, 0, wx.CENTER, 10)
        
        self.pass_input = wx.TextCtrl(self.panel, style=wx.TE_PASSWORD | wx.TE_PROCESS_ENTER)
        self.pass_input.SetMinSize((200, -1))
        self.sizer.Add(self.pass_input, 0, wx.ALL | wx.CENTER, 10)
        
        btn = wx.Button(self.panel, label="Log In")
        btn.Bind(wx.EVT_BUTTON, self.on_login)
        self.pass_input.Bind(wx.EVT_TEXT_ENTER, self.on_login)
        self.sizer.Add(btn, 0, wx.ALL | wx.CENTER, 10)
        
        self.sizer.AddStretchSpacer()
        
        self.panel.SetSizer(self.sizer)
        self.Centre()
        
        wx.CallAfter(self.greet)

    def greet(self):
        self.api.speak(f"Welcome to PyOS. Please enter your password for {self.config.get('user_name', 'User')}.")
        self.pass_input.SetFocus()

    def on_login(self, event):
        entered = self.pass_input.GetValue()
        correct = self.config.get("password")
        if entered == correct:
            self.api.play_sound("startup")
            self.api.speak("Access granted.")
            self.on_success()
            self.Destroy()
        else:
            self.api.speak("Invalid password. Please try again.")
            self.pass_input.Clear()
            self.pass_input.SetFocus()

class DesktopFrame(wx.Frame):
    def __init__(self, api):
        super().__init__(None, title="PyOS Desktop", size=(800, 600))
        self.api = api
        self.api.desktop = self # Update API reference
        
        self.os_kernel = self.api.kernel
        self.apps = []
        self.app_buttons = []
        self.active_app = None
        
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.header = wx.StaticText(self.panel, label="PyOS Desktop")
        self.header.SetForegroundColour(wx.Colour(255, 255, 255))
        self.header.SetFont(wx.Font(18, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.sizer.Add(self.header, 0, wx.ALL | wx.CENTER, 20)

        self.scrolled_window = wx.ScrolledWindow(self.panel, style=wx.VSCROLL)
        self.scrolled_window.SetScrollRate(0, 20)
        self.scrolled_window.SetBackgroundColour(wx.Colour(0, 0, 0))
        self.app_sizer = wx.BoxSizer(wx.VERTICAL)
        self.scrolled_window.SetSizer(self.app_sizer)
        self.sizer.Add(self.scrolled_window, 1, wx.EXPAND | wx.ALL, 10)

        self.panel.SetSizer(self.sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self.on_key_down)
        self.load_plugins()
        wx.CallAfter(self.greet)

    def on_key_down(self, event):
        keycode = event.GetKeyCode()
        if keycode == wx.WXK_F1:
            self.show_help()
        elif event.ControlDown() and keycode == ord('D'):
            self.show_docs()
        else:
            event.Skip()

    def show_help(self):
        if self.active_app:
            msg = f"Help for {self.active_app.name}: {self.active_app.help_text}"
        else:
            msg = "PyOS Desktop Help: Use Tab to navigate apps, Enter to launch, and F1 for help. Ctrl+D for app documentation."
        self.api.speak(msg)

    def show_docs(self):
        if self.active_app:
            msg = f"Documentation for {self.active_app.name}: {self.active_app.docs}"
        else:
            msg = "Desktop Documentation: PyOS is a modular OS for the blind. Developers can add apps to the apps folder."
        self.api.speak(msg)

    def greet(self):
        msg = "Desktop loaded. Use Tab to navigate through your apps."
        self.api.speak(msg)
        if self.app_buttons:
            self.app_buttons[0].SetFocus()

    def load_plugins(self):
        apps_dir = os.path.join(os.getcwd(), "apps")
        if not os.path.exists(apps_dir):
            os.makedirs(apps_dir)
        
        self.apps = []
        for filename in os.listdir(apps_dir):
            if filename.endswith(".py") and filename != "__init__.py" and filename != "oobe.py":
                self.load_app_from_file(os.path.join(apps_dir, filename))
        self.refresh_app_list()

    def load_app_from_file(self, path):
        try:
            module_name = os.path.basename(path)[:-3]
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            from api import BlindApp
            for attr in dir(module):
                cls = getattr(module, attr)
                if isinstance(cls, type) and issubclass(cls, BlindApp) and cls is not BlindApp:
                    app_instance = cls(self.api)
                    self.apps.append(app_instance)
        except Exception: pass

    def refresh_app_list(self):
        self.app_sizer.Clear(True)
        self.app_buttons = []
        for app in self.apps:
            btn = wx.Button(self.scrolled_window, label=app.name)
            btn.Bind(wx.EVT_BUTTON, lambda evt, a=app: self.on_launch_app(a))
            btn.Bind(wx.EVT_SET_FOCUS, lambda evt, a=app: self.on_item_focused(a))
            self.app_sizer.Add(btn, 0, wx.EXPAND | wx.ALL, 5)
            self.app_buttons.append(btn)
        self.app_sizer.Layout()

    def on_item_focused(self, app):
        if self.IsActive():
            self.api.play_sound("nav")
            self.api.speak(f"{app.name}: {app.description}")

    def on_launch_app(self, app):
        self.active_app = app
        self.api.play_sound("launch")
        app.run()

    def on_app_closed(self, app_instance):
        self.active_app = None
        wx.CallAfter(self._return_focus, app_instance.name)

    def _return_focus(self, app_name):
        for btn in self.app_buttons:
            if btn.GetLabel() == app_name:
                btn.SetFocus()
                break

class PyOSController:
    def __init__(self):
        self.data_dir = get_data_dir()
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
            
        self.os_kernel = kernel.VirtualOS()
        self.sound_manager = sounds.SoundManager(self.data_dir)
        self.api = SystemAPI(None, self.os_kernel, speech.engine, self.sound_manager)
        self.api.message_service.start()
        self._start_music_service()

    def check_integrity(self):
        critical_files = [
            "api.py", "app_paths.py", "audio_devices.py", "kernel.py",
            "message_service.py", "platform_support.py", "sounds.py",
            "speech.py", "oobe_wizard.py", "update_wizard.py"
        ]
        missing = []
        for f in critical_files:
            if not os.path.exists(os.path.join(os.getcwd(), f)):
                missing.append(f)
        return missing

    def start(self):
        # 1. Integrity Check (The "Kernel" Self-Test)
        missing = self.check_integrity()
        if missing:
            repair = RepairFrame(missing, self.api)
            repair.Show()
            return

        # 2. Pending updates
        update_state_path = self.api.get_data_path("update_state.json")
        if os.path.exists(update_state_path):
            from update_wizard import UpdateWizard
            wizard = UpdateWizard(self.api, update_state_path, on_finish=self._continue_boot)
            wizard.Show()
            return

        # 3. Setup/OOBE/Login flow
        self._continue_boot()

    def _continue_boot(self):
        config_path = self.api.get_data_path("config.json")
        if not os.path.exists(config_path):
            self.launch_oobe()
        else:
            with open(config_path, "r") as f:
                config = json.load(f)
                if not config.get("completed_oobe"):
                    self.launch_oobe()
                else:
                    self.launch_login()

    def launch_oobe(self):
        from oobe_wizard import OOBEWizard
        oobe = OOBEWizard(self.api, on_finish=self.launch_login)
        oobe.Show()

    def launch_login(self):
        login = LoginFrame(self.api, on_success=self.launch_desktop)
        login.Show()

    def launch_desktop(self):
        desktop = DesktopFrame(self.api)
        desktop.Show()

    def _start_music_service(self):
        threading.Thread(target=self._background_music_thread, daemon=True).start()

    def _background_music_thread(self):
        # Optional dependencies: without sounddevice/soundfile the thread simply
        # stays idle instead of crashing on import.
        try:
            import sounddevice as sd
            import soundfile as sf
            import numpy as np
        except ImportError:
            return
        music_config_path = self.api.get_data_path("music_config.json")
        current_music = None
        current_volume = None
        stream = None
        while True:
            new_music = "None"
            new_volume = 50
            if os.path.exists(music_config_path):
                try:
                    with open(music_config_path, "r") as f:
                        cfg = json.load(f)
                        new_music = cfg.get("music", "None")
                        new_volume = cfg.get("volume", 50)
                except Exception: pass
            volume_changed = new_volume != current_volume
            if new_music != current_music or volume_changed:
                current_music = new_music
                current_volume = new_volume
                if stream:
                    stream.stop()
                    stream.close()
                    stream = None
            if current_music != "None" and stream is None:
                # Resolve through SoundManager so custom/absolute theme music
                # paths work, not just files in the repo music folder.
                music_path = self.sound_manager.resolve_music_path(current_music)
                if music_path and os.path.exists(music_path):
                    try:
                        file = sf.SoundFile(music_path)
                        vol = current_volume / 100.0
                        def callback(outdata, frames, time, status):
                            data = file.read(frames, fill_value=0)
                            if len(data) < frames:
                                file.seek(0)
                                data = np.concatenate([data, file.read(frames - len(data), fill_value=0)])
                            outdata[:] = data * vol
                        stream = sd.OutputStream(samplerate=file.samplerate, channels=file.channels, callback=callback)
                        stream.start()
                    except Exception: time.sleep(5)
            time.sleep(1)

def print_centered(text, row):
    width = 80
    padding = (width - len(text)) // 2
    sys.stdout.write(f"\033[{row};1H" + " " * padding + text + " " * (width - len(text) - padding))
    sys.stdout.flush()

def wait_for_key(expected_keys=None):
    with platform_support.raw_mode(sys.stdin):
        while True:
            if platform_support.kbhit():
                # Note: platform_support.getch() internally also uses raw_mode, 
                # but nested raw_mode should be fine with our implementation.
                # However, for efficiency, we could read directly here if in raw_mode.
                key = platform_support.getch()
                if key in [b'\x00', b'\xe0']:
                    key += platform_support.getch()
                elif key == b'\x1b':
                    # On Linux, handle escape sequences (simplistic)
                    if platform_support.kbhit():
                        key += platform_support.getch()
                        if platform_support.kbhit():
                            key += platform_support.getch()
                if expected_keys is None:
                    return key
                if key in expected_keys:
                    return key
            time.sleep(0.01) # Add a small sleep to prevent 100% CPU usage

def check_system_integrity():
    critical_files = [
        "api.py", "app_paths.py", "audio_devices.py", "kernel.py",
        "message_service.py", "platform_support.py", "sounds.py",
        "speech.py", "oobe_wizard.py", "update_wizard.py"
    ]
    missing = []
    for f in critical_files:
        if not os.path.exists(os.path.join(os.getcwd(), f)):
            missing.append(f)
    return missing

def run_text_mode_setup():
    from speech import engine
    config_path = os.path.join(get_data_dir(), "config.json")
    if os.path.exists(config_path): return

    # 1. WELCOME
    sys.stdout.write("\033[2J\033[H\033[44;37m")
    for r in range(1, 25): sys.stdout.write(f"\033[{r};1H" + " " * 80)
    print_centered("Windows XP Setup", 1)
    sys.stdout.write("\033[24;1H\033[47;30m ENTER=Continue  F3=Quit " + "\033[44;37m" + " " * 55)
    print_centered("Welcome to Setup.", 3)
    msg = ["To set up Windows XP now, press ENTER.", "To repair a Windows XP installation, press R.", "To quit Setup without installing Windows XP, press F3."]
    for i, line in enumerate(msg): sys.stdout.write(f"\033[{10+i};5H{line}")
    sys.stdout.flush()
    engine.speak("Welcome to Windows XP Setup. Press Enter to continue, or F3 to quit.")
    
    # F3 = b'\x00=' or b'\xe0=', or '3' as fallback
    key = wait_for_key([b'\r', b'\x00=', b'\xe0=', b'3', b'\x00;', b'r', b'R'])
    if key in [b'\x00=', b'\xe0=', b'3']: sys.exit(0)
    if key in [b'r', b'R']:
        engine.speak("Recovery Console is not available in this version. Proceeding with standard installation.")

    # 2. LICENSE
    sys.stdout.write("\033[2J\033[H\033[44;37m")
    for r in range(1, 25): sys.stdout.write(f"\033[{r};1H" + " " * 80)
    print_centered("Windows XP Licensing Agreement", 1)
    sys.stdout.write("\033[24;1H\033[47;30m F8=I Agree  ESC=I Do Not Agree " + "\033[44;37m" + " " * 47)
    license_text = ["Please read the following license agreement carefully.", "You must agree to these terms to continue installation.", "", "1. You agree that PyOS is a simulator.", "2. You agree to have fun.", "3. You agree that Windows XP is legendary."]
    for i, line in enumerate(license_text): sys.stdout.write(f"\033[{5+i};5H{line}")
    sys.stdout.flush()
    engine.speak("Please read the licensing agreement. Press F8 to agree and continue, or Escape to cancel. If F8 doesn't work, press the 8 key.")
    
    # F8 = b'\x00B' or b'\xe0B', or '8' as fallback
    key = wait_for_key([b'\x00B', b'\xe0B', b'8', b'\x1b']) 
    if key == b'\x1b': sys.exit(0)

    # 3. PARTITIONING
    sys.stdout.write("\033[2J\033[H\033[44;37m")
    for r in range(1, 25): sys.stdout.write(f"\033[{r};1H" + " " * 80)
    print_centered("Windows XP Setup", 1)
    sys.stdout.write("\033[24;1H\033[47;30m ENTER=Install  C=Create Partition  D=Delete Partition " + "\033[44;37m" + " " * 27)
    print_centered("The following list shows the existing partitions.", 3)
    sys.stdout.write("\033[10;5H- Unpartitioned space [Virtual Drive]          40960 MB")
    sys.stdout.flush()
    engine.speak("Select a partition to install PyOS. Press Enter to use the unpartitioned space.")
    
    wait_for_key([b'\r'])

    # 4. FORMATTING
    sys.stdout.write("\033[2J\033[H\033[44;37m")
    for r in range(1, 25): sys.stdout.write(f"\033[{r};1H" + " " * 80)
    print_centered("Windows XP Setup", 1)
    print_centered("Setup is formatting...", 10)
    engine.speak("Formatting the virtual drive...")
    bar_width = 40
    for i in range(101):
        filled = int(bar_width * i / 100)
        bar = "\033[47m" + " " * filled + "\033[40m" + " " * (bar_width - filled) + "\033[44m"
        sys.stdout.write(f"\033[15;20H{bar} {i}%")
        sys.stdout.flush()
        time.sleep(0.02)

    # 5. COPYING
    sys.stdout.write("\033[2J\033[H\033[44;37m")
    for r in range(1, 25): sys.stdout.write(f"\033[{r};1H" + " " * 80)
    print_centered("Windows XP Setup", 1)
    print_centered("Setup is copying files...", 10)
    engine.speak("Copying system files...")
    files = ["ntoskrnl.exe", "hal.dll", "vga.sys", "ntfs.sys", "shell32.dll"]
    for f in files:
        sys.stdout.write(f"\033[15;25HCopying: {f}" + " " * 20)
        sys.stdout.flush()
        time.sleep(0.3)

    # 6. REBOOT
    sys.stdout.write("\033[2J\033[H\033[40;37m")
    print_centered("Setup has completed successfully.", 10)
    print_centered("Your computer will reboot in 5 seconds.", 12)
    engine.speak("Setup has completed successfully. The system will now reboot.")
    for i in range(5, 0, -1):
        sys.stdout.write(f"\033[14;40H{i}...")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\033[0m\033[2J\033[H")
    sys.stdout.flush()

if __name__ == "__main__":
    missing = check_system_integrity()
    if missing:
        app = wx.App()
        repair = RepairFrame(missing)
        repair.Show()
        app.MainLoop()
        sys.exit(0)

    run_text_mode_setup()
    from update_wizard import run_text_mode_update
    run_text_mode_update(get_data_dir())
    app = wx.App()
    controller = PyOSController()
    controller.start()
    app.MainLoop()
    controller.sound_manager.play_sync("shutdown")
