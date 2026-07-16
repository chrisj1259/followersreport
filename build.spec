# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Instagram Follower Report desktop app.

Output:
    dist/InstagramFollowerReport/InstagramFollowerReport.exe   (windowed, no console)
    dist/InstagramFollowerReport/_internal/...                (DLLs, pywebview, run.py, ...)

Build:
    pip install pyinstaller pywebview
    pyinstaller build.spec

Notes:
    * --onedir (not --onefile) -- the installer ships the folder anyway, and
      --onedir removes the 5-second temp-dir extraction lag on each launch.
    * windowed -- no conhost window flashes at the user; pywebview owns the UI.
    * we explicitly bundle index.html as data (PyInstaller's Analysis picks
      up pure-Python imports of run automatically, but a runtime-read asset
      like index.html must be declared).
    * we don't declare sys.platform / os-specific hidden imports -- pywebview
      is the only third-party dep and its setup.py already covers Windows.
"""
import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('index.html', '.')],
    hiddenimports=[
        'run',
        # ponytail: pywebview defers the Windows backend (winforms) until
        # webview.start() runs and only on sys.platform == 'win32'. Static
        # PyInstaller analysis misses it. clr_loader + pythonnet pull in
        # the .NET bridge that winforms sits on top of. Without these the
        # bundle builds clean but `webview.start()` raises ModuleNotFoundError
        # at first window-create.
        'webview.platforms.winforms',
        'clr_loader',
        'pythonnet',
    ] + collect_submodules('webview'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ponytail: trim a few stdlib-heavy modules PyInstaller's safe defaults
        # still pull in. We don't use them, and they bloat the installer.
        'tkinter', 'test', 'unittest', 'pydoc', 'doctest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='InstagramFollowerReport',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # windowed -- no terminal window
    icon='icon.ico',         # app icon shown in taskbar / window title
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='InstagramFollowerReport',
)
