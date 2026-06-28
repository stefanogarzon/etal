# -*- mode: python ; coding: utf-8 -*-


import os as _os
_datas = [('frontend', 'frontend'), ('packs', 'packs'), ('icons', 'icons'),
          ('fields.yaml', '.')]
# Bundle the shared Groq key if present (git-ignored; placed by the maintainer).
if _os.path.exists('groq_key.txt'):
    _datas.append(('groq_key.txt', '.'))

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Et al.',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icons/etal-main.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Et al.',
)
app = BUNDLE(
    coll,
    name='Et al..app',
    icon='icons/etal-main.icns',
    bundle_identifier=None,
)
