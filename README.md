# Fembimmo Display — Windows portable EXE build

This package builds `FembimmoDisplay.exe` and places the Python runtime and Python/Qt dependencies alongside it. The receiving PC does not need Python or pip installed. **Share the whole `FembimmoDisplay` folder (or ZIP it), not just the EXE.**

The build uses 64-bit Python 3.11 because native Qt packages need a compatible Windows wheel. The app itself is intended to use its Qt WebEngine backend on Windows.

## Option A — build on a Windows PC

1. Install **64-bit Python 3.11** from <https://www.python.org/downloads/windows/> if it is not already installed. Enable the Python Launcher.
2. Extract this folder to a writable location.
3. Double-click `build_windows.bat`.
4. When it succeeds, launch `dist\FembimmoDisplay\FembimmoDisplay.exe`.
5. To move it to another PC, ZIP and share the entire `dist\FembimmoDisplay` folder.

The first build requires an internet connection to install packages. The QtWebEngine dependencies make the bundle large.

## Option B — build on GitHub's Windows runner (avoids local CPU/Python wheel issues)

This route builds on a clean Windows x64 machine in the cloud, so you do not need Python or PyInstaller installed locally.

1. Create a new repository on GitHub (a private repository is fine).
2. Upload `main.py`, `requirements-build.txt`, and `.github/workflows/build-windows.yml`, preserving the `.github/workflows` path, then commit the files.
3. Open the repository's **Actions** tab and select **Build Fembimmo Display Windows EXE**.
4. Click **Run workflow** and wait for the workflow run to finish successfully.
5. Open that completed run and download the `FembimmoDisplay-Windows-x64` artifact.
6. Extract the downloaded ZIP and run `FembimmoDisplay.exe` from the extracted `FembimmoDisplay` folder.

GitHub may require you to enable Actions for a new repository. A workflow build normally takes several minutes because QtWebEngine is large.

## What's included and what still downloads

- Bundled by PyInstaller: Python runtime, pywebview, Qt/Qt WebEngine, and the listed Python packages.
- Still downloaded by the app, when required: FFmpeg, yt-dlp, Deno, and Cloudflared. Those downloads need internet access on first use. Cloudflare upload/remote links and online media services also need internet access.
- Optional Office-to-PDF conversion still requires LibreOffice to be installed on the target PC.

This creates a **portable app folder**, not a traditional Windows installer. The build currently keeps a console window open so startup errors and download progress are visible.
