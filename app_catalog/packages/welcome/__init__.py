import wx

from api import BlindApp

from .greeting import get_greeting


class WelcomeApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "Welcome"
        self.description = "A sample multi-file PyOS app."
        self.category = "Examples"
        self.help_text = "Open the Welcome app to read its greeting."
        self.docs = "This sample package imports its greeting from a sibling module."

    def run(self):
        wx.MessageBox(get_greeting(), self.name, wx.OK | wx.ICON_INFORMATION)
