# Building & releasing Et al.

The in-app updater (Tools → Check for updates) compares the running `__version__`
in `server.py` with the latest **GitHub Release** tag, then downloads the matching
asset and swaps it in. So a release must:

1. Have a tag equal to `__version__` (e.g. `__version__ = "0.2.0"` → tag `v0.2.0`).
2. Attach the per-OS asset(s) the updater looks for:
   - **Windows:** an asset whose name ends in **`.exe`** (e.g. `EtAl.exe`).
   - **macOS:** an asset whose name ends in **`.app.zip`** (e.g. `EtAl-macos.app.zip`).

## Shared AI key (do this once before building)

The bundled "on by default" Groq key is **not** in source (this repo is public). Put it
in a git-ignored `groq_key.txt` at the project root before building so it gets baked into
the app:

```bash
printf 'gsk_yourDedicatedKey' > groq_key.txt   # never commit this file
```

`llm.py` reads the key from `ETAL_GROQ_KEY`, else this file, else falls back to opt-in
(users paste their own key in Tools). Use a dedicated, rotatable key — never a personal one.

## Release checklist

1. Bump `__version__` in `server.py`.
2. Build the artifact(s):
   - **Windows** (on a Windows machine): `powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1` → `dist\EtAl.exe`
   - **macOS** (on a Mac): `bash scripts/build_macos.sh` → `dist/EtAl-macos.app.zip`
   - Each OS can only build its own artifact — PyInstaller doesn't cross-compile.
3. Publish the release (needs the `gh` CLI, `gh auth login`):
   `bash scripts/release.sh 0.2.0`
   …or do it in the GitHub web UI: **Releases → Draft a new release**, tag `v0.2.0`,
   drag the built files in, Publish.

That's it — installed copies on an older version will detect `v0.2.0` and self-update.

## Notes

- The Windows `.exe` needs the **Microsoft Edge WebView2 runtime** on the target
  machine (preinstalled on Windows 11; a bootstrapper is available from Microsoft for
  Windows 10).
- The unsigned macOS `.app` runs for personal use; for wider distribution, codesign +
  notarize. The updater strips the Gatekeeper quarantine flag from downloaded builds.
- First-time install on a new platform is manual (hand someone the `.exe`/`.app`);
  only subsequent updates go through the in-app button.
