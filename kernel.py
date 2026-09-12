import os
import datetime
import json
import subprocess
import threading
import platform
import shutil
from platform_support import get_shells
from app_paths import get_data_dir

class VirtualOS:
    def __init__(self, root_dir=None):
        self.root_dir = os.path.abspath(root_dir or os.path.join(get_data_dir(), "vfs"))
        self.cwd = "/"
        self.shell_proc = None
        self.shell_type = None
        self.output_callback = None
        self.platform_name = platform.system()
        if not os.path.exists(self.root_dir):
            legacy_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "vfs"))
            if root_dir is None and os.path.isdir(legacy_root):
                os.makedirs(os.path.dirname(self.root_dir), exist_ok=True)
                shutil.copytree(legacy_root, self.root_dir)
            else:
                os.makedirs(self.root_dir, exist_ok=True)
                self._create_default_files()

    def _create_default_files(self):
        with open(os.path.join(self.root_dir, "welcome.txt"), "w") as f:
            f.write("Welcome to BlindOS. This is a safe environment for you to explore.")
        os.makedirs(os.path.join(self.root_dir, "documents"))

    def get_real_path(self, virtual_path):
        # Very basic path resolution
        if virtual_path.startswith("/"):
            rel_path = virtual_path.lstrip("/")
        else:
            # Handle relative paths from current cwd
            current_abs_vpath = os.path.join(self.cwd, virtual_path)
            rel_path = os.path.normpath(current_abs_vpath).lstrip("/")
        
        return os.path.join(self.root_dir, rel_path)

    def _shell_reader(self):
        while self.shell_proc:
            try:
                line = self.shell_proc.stdout.readline()
                if not line:
                    break
                if self.output_callback:
                    self.output_callback(line.rstrip())
            except Exception:
                break
        self.shell_proc = None
        self.shell_type = None
        if self.output_callback:
            self.output_callback("Shell session ended.")

    def _available_shells(self):
        return get_shells()

    def _launch_shell(self, shell_type):
        shells = self._available_shells()
        if shell_type not in shells:
            available = ", ".join(shells) if shells else "none"
            return f"Unknown shell type: {shell_type}. Available shells: {available}."

        popen_kwargs = {
            "args": [shells[shell_type]],
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
        }
        if self.platform_name == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self.shell_proc = subprocess.Popen(**popen_kwargs)
        self.shell_type = shell_type
        threading.Thread(target=self._shell_reader, daemon=True).start()
        return f"Switched to {shell_type}. Type 'exit' to return to PyOS."

    def execute(self, command_str):
        if self.shell_proc:
            if command_str.lower().strip() == "exit":
                self.shell_proc.stdin.write("exit\n")
                self.shell_proc.stdin.flush()
                return "Exiting shell..."
            
            self.shell_proc.stdin.write(command_str + "\n")
            self.shell_proc.stdin.flush()
            return ""

        parts = command_str.lower().split()
        if not parts:
            return "No command entered."
        
        cmd = parts[0]
        args = parts[1:]

        if cmd == "help":
            available_shells = ", ".join(self._available_shells()) or "none"
            return (
                "Available commands: list, open, create, delete, where, time, exit, "
                "shutdown, reboot, shell. Available host shells: "
                f"{available_shells}."
            )
        
        elif cmd == "list":
            real_path = self.get_real_path(self.cwd)
            items = os.listdir(real_path)
            if not items:
                return "The directory is empty."
            return f"Directory contains {len(items)} items: " + ", ".join(items)

        elif cmd == "where":
            return f"You are currently in {self.cwd}"

        elif cmd == "time":
            now = datetime.datetime.now()
            return f"The current time is {now.strftime('%H:%M')}."

        elif cmd == "open":
            if not args:
                return "Please specify a file name to open."
            file_name = args[0]
            real_path = self.get_real_path(file_name)
            
            if os.path.isdir(real_path):
                # If it's a directory, change to it
                self.cwd = os.path.join(self.cwd, file_name).replace("\\", "/")
                return f"Opened directory {file_name}."
            
            if os.path.exists(real_path):
                with open(real_path, "r") as f:
                    content = f.read()
                return f"Reading {file_name}: {content}"
            else:
                return f"File {file_name} not found."

        elif cmd == "create":
            if not args:
                return "Please specify a name for the new file."
            file_name = args[0]
            real_path = self.get_real_path(file_name)
            with open(real_path, "w") as f:
                f.write("New file created by user.")
            return f"File {file_name} created successfully."

        elif cmd == "delete":
            if not args:
                return "Please specify a file name to delete."
            file_name = args[0]
            real_path = self.get_real_path(file_name)
            if os.path.exists(real_path):
                if os.path.isdir(real_path):
                    os.rmdir(real_path)
                else:
                    os.remove(real_path)
                return f"Deleted {file_name}."
            else:
                return f"Item {file_name} not found."

        elif cmd == "shutdown":
            return "Host shutdown is disabled from the PyOS simulator for safety."

        elif cmd == "reboot":
            return "Host restart is disabled from the PyOS simulator for safety."

        elif cmd in {"winshell", "shell"}:
            if not args or args[0] == "help":
                available_shells = ", ".join(self._available_shells()) or "none"
                return f"Shell usage: shell <type>. Available shells: {available_shells}."

            shell_type = args[0]

            try:
                return self._launch_shell(shell_type)
            except Exception as e:
                return f"Failed to launch {shell_type}: {e}"

        return f"Unknown command: {cmd}. Type help for a list of commands."
