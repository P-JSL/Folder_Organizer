"""New native Windows checks for the changed 0.7 Python packaging policy.

Executes the final bundled program, decodes supported image variants through its
real CLI and captures its real visible Qt Widgets window through Win32. All data
is disposable. The previous test suites and previous GUI checks are not invoked.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def offline_updates():
    """Temporarily disable the one native QSettings value, then restore it."""
    import winreg
    key_name = r"Software\P-JSL\FolderOrganizer\updates"
    missing = object()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_name, access=winreg.KEY_ALL_ACCESS) as key:
        try:
            previous = winreg.QueryValueEx(key, "enabled")
        except FileNotFoundError:
            previous = missing
        winreg.SetValueEx(key, "enabled", 0, winreg.REG_SZ, "false")
        try:
            yield
        finally:
            if previous is missing:
                winreg.DeleteValue(key, "enabled")
            else:
                winreg.SetValueEx(key, "enabled", 0, previous[1], previous[0])


def main_window(process: subprocess.Popen, timeout: float = 30.0) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Packaged Widgets process exited before showing its window: {process.returncode}")
        found = []
        @callback_type
        def each(hwnd, _parameter):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process.pid and user32.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(1024)
                user32.GetWindowTextW(hwnd, title, len(title))
                if title.value.startswith("Folder Organizer"):
                    found.append(int(hwnd))
            return True
        user32.EnumWindows(each, 0)
        if found:
            return found[0]
        time.sleep(0.1)
    raise RuntimeError("The bundled program did not create a visible native Widgets main window")


def capture_window(hwnd: int, destination: Path) -> dict:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    rectangle = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
        raise ctypes.WinError(ctypes.get_last_error())
    width, height = rectangle.right - rectangle.left, rectangle.bottom - rectangle.top
    if not (640 <= width <= 4096 and 400 <= height <= 4096):
        raise RuntimeError(f"Unexpected native main window dimensions: {width}x{height}")
    screen = user32.GetWindowDC(hwnd)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(memory, bitmap)
    try:
        # Qt Widgets paint into a raster backing store, so WM_PRINT can capture
        # the actual rendered content even on a CI runner with no physical GPU.
        if not user32.PrintWindow(hwnd, memory, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        gdi32.SelectObject(memory, old)
        old = None
        class Header(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                        ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                        ("image_size", wintypes.DWORD), ("x_pixels", wintypes.LONG),
                        ("y_pixels", wintypes.LONG), ("used", wintypes.DWORD), ("important", wintypes.DWORD)]
        header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
            raise ctypes.WinError(ctypes.get_last_error())
        image = Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
        colors = image.resize((160, 100)).getcolors(16000)
        count = len(colors) if colors is not None else 16001
        if count < 16:
            raise RuntimeError("Native window capture lacks rendered Widgets content")
        image.save(destination)
        return {"width": width, "height": height, "sampled_colors": count,
                "capture": destination.name, "native_visible_window": True}
    finally:
        if old is not None:
            gdi32.SelectObject(memory, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, screen)


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("The changed package must be exercised on native Windows")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "dist/FolderOrganizer")
    args = parser.parse_args()
    package = args.directory.resolve()
    program = package / "FolderOrganizer.exe"
    marker = json.loads((package / "version.json").read_text(encoding="utf-8"))
    files = {path.relative_to(package).as_posix().casefold() for path in package.rglob("*") if path.is_file()}
    if any("/qml/" in name or name.endswith(("designer.exe", "linguist.exe", "assistant.exe")) for name in files):
        raise RuntimeError("The Widgets build still contains unused QML/development tools")
    if not any(name.endswith("/platforms/qwindows.dll") for name in files):
        raise RuntimeError("Native Windows Qt platform plugin is missing")
    if not any(name.endswith("/opengl32sw.dll") for name in files):
        raise RuntimeError("The Windows software-rendering fallback was removed")
    with tempfile.TemporaryDirectory(prefix="organizer-widgets-v070-") as temporary:
        scratch = Path(temporary)
        fixture = scratch / "이미지 코덱·폴더 검사"
        fixture.mkdir()
        pattern = Image.new("RGB", (96, 64), "#efb841")
        draw = ImageDraw.Draw(pattern)
        draw.rectangle((5, 8, 45, 39), fill="#265c8d")
        draw.ellipse((49, 12, 85, 52), fill="#df5340")
        for suffix, format_name in (("png", "PNG"), ("jpg", "JPEG"), ("bmp", "BMP"),
                                    ("gif", "GIF"), ("webp", "WEBP"), ("tif", "TIFF")):
            pattern.save(fixture / f"사진.{suffix}", format=format_name)
        (fixture / "사진 사본.png").write_bytes((fixture / "사진.png").read_bytes())
        for name in ("원문.txt", "사본.txt"):
            (fixture / name).write_text("새 패키지의 실제 바이트 중복 검사\n", encoding="utf-8")
        original = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.iterdir()}
        report_path = scratch / "읽기 전용 검사.json"
        environment = {**os.environ, "LOCALAPPDATA": str(scratch / "LocalAppData"),
                       "APPDATA": str(scratch / "AppData"), "QT_QPA_PLATFORM": "windows"}
        result = subprocess.run([str(program), "--cli", str(fixture), "--duplicates", "--similar",
                                 "--similar-method", "image", "--image-threshold", "0.6", "--max-pairs", "100",
                                 "--json", str(report_path)], timeout=90, env=environment,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(f"New bundled image/CLI fixture failed: {result.returncode}: {result.stderr}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if (report["app_version"] != marker["version"]
                or len(report["files"]) != 9 or len(report["duplicates"]) != 2):
            raise RuntimeError("New bundled image/CLI fixture lost files or exact groups")
        image_names = {Path(path).name for group in report["similar"]
                       if group["kind"] == "similar_image" for path in group["paths"]}
        expected_formats = {f"사진.{suffix}" for suffix in ("png", "jpg", "bmp", "gif", "webp", "tif")}
        if not expected_formats <= image_names:
            raise RuntimeError(f"A supported bundled image decoder is unavailable: {expected_formats - image_names}")
        if original != {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.iterdir()}:
            raise RuntimeError("The package check modified an original fixture")
        with offline_updates():
            process = subprocess.Popen([str(program)], cwd=scratch, env=environment)
            try:
                hwnd = main_window(process)
                time.sleep(0.5)
                screenshot = ROOT / "dist/PYTHON_PACKAGE_GUI.png"
                capture = capture_window(hwnd, screenshot)
                ctypes.windll.user32.PostMessageW(wintypes.HWND(hwnd), 0x0010, 0, 0)
                process.wait(timeout=15)
                if process.returncode:
                    raise RuntimeError(f"Native Widgets exit failed: {process.returncode}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        proof = {"edition": "Python", "version": marker["version"], "fixture_files": 9,
                 "exact_groups": 2, "decoded_image_formats": sorted(expected_formats),
                 "source_bytes_unchanged": True, "gui": capture,
                 "previously_passed_suites": "not rerun"}
        (ROOT / "dist/PYTHON_PACKAGE_CHECK.json").write_text(
            json.dumps(proof, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(proof, ensure_ascii=True))


if __name__ == "__main__":
    main()
