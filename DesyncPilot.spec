# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Desync Pilot.

Builds a one-dir Windows bundle that includes:
  - all Python sources (app/), including app/strategy_parser.py so the
    updater can re-parse a new upstream release in-process
  - the bundled bin/ (winws.exe, WinDivert, .bin data files)
  - lists/ and utils/targets.txt
  - strategies.json
"""
import os

block_cipher = None

ROOT = os.path.abspath(SPECPATH)

a = Analysis(
    ['app\\main.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[
        ('bin', 'bin'),
        ('lists', 'lists'),
        ('utils', 'utils'),
        ('strategies.json', '.'),
        ('strategies_gui.json', '.'),
        ('README.md', '.'),
    ],
    hiddenimports=['PyQt6', 'PyQt6.QtSvg', 'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'difflib'],
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
    name='DesyncPilot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DesyncPilot',
)
