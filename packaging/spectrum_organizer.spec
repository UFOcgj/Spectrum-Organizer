# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from shutil import copyfile


ROOT = Path(SPEC).resolve().parents[1]

a = Analysis(
    [str(ROOT / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "assets" / "spectrum-organizer.png"), "assets"),
        (
            str(ROOT / "src" / "spectrum_organizer" / "origin" / "origin_current_pid.c"),
            "spectrum_organizer/origin",
        ),
    ],
    hiddenimports=[
        "originpro",
        "win32timezone",
        "spectrum_organizer.origin.extraction_process",
        "spectrum_organizer.origin.output_process",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "validation"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Spectrum Organizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "spectrum-organizer.ico"),
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Spectrum Organizer",
)

copyfile(
    ROOT / "packaging" / "README.txt",
    Path(DISTPATH) / "Spectrum Organizer" / "README.txt",
)
