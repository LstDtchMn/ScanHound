# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ScanHound backend sidecar.

Produces a single-folder executable that Tauri launches as a sidecar.
Output: dist/scanhound-backend/scanhound-backend.exe
"""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / 'backend' / 'api' / '__main__.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'config.example.json'), '.'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'backend',
        'backend.api',
        'backend.api.main',
        'backend.api.ws',
        'backend.api.dependencies',
        'backend.api.routes',
        'backend.api.routes.system',
        'backend.api.routes.scanner',
        'backend.api.routes.results',
        'backend.api.routes.downloads',
        'backend.api.routes.settings',
        'backend.api.routes.sources',
        'backend.api.routes.plex',
        'backend.app_service',
        'backend.scanner_service',
        'backend.download_service',
        'backend.database',
        'backend.config',
        'backend.plex_service',
        'backend.notification_bridge',
        'backend.watchlist',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtWidgets',
        'tkinter',
        'matplotlib',
        'numpy',
        'PIL',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='scanhound-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='scanhound-backend',
)
