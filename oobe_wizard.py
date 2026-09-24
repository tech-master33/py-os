import wx
import os
import json

MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")
MUSIC_FILES = sorted(
    [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith(('.wav', '.aif', '.mp3', '.ogg'))],
    key=str.lower,
) if os.path.isdir(MUSIC_DIR) else []

class OOBEWizard(wx.Frame):
    def __init__(self, api, on_finish):
        super().__init__(None, title="PyOS Setup", size=(600, 500), style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
        self.api = api
        self.on_finish = on_finish
        self.config_path = self.api.get_data_path("config.json")
        self.steps = [
            self.step_welcome,
            self.step_install,
            self.step_regional,
            self.step_user_name,
            self.step_password,
            self.step_accessibility,
            self.step_theme,
            self.step_finish
        ]
        self.current_step = 0
        self.user_data = {}
        self.install_timer = None
        self.install_progress = 0
        self.music_index = 0

        self.panel = wx.Panel(self)
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.content_sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(self.content_sizer, 1, wx.EXPAND | wx.ALL, 20)
        
        self.panel.SetSizer(self.sizer)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        
        # Play XP intro music if possible
        self.play_intro_music()
        
        self.show_step()
        self.Centre()

    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_F5:
            self.cycle_music()
        else:
            event.Skip()

    def cycle_music(self):
        self.music_index = (self.music_index + 1) % len(MUSIC_FILES)
        track = MUSIC_FILES[self.music_index]
        music_path = os.path.join(MUSIC_DIR, track)
        if os.path.exists(music_path):
            music_config = {"music": track}
            config_file = self.api.get_data_path("music_config.json")
            try:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(music_config, f, ensure_ascii=False)
                name = track.replace(".aif", "").replace(".wav", "").replace("_", " ").strip()
                self.api.speak(f"Now playing: {name}")
            except Exception as e:
                print(f"Error cycling music: {e}")

    def on_close(self, event):
        if self.install_timer:
            self.install_timer.Stop()
        self.Destroy()

    def play_intro_music(self):
        music_path = os.path.join(MUSIC_DIR, "1996 Internet Starter Kit - Velkommen - Original Mix.wav")
        if os.path.exists(music_path):
            music_config = {"music": "1996 Internet Starter Kit - Velkommen - Original Mix.wav"}
            config_file = self.api.get_data_path("music_config.json")
            try:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(music_config, f, ensure_ascii=False)
            except Exception as e:
                print(f"Error setting music: {e}")

    def show_step(self):
        self.content_sizer.Clear(True)
        self.steps[self.current_step]()
        self.panel.Layout()

    def next_step(self):
        self.current_step += 1
        if self.current_step < len(self.steps):
            self.show_step()
        else:
            self.finish_oobe()

    def step_welcome(self):
        self.panel.SetBackgroundColour(wx.Colour(0, 51, 153)) # XP Blueish
        text = wx.StaticText(self.panel, label="Welcome to PyOS!")
        text.SetFont(wx.Font(22, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(text, 0, wx.ALL | wx.CENTER, 10)
        
        desc = wx.StaticText(self.panel, label="This wizard will help you set up your new operating system simulator.\n\nRelax while we prepare your experience.")
        desc.SetForegroundColour(wx.Colour(200, 200, 200))
        desc.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.content_sizer.Add(desc, 0, wx.ALL | wx.CENTER, 10)
        
        btn = wx.Button(self.panel, label="Next")
        btn.Bind(wx.EVT_BUTTON, lambda e: self.next_step())
        self.content_sizer.Add(btn, 0, wx.ALL | wx.CENTER, 20)
        
        btn.SetFocus()
        self.api.speak("Welcome to PyOS! This wizard will help you set up your new operating system simulator. Press Next to continue.")

    def step_install(self):
        self.panel.SetBackgroundColour(wx.Colour(240, 240, 240))
        text = wx.StaticText(self.panel, label="Installing PyOS Components...")
        text.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.content_sizer.Add(text, 0, wx.ALL | wx.CENTER, 10)
        
        self.files_to_copy = [
            "kernel.sys", "shell.dll", "drivers/audio.drv", "drivers/video.sys",
            "ui/theme_xp.dat", "apps/terminal.py", "apps/editor.py",
            "vfs/welcome.txt", "fonts/tahoma.ttf", "registry.bin"
        ]
        
        self.gauges = []
        self.gauge_labels = []
        
        grid = wx.FlexGridSizer(rows=10, cols=2, vgap=5, hgap=10)
        grid.AddGrowableCol(1, 1)
        
        for file_name in self.files_to_copy:
            lbl = wx.StaticText(self.panel, label=f"Copying {file_name}...")
            lbl.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            gauge = wx.Gauge(self.panel, range=100, size=(300, 15))
            
            grid.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
            grid.Add(gauge, 1, wx.EXPAND | wx.RIGHT, 10)
            
            self.gauges.append(gauge)
            self.gauge_labels.append(lbl)
            
        self.content_sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 10)
        
        self.install_progress = 0
        self.install_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_install_timer, self.install_timer)
        self.install_timer.Start(500) 
        
        self.api.speak("Starting file copy. Please wait while we install system components.")

    def on_install_timer(self, event):
        self.install_progress += 1
        
        import random
        all_done = True
        
        for i, gauge in enumerate(self.gauges):
            current = gauge.GetValue()
            if current < 100:
                all_done = False
                increment = max(1, 5 - (i // 2)) 
                increment += random.randint(0, 2)
                
                new_val = min(100, current + increment)
                gauge.SetValue(new_val)
                
                if new_val == 100:
                    self.gauge_labels[i].SetLabel(f"Installed {self.files_to_copy[i]}")
                    self.api.speak(f"Finished copying {self.files_to_copy[i]}.")
            
        if all_done or self.install_progress >= 120:
            self.install_timer.Stop()
            wx.CallLater(1000, self.next_step)

    def step_regional(self):
        self.panel.SetBackgroundColour(wx.Colour(0, 51, 153))
        text = wx.StaticText(self.panel, label="Regional and Language Options")
        text.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(text, 0, wx.ALL, 10)
        
        regions = ["English (United States)", "English (United Kingdom)", "French (France)", "German (Germany)", "Spanish (Spain)"]
        self.region_choice = wx.Choice(self.panel, choices=regions)
        self.region_choice.SetSelection(0)
        self.content_sizer.Add(self.region_choice, 0, wx.EXPAND | wx.ALL, 10)
        
        btn = wx.Button(self.panel, label="Next")
        btn.Bind(wx.EVT_BUTTON, self.on_regional_submit)
        self.content_sizer.Add(btn, 0, wx.ALL | wx.RIGHT, 10)
        
        self.region_choice.SetFocus()
        self.api.speak("Select your regional and language settings. Use arrow keys and press Next.")

    def on_regional_submit(self, event):
        self.user_data["region"] = self.region_choice.GetStringSelection()
        self.next_step()

    def step_user_name(self):
        text = wx.StaticText(self.panel, label="Personalize your software")
        text.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(text, 0, wx.ALL, 10)
        
        lbl = wx.StaticText(self.panel, label="Name:")
        lbl.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(lbl, 0, wx.LEFT, 10)
        
        self.name_input = wx.TextCtrl(self.panel)
        self.name_input.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak("Name input"), e.Skip()))
        self.content_sizer.Add(self.name_input, 0, wx.EXPAND | wx.ALL, 10)
        
        btn = wx.Button(self.panel, label="Next")
        btn.Bind(wx.EVT_BUTTON, self.on_name_submit)
        self.content_sizer.Add(btn, 0, wx.ALL | wx.RIGHT, 10)
        
        self.name_input.SetFocus()
        self.api.speak("Type your full name and press Next.")

    def on_name_submit(self, event):
        name = self.name_input.GetValue().strip()
        if not name:
            self.api.speak("Please enter a name.")
            return
        self.user_data["user_name"] = name
        self.next_step()

    def step_password(self):
        text = wx.StaticText(self.panel, label="Protect your account")
        text.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(text, 0, wx.ALL, 10)
        
        lbl = wx.StaticText(self.panel, label="Create a password:")
        lbl.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(lbl, 0, wx.LEFT, 10)
        
        self.pass_input = wx.TextCtrl(self.panel, style=wx.TE_PASSWORD)
        self.pass_input.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak("Password input"), e.Skip()))
        self.content_sizer.Add(self.pass_input, 0, wx.EXPAND | wx.ALL, 10)
        
        lbl2 = wx.StaticText(self.panel, label="Confirm password:")
        lbl2.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(lbl2, 0, wx.LEFT, 10)
        
        self.pass_confirm = wx.TextCtrl(self.panel, style=wx.TE_PASSWORD)
        self.content_sizer.Add(self.pass_confirm, 0, wx.EXPAND | wx.ALL, 10)
        
        btn = wx.Button(self.panel, label="Next")
        btn.Bind(wx.EVT_BUTTON, self.on_pass_submit)
        self.content_sizer.Add(btn, 0, wx.ALL | wx.RIGHT, 10)
        
        self.pass_input.SetFocus()
        self.api.speak("Create a password for your account. You will need this to log in.")

    def on_pass_submit(self, event):
        p1 = self.pass_input.GetValue()
        p2 = self.pass_confirm.GetValue()
        if not p1:
            self.api.speak("Please enter a password.")
            return
        if p1 != p2:
            self.api.speak("Passwords do not match. Please try again.")
            return
        self.user_data["password"] = p1
        self.next_step()

    def step_accessibility(self):
        text = wx.StaticText(self.panel, label="Accessibility Settings")
        text.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(text, 0, wx.ALL, 10)

        self.acc_check = wx.CheckBox(self.panel, label="Use screen reader enhanced mode")
        self.acc_check.SetForegroundColour(wx.Colour(255, 255, 255))
        self.acc_check.SetValue(True)
        self.acc_check.Bind(wx.EVT_SET_FOCUS, lambda e: (self.api.speak(f"Use screen reader enhanced mode, checkbox {'checked' if self.acc_check.GetValue() else 'unchecked'}"), e.Skip()))
        self.content_sizer.Add(self.acc_check, 0, wx.ALL, 10)
        
        btn = wx.Button(self.panel, label="Next")
        btn.Bind(wx.EVT_BUTTON, self.on_acc_submit)
        self.content_sizer.Add(btn, 0, wx.ALL | wx.RIGHT, 10)
        
        self.acc_check.SetFocus()
        self.api.speak("Would you like to use screen reader enhanced mode? This is enabled by default. Press Space to toggle and Enter to continue.")

    def on_acc_submit(self, event):
        self.user_data["accessibility_mode"] = self.acc_check.GetValue()
        self.next_step()

    def step_theme(self):
        text = wx.StaticText(self.panel, label="Choose your theme")
        text.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(text, 0, wx.ALL, 10)
        
        themes = self.api.sounds.get_available_themes()
        self.theme_choice = wx.ListBox(self.panel, choices=themes)
        if "XP" in themes:
            self.theme_choice.SetSelection(themes.index("XP"))
        else:
            self.theme_choice.SetSelection(0)
            
        self.content_sizer.Add(self.theme_choice, 1, wx.EXPAND | wx.ALL, 10)
        
        btn = wx.Button(self.panel, label="Next")
        btn.Bind(wx.EVT_BUTTON, self.on_theme_submit)
        self.content_sizer.Add(btn, 0, wx.ALL | wx.RIGHT, 10)
        
        self.theme_choice.SetFocus()
        self.api.speak("Choose your sound theme. Use the arrow keys to select and press Next.")

    def on_theme_submit(self, event):
        theme = self.theme_choice.GetStringSelection()
        self.user_data["theme"] = theme
        self.api.sounds.save_theme_name(theme)
        self.next_step()

    def step_finish(self):
        text = wx.StaticText(self.panel, label="Congratulations!")
        text.SetFont(wx.Font(18, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        text.SetForegroundColour(wx.Colour(255, 255, 255))
        self.content_sizer.Add(text, 0, wx.ALL | wx.CENTER, 10)
        
        desc = wx.StaticText(self.panel, label=f"Thank you, {self.user_data.get('user_name', 'User')}. Your setup is complete.")
        desc.SetForegroundColour(wx.Colour(200, 200, 200))
        self.content_sizer.Add(desc, 0, wx.ALL | wx.CENTER, 10)
        
        btn = wx.Button(self.panel, label="Finish")
        btn.Bind(wx.EVT_BUTTON, lambda e: self.finish_oobe())
        self.content_sizer.Add(btn, 0, wx.ALL | wx.CENTER, 20)
        btn.SetFocus()
        self.api.speak(f"Congratulations! Thank you, {self.user_data.get('user_name', 'User')}. Your setup is complete. Press Finish to start using PyOS.")

    def finish_oobe(self):
        self.user_data["completed_oobe"] = True
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.user_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving config: {e}")
        
        # Reset music to None after OOBE
        music_config = {"music": "None"}
        try:
            with open(self.api.get_data_path("music_config.json"), "w", encoding="utf-8") as f:
                json.dump(music_config, f, ensure_ascii=False)
        except Exception as e:
            print(f"Error resetting music: {e}")
            
        self.on_finish()
        self.Destroy()
