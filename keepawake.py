"""
Keep the computer awake while it records: no sleep, no screen blanking, during a ride or a
morning check. Held only while needed, given back the moment the ride or check ends.

Linux: GNOME's session inhibitor (gnome-session-inhibit: idle + suspend, what GNOME's own power
settings obey) and logind's (systemd-inhibit: sleep + idle, for everything else), each a small
child process that holds the lock until it is stopped. Windows: SetThreadExecutionState, from the
thread that calls hold() (the app's engine thread, which lives as long as the app).
Standard library only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

REASON = "Assioma Recorder is recording"


class KeepAwake:
    def __init__(self, log=lambda text: None) -> None:
        self.log = log
        self.held = False
        self._procs: list = []

    def hold(self) -> None:
        if self.held:
            return
        self.held = True
        if os.name == "nt":
            import ctypes
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
        elif sys.platform.startswith("linux"):
            # the held command ends by itself if the app goes away (even a crash): no lock left behind
            watch = ["sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done"]
            cmds = []
            if shutil.which("gnome-session-inhibit"):
                cmds.append(["gnome-session-inhibit", "--app-id", "assioma-recorder", "--reason", REASON,
                             "--inhibit", "idle:suspend", *watch])
            if shutil.which("systemd-inhibit"):
                cmds.append(["systemd-inhibit", "--what=sleep:idle", "--who=Assioma Recorder", f"--why={REASON}",
                             "--mode=block", *watch])
            for c in cmds:
                try:
                    self._procs.append(subprocess.Popen(c, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                        stderr=subprocess.DEVNULL))
                except OSError as e:
                    self.log(f"keep awake: {c[0]}: {e}")
        self.log("keep awake: on (no sleep, no screen blanking while recording)")

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self._procs = []
        self.log("keep awake: off")
