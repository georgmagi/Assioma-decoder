"""
Builds "Assioma Recorder.exe" next to this file: one file, no console, its own icon, so it can be
pinned to the taskbar and the Start menu. Rides, settings and the pedal address stay in this folder
(next to the .exe), exactly where start_app.bat keeps them.

    py -3 build_exe.py            (needs: pip install pyinstaller pillow)
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "Assioma Recorder"
ICON = os.path.join(HERE, "web", "icon.ico")


def make_icon(path: str) -> None:
    """A power-phase ring like the app's: grey stroke, blue power phase, yellow peak, on a dark tile."""
    from PIL import Image, ImageDraw
    S = 1024
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill="#12151a", outline="#2a3039", width=18)
    box = (165, 165, S - 165, S - 165)
    w = 150
    d.arc(box, 0, 360, fill="#232830", width=w)                    # the whole pedal stroke
    d.arc(box, -90 - 15, -90 + 200, fill="#5b9dff", width=w)       # power phase: 345° to 200° (0° = top)
    inner = tuple(v + (1 if i < 2 else -1) * w * 0.62 for i, v in enumerate(box))
    d.arc(inner, -90 + 70, -90 + 125, fill="#ffb020", width=int(w * 0.62))   # peak
    im.save(path, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


def main() -> None:
    if not os.path.exists(ICON):
        make_icon(ICON)
    import PyInstaller.__main__
    work = os.path.join(HERE, "build")
    PyInstaller.__main__.run([
        os.path.join(HERE, "recorder_app.py"), "--name", NAME, "--onefile", "--windowed", "--noconfirm",
        "--icon", ICON, "--add-data", f"{os.path.join(HERE, 'web')}{os.pathsep}web",
        "--collect-all", "bleak", "--collect-all", "webview", "--collect-all", "libusb",
        "--collect-submodules", "winrt", "--collect-all", "garmin_fit_sdk", "--hidden-import", "ant_hr", "--hidden-import", "vt1", "--hidden-import", "eftp",
        "--distpath", HERE, "--workpath", work, "--specpath", work,
    ])
    shutil.rmtree(work, ignore_errors=True)
    print(f"built {os.path.join(HERE, NAME + '.exe')}")


if __name__ == "__main__":
    sys.exit(main())
