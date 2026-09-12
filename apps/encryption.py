import wx
import os
import hashlib
import hmac
import threading
from api import BlindApp

CHUNK_SIZE = 65536

def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200000, dklen=32)

def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()

def _generate_keystream(key: bytes, length: int, progress_cb=None):
    keystream = b''
    counter = 0
    while len(keystream) < length:
        keystream += _hmac_sha256(key, counter.to_bytes(16, 'big'))
        counter += 1
        if progress_cb and counter % 64 == 0:
            progress_cb(min(len(keystream), length), length)
    return keystream[:length]

def _xor_data(data: bytes, keystream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, keystream))

ENCRYPT_EXT = ".pyos-enc"

class EncryptionApp(BlindApp):
    def __init__(self, api):
        super().__init__(api)
        self.name = "Encryption"
        self.description = "Encrypt or decrypt files with a password."
        self.category = "Tools"
        self.help_text = "Select a file, choose Encrypt or Decrypt, enter a password, then press Run."
        self.docs = "Encryption uses PBKDF2 key derivation with HMAC-SHA256 stream cipher and integrity verification. Encrypted files have a .pyos-enc extension."
        self.progress_dialog = None
        self.work_thread = None
        self.cancelled = False

    def run(self):
        self.frame = wx.Frame(None, title="Encryption", size=(500, 450))
        panel = wx.Panel(self.frame)
        panel.SetBackgroundColour(wx.Colour(0, 0, 0))
        sizer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(panel, label="Encryption Tool")
        title.SetForegroundColour(wx.Colour(255, 255, 255))
        title.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(title, 0, wx.ALL | wx.CENTER, 15)

        file_lbl = wx.StaticText(panel, label="Selected File:")
        file_lbl.SetForegroundColour(wx.Colour(200, 200, 200))
        sizer.Add(file_lbl, 0, wx.LEFT | wx.TOP, 10)

        self.file_display = wx.TextCtrl(panel, style=wx.TE_READONLY)
        self.file_display.SetBackgroundColour(wx.Colour(20, 20, 20))
        self.file_display.SetForegroundColour(wx.Colour(255, 255, 255))
        self.file_display.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Selected file"))
        sizer.Add(self.file_display, 0, wx.EXPAND | wx.ALL, 10)

        browse_btn = wx.Button(panel, label="&Browse for File")
        browse_btn.Bind(wx.EVT_BUTTON, self.on_browse)
        browse_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Browse for File"))
        sizer.Add(browse_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        mode_lbl = wx.StaticText(panel, label="Operation:")
        mode_lbl.SetForegroundColour(wx.Colour(200, 200, 200))
        sizer.Add(mode_lbl, 0, wx.LEFT, 10)

        self.mode_radio = wx.RadioBox(panel, choices=["Encrypt", "Decrypt"], style=wx.RA_HORIZONTAL)
        self.mode_radio.SetBackgroundColour(wx.Colour(0, 0, 0))
        self.mode_radio.SetForegroundColour(wx.Colour(255, 255, 255))
        self.mode_radio.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Operation: Encrypt or Decrypt"))
        sizer.Add(self.mode_radio, 0, wx.EXPAND | wx.ALL, 10)

        pw_lbl = wx.StaticText(panel, label="Password:")
        pw_lbl.SetForegroundColour(wx.Colour(200, 200, 200))
        sizer.Add(pw_lbl, 0, wx.LEFT, 10)

        self.pw_input = wx.TextCtrl(panel, style=wx.TE_PASSWORD | wx.TE_PROCESS_ENTER)
        self.pw_input.SetBackgroundColour(wx.Colour(30, 30, 30))
        self.pw_input.SetForegroundColour(wx.Colour(255, 255, 255))
        self.pw_input.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Password"))
        sizer.Add(self.pw_input, 0, wx.EXPAND | wx.ALL, 10)

        run_btn = wx.Button(panel, label="&Run")
        run_btn.SetDefault()
        run_btn.Bind(wx.EVT_BUTTON, self.on_run)
        run_btn.Bind(wx.EVT_SET_FOCUS, lambda e: self.api.speak("Run"))
        self.pw_input.Bind(wx.EVT_TEXT_ENTER, self.on_run)
        sizer.Add(run_btn, 0, wx.ALL | wx.CENTER, 10)

        self.status_lbl = wx.StaticText(panel, label="")
        self.status_lbl.SetForegroundColour(wx.Colour(180, 255, 180))
        sizer.Add(self.status_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(sizer)
        self.frame.Bind(wx.EVT_CLOSE, self.on_close)
        self.frame.Show()
        self.api.speak("Encryption app opened. Browse for a file, select encrypt or decrypt, enter a password, and press Run.")
        browse_btn.SetFocus()

    def on_browse(self, event):
        wildcard = "All files (*.*)|*.*"
        path = self.api.choose_file(self.frame, "open", "Select a file", wildcard)
        if path:
            self.file_display.SetValue(path)
            self.api.speak(f"Selected {os.path.basename(path)}")

    def on_run(self, event):
        file_path = self.file_display.GetValue().strip()
        if not file_path:
            self.api.speak("Please select a file first.")
            return
        if not os.path.exists(file_path):
            self.api.speak("The selected file does not exist.")
            return
        password = self.pw_input.GetValue()
        if not password:
            self.api.speak("Please enter a password.")
            self.pw_input.SetFocus()
            return
        mode = self.mode_radio.GetStringSelection()
        if mode == "Decrypt" and not file_path.endswith(ENCRYPT_EXT):
            confirm = wx.MessageBox(
                "The file does not have a .pyos-enc extension. Decrypt anyway?",
                "Confirm Decryption",
                style=wx.YES_NO | wx.ICON_QUESTION,
            )
            if confirm != wx.YES:
                self.api.speak("Decryption cancelled.")
                return
        self.cancelled = False
        self.progress_dialog = wx.ProgressDialog(
            f"{mode}ing file...",
            "Initializing...",
            maximum=100,
            parent=self.frame,
            style=wx.PD_APP_MODAL | wx.PD_ELAPSED_TIME | wx.PD_CAN_ABORT,
        )
        self.progress_dialog.SetBackgroundColour(wx.Colour(0, 0, 0))
        self.progress_dialog.SetForegroundColour(wx.Colour(255, 255, 255))
        self.work_thread = threading.Thread(
            target=self._do_work,
            args=(file_path, password, mode),
            daemon=True,
        )
        self.work_thread.start()

    def _progress_cb(self, current, total):
        if self.progress_dialog:
            pct = int(current * 100 / total) if total else 0
            keep_going, _ = self.progress_dialog.Update(pct, f"Processing... {pct}%")
            if not keep_going:
                self.cancelled = True

    def _do_work(self, file_path, password, mode):
        try:
            if mode == "Encrypt":
                self._do_encrypt(file_path, password)
            else:
                self._do_decrypt(file_path, password)
        except Exception as e:
            wx.CallAfter(self._work_error, str(e))
        finally:
            wx.CallAfter(self._work_done)

    def _work_error(self, msg):
        if self.progress_dialog:
            self.progress_dialog.Destroy()
            self.progress_dialog = None
        self.api.speak(f"Operation failed: {msg}")
        self.status_lbl.SetLabel(f"Error: {msg}")
        self.status_lbl.SetForegroundColour(wx.Colour(255, 100, 100))

    def _work_done(self):
        if self.progress_dialog:
            self.progress_dialog.Destroy()
            self.progress_dialog = None

    def _do_encrypt(self, file_path, password):
        wx.CallAfter(self._set_progress_msg, "Reading file...")
        with open(file_path, 'rb') as f:
            data = f.read()
        if self.cancelled:
            return
        file_size = len(data)
        wx.CallAfter(self._set_progress_msg, "Deriving key...")
        salt = os.urandom(16)
        key = _derive_key(password, salt)
        if self.cancelled:
            return
        enc_key = key[:16]
        mac_key = key[16:32]
        wx.CallAfter(self._set_progress_msg, "Generating keystream...")
        keystream = _generate_keystream(enc_key, file_size, self._progress_cb)
        if self.cancelled:
            return
        wx.CallAfter(self._set_progress_msg, "Encrypting...")
        ciphertext = _xor_data(data, keystream)
        wx.CallAfter(self._set_progress_msg, "Computing integrity check...")
        mac = _hmac_sha256(mac_key, salt + ciphertext)
        out_path = file_path + ENCRYPT_EXT
        wx.CallAfter(self._set_progress_msg, "Writing output...")
        with open(out_path, 'wb') as f:
            f.write(salt + mac + ciphertext)
        if not self.cancelled:
            wx.CallAfter(self._finish_msg, f"Encrypted to {os.path.basename(out_path)}")

    def _do_decrypt(self, file_path, password):
        wx.CallAfter(self._set_progress_msg, "Reading file...")
        with open(file_path, 'rb') as f:
            data = f.read()
        if self.cancelled:
            return
        if len(data) < 48:
            raise ValueError("File is too small to be a valid encrypted file.")
        salt = data[:16]
        stored_mac = data[16:48]
        ciphertext = data[48:]
        if self.cancelled:
            return
        wx.CallAfter(self._set_progress_msg, "Deriving key...")
        key = _derive_key(password, salt)
        if self.cancelled:
            return
        enc_key = key[:16]
        mac_key = key[16:32]
        wx.CallAfter(self._set_progress_msg, "Verifying integrity...")
        expected_mac = _hmac_sha256(mac_key, salt + ciphertext)
        if not hmac.compare_digest(stored_mac, expected_mac):
            raise ValueError("Invalid password or corrupted data")
        if self.cancelled:
            return
        wx.CallAfter(self._set_progress_msg, "Generating keystream...")
        keystream = _generate_keystream(enc_key, len(ciphertext), self._progress_cb)
        if self.cancelled:
            return
        wx.CallAfter(self._set_progress_msg, "Decrypting...")
        decrypted = _xor_data(ciphertext, keystream)
        if file_path.endswith(ENCRYPT_EXT):
            out_path = file_path[:-len(ENCRYPT_EXT)]
        else:
            base, ext = os.path.splitext(file_path)
            out_path = base + "_decrypted" + ext
        wx.CallAfter(self._set_progress_msg, "Writing output...")
        with open(out_path, 'wb') as f:
            f.write(decrypted)
        if not self.cancelled:
            wx.CallAfter(self._finish_msg, f"Decrypted to {os.path.basename(out_path)}")

    def _set_progress_msg(self, msg):
        if self.progress_dialog and not self.cancelled:
            self.progress_dialog.Update(0, msg)

    def _finish_msg(self, msg):
        self.api.speak(msg)
        self.status_lbl.SetLabel(msg)
        self.status_lbl.SetForegroundColour(wx.Colour(180, 255, 180))

    def on_close(self, event=None):
        self.cancelled = True
        if self.frame:
            self.frame.Destroy()
        self.api.sounds.play("close")
        self.api.desktop.on_app_closed(self)
