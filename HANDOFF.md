# Instagram Follower Report — Handoff Document

> Generated: 2026-07-14
> Project root: `D:\vscode\removedfollowers`
> Last installer: `Output/InstagramFollowerReport-Setup-v1.0.exe`

---

## 1. Project Overview

This is a local Instagram follower/following analyzer with two delivery modes:

1. **Web app / CLI**: `python run.py [port]` serves the UI at `http://localhost:8000`.
2. **Windows desktop app**: PyInstaller + Inno Setup installer that bundles the same HTML/JS/CSS UI inside a pywebview window.

User data lives in `data/<MM-DD-YYYY>/` (JSON exports from Instagram). The analyzer writes `dist/report.json`. The desktop app redirects writes to `%APPDATA%\InstagramFollowerReport\`.

---

## 2. Recent Work (last session)

### 2.1 Feature: "People who Unfollowed me that I never followed"

- **run.py**: Computes a global `unilateral_unfollowed` list across **all** snapshots:
  - `ever_followers` — anyone who ever followed you
  - `latest_followers` — current followers
  - `ever_following` — anyone you ever followed back
  - Result = `ever_followers - latest_followers - ever_following`
- **index.html**: Renders a collapsible disclosure section (mirrors the disabled-accounts panel).
- **smoke.py**: Asserts the feature works.

### 2.2 UI Polish

- Capitalized page title to "Follower Report".
- Changed unilateral-section accent from purple to orange.
- Made all stat-card left accents orange.
- Made NFB panel count badge orange.
- Disabled section title forced white.

### 2.3 Bug: Disabled section title rendering black in compiled app

**Root cause**: WebView2/Edge Auto Dark Mode. When Windows is in Dark Mode, WebView2 inverts light page colors at the paint layer. The dark `#222` disabled section triggered the heuristic, so the white `#fff` title text was inverted to black.

**Fix applied**:
- Added to `<head>` of `index.html`:
  ```html
  <meta name="color-scheme" content="light">
  ```
  This opts the page out of Auto Dark Mode.
- Defense-in-depth kept in place:
  - Inline style on disabled section h2: `style="color:#fff !important"`
  - Wrapped title text in `<span style="color:#fff !important">`
  - Cache-busting URL in `app.py`: `http://127.0.0.1:{port}/?v=2`
  - `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` headers added in `run.py` for all responses.

### 2.4 Icon support

- `build.spec`: `icon='icon.ico'` embeds the icon in the .exe.
- `installer.iss`: `SetupIconFile=icon.ico` sets the installer icon.
- User must create `icon.ico` in the project root (multi-size: 16, 32, 48, 256 px).

---

## 3. File Map

| File | Purpose |
|------|---------|
| `run.py` | Analyze pipeline + HTTP server (`FlagHandler`) + CLI |
| `index.html` | Single-page UI (stats, NFB, DFMB, unilateral, disabled, manage snapshots, import) |
| `app.py` | Desktop entry point: bootstrap %APPDATA%, start server, open pywebview window |
| `build.spec` | PyInstaller spec (onedir, windowed, icon) |
| `installer.iss` | Inno Setup installer script |
| `build.py` | Orchestrates: download WebView2 bootstrapper → PyInstaller → Inno Setup |
| `smoke.py` | End-to-end smoke test |
| `data/` | User snapshot folders (`MM-DD-YYYY`) and `disabledaccounts.json` |
| `dist/report.json` | Generated report (latest snapshot + weeks + unilateral list) |

---

## 4. Build & Test Commands

```bash
# Run smoke test
python smoke.py

# Create icon.ico in the project root (16, 32, 48, 256 px) before building.
# Without it, PyInstaller/Inno Setup will fail.

# Build desktop installer
python build.py
# Output: Output/InstagramFollowerReport-Setup-v1.0.exe
```

---

## 5. Known Issues / Watchouts

1. **WebView2 Auto Dark Mode**: Fixed by `<meta name="color-scheme" content="light">`. If the user ever wants a real dark mode, this meta tag must be removed/replaced.
2. **Installer caching / old version still running**: The compiled app extracts to a temp dir, but WebView2 can cache pages. The `?v=2` query param and cache-control headers mitigate this. If a user still sees old UI, they must fully uninstall and delete `C:\Program Files\InstagramFollowerReport` before reinstalling.
3. **Icon file missing**: `icon.ico` does not exist in the repo. Build will fail until the user creates it.
4. **PyInstaller warnings**: Non-critical warnings about `webview.platforms.android` and `pycparser` hidden imports are expected on Windows.

---

## 6. Next Steps for Continuing Agent

- Verify the disabled section title is white in both web app and compiled app after a fresh install.
- Consider adding a real dark mode toggle (would require removing the hard-coded `color-scheme: light` meta tag and using CSS variables).
- Add unit tests for the unilateral_unfollowed computation.
- Create the `icon.ico` file and verify it appears in the taskbar/installer.

---

## 7. Key Code Snippets

### Disabled section (index.html)
```html
<section id="disabled-section" class="disabled-section" hidden data-collapsed="true">
  <h2 style="color:#fff !important">
    <span style="color:#fff !important">Likely disabled / deactivated</span>
    <span class="count" id="disabled-count">0</span>
  </h2>
  ...
</section>
```

### Color-scheme meta tag (index.html `<head>`)
```html
<meta name="color-scheme" content="light">
```

### Cache-busting URL (app.py)
```python
url = f"http://127.0.0.1:{port}/?v=2"
```

### Cache-Control headers (run.py)
```python
def end_headers(self) -> None:
    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    super().end_headers()
```

---

## 8. Contact / Context

This handoff was generated after multiple rounds of debugging the disabled-section title color in the compiled Windows app. The final root cause was WebView2 Auto Dark Mode, and the fix is the `color-scheme` meta tag plus the defensive inline styles and cache-busting described above.
