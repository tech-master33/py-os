import socket
import threading
import wx

class MessageService:
    def __init__(self, api):
        self.api = api
        self.port = 3030
        self.history = []
        self.running = False
        self.server_socket = None
        self.subscribers = []

    def start(self):
        if self.running:
            return
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.server_socket.bind(('', self.port))
            thread = threading.Thread(target=self.listen_thread, daemon=True)
            thread.start()
            print(f"Message Service started on port {self.port}")
        except Exception as e:
            print(f"Message Service failed to start: {e}")
            self.running = False

    def listen_thread(self):
        while self.running:
            try:
                data, addr = self.server_socket.recvfrom(1024)
                message = data.decode('utf-8')
                sender_ip = addr[0]
                formatted_msg = f"From {sender_ip}: {message}"
                self.history.append(formatted_msg)
                
                # Alert the user regardless of whether the app is open
                # We use CallAfter to ensure thread safety with speech/sounds if needed, 
                # though the speech engine should be thread-safe.
                self.api.speak(f"New message received: {message}", interrupt=False)
                self.api.play_sound("alert")
                
                # Notify active app subscribers
                for callback in self.subscribers:
                    wx.CallAfter(callback, formatted_msg)
            except Exception:
                if not self.running:
                    break

    def subscribe(self, callback):
        if callback not in self.subscribers:
            self.subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    def stop(self):
        self.running = False
        if self.server_socket:
            self.server_socket.close()
            self.server_socket = None
