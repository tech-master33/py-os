import wx
import os
import json
import sys
import subprocess
import threading
import time

from app_paths import get_repo_root

# desktop.py lives beside this wizard, so relaunches resolve from the
# repository root instead of the current working directory: an update finished
# from a shortcut with a different "Start in" folder must still bring PyOS
# back up.
REPO_ROOT = get_repo_root()

OOBE_MUSIC = "1996 Internet Starter Kit - Velkommen - Original Mix.wav"


def run_text_mode_update(data_dir):
    from speech import engine
    state_path = os.path.join(data_dir, "update_state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return

    if state.get("phase") != 1:
        return

    sys.stdout.write("\033[2J\033[H\033[44;37m")
    for r in range(1, 25):
        sys.stdout.write(f"\033[{r};1H" + " " * 80)

    lines = [
        "PyOS Update",
        "",
        "Setup is starting the update.",
        "",
        "Do not unplug your computer.",
        "",
    ]
    for i, line in enumerate(lines):
        offset = 10 + i
        padding = (80 - len(line)) // 2
        sys.stdout.write(f"\033[{offset};1H" + " " * padding + line + " " * (80 - len(line) - padding))

    sys.stdout.write("\033[24;1H\033[47;30m" + " " * 80)
    sys.stdout.flush()
    engine.speak("Setup is starting the update. Do not unplug your computer.")

    for i in range(20, 0, -1):
        bar = "[" + "#" * min(20 - i, 20) + " " * i + "]"
        sys.stdout.write(f"\033[18;30H{bar} {20 - i}/20")
        sys.stdout.flush()
        time.sleep(1)

    state["phase"] = 2
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    sys.stdout.write("\033[0m\033[2J\033[H")
    sys.stdout.flush()
    subprocess.Popen([sys.executable, os.path.join(REPO_ROOT, "desktop.py")], cwd=str(REPO_ROOT))
    sys.exit(0)

class UpdateWizard(wx.Frame):
    def __init__(self, api, state_path, on_finish):
        super().__init__(None, title="PyOS Update", size=(650, 450),
                         style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
        self.api = api
        self.state_path = state_path
        self.on_finish = on_finish
        self._cancelled = False

        with open(state_path, "r", encoding="utf-8") as f:
            self.state = json.load(f)
        self.phase = self.state.get("phase", 1)

        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(wx.Colour(0, 0, 139))
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.AddStretchSpacer()

        self.header = wx.StaticText(self.panel, label="")
        self.header.SetForegroundColour(wx.Colour(255, 255, 255))
        self.header.SetFont(wx.Font(18, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.sizer.Add(self.header, 0, wx.ALL | wx.CENTER, 15)

        self.msg = wx.StaticText(self.panel, label="")
        self.msg.SetForegroundColour(wx.Colour(200, 200, 200))
        self.msg.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.sizer.Add(self.msg, 0, wx.ALL | wx.CENTER, 10)

        self.progress = wx.Gauge(self.panel, range=100, size=(400, 24))
        self.progress.SetBackgroundColour(wx.Colour(0, 0, 0))
        self.progress.SetForegroundColour(wx.Colour(0, 255, 0))
        self.sizer.Add(self.progress, 0, wx.ALL | wx.CENTER, 15)

        self.sub_msg = wx.StaticText(self.panel, label="")
        self.sub_msg.SetForegroundColour(wx.Colour(180, 180, 255))
        self.sizer.Add(self.sub_msg, 0, wx.ALL | wx.CENTER, 5)

        self.sizer.AddStretchSpacer()
        self.panel.SetSizer(self.sizer)

        self.Centre()
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self._play_oobe_music()
        self._run_phase()

    def _play_oobe_music(self):
        music_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music", OOBE_MUSIC)
        if os.path.exists(music_path):
            config_file = self.api.get_data_path("music_config.json")
            try:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump({"music": OOBE_MUSIC}, f, ensure_ascii=False)
            except Exception:
                pass

    def _run_phase(self):
        threading.Thread(target=self._do_phase, daemon=True).start()

    def _do_phase(self):
        if self.phase == 2:
            self._phase_getting_ready(1)
        elif self.phase == 3:
            self._phase_getting_ready(2)
        elif self.phase == 4:
            self._phase_getting_ready(3)

    def _set_ui(self, header, msg, sub=""):
        wx.CallAfter(self.header.SetLabel, header)
        wx.CallAfter(self.msg.SetLabel, msg)
        wx.CallAfter(self.sub_msg.SetLabel, sub)

    def _set_progress(self, val):
        wx.CallAfter(self.progress.SetValue, val)

    def _phase_getting_ready(self, num):
        self._set_ui("Getting ready...",
                      f"Please wait while PyOS prepares your system ({num}/3).",
                      "Do not turn off your computer.")
        total_sec = 10
        for i in range(101):
            if self._cancelled:
                return
            self._set_progress(i)
            time.sleep(total_sec / 100)

        if self.phase >= 4:
            self._finish_updates()
        else:
            self.state["phase"] = self.phase + 1
            self._save_and_reboot()

    def _save_and_reboot(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except Exception:
            pass
        wx.CallAfter(self._reboot)

    def _reboot(self):
        subprocess.Popen([sys.executable, os.path.join(REPO_ROOT, "desktop.py")], cwd=str(REPO_ROOT))
        wx.GetApp().ExitMainLoop()

    def _finish_updates(self):
        try:
            if os.path.exists(self.state_path):
                os.remove(self.state_path)
        except Exception:
            pass
        wx.CallAfter(self.on_finish)
        wx.CallAfter(self.Close)

    def on_close(self, event=None):
        self._cancelled = True
        self.Destroy()
