# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).parent
icon_suffix = ".icns" if sys.platform == "darwin" else ".ico"
icon_path = project_root / "packaging" / f"LandPlotFinder{icon_suffix}"
icon = str(icon_path) if icon_path.exists() else None
hidden_imports = collect_submodules("sqlalchemy.dialects.sqlite") + [
    "tkinter",
    "tkinter.messagebox",
    "uvicorn.lifespan.on",
    "uvicorn.protocols.http.h11_impl",
]

analysis = Analysis(
    [str(project_root / "app" / "desktop.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "app" / "static"), "app/static")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "google",
        "googleapiclient",
        "httplib2",
        "PIL",
        "psycopg",
        "psycopg_binary",
        "pytest",
        "ruff",
        "uvloop",
        "watchfiles",
        "websockets",
    ],
    noarchive=False,
    optimize=1,
)

python_bundle = PYZ(analysis.pure)

executable = EXE(
    python_bundle,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="LandPlotFinder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=icon,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LandPlotFinder",
)

if sys.platform == "darwin":
    application = BUNDLE(
        bundle,
        name="LandPlotFinder.app",
        icon=icon,
        bundle_identifier="by.landplotfinder.desktop",
        info_plist={
            "CFBundleDisplayName": "LandPlotFinder",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
