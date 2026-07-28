; Inno Setup script for Instagram Follower Report.
;
; Build:
;   1. pip install pyinstaller pywebview
;   2. pyinstaller build.spec           -> dist/InstagramFollowerReport/
;   3. Install Inno Setup 6.x (https://jrsoftware.org/isdl.php) -- free, ~5MB.
;   4. Open installer.iss in Inno Setup Compiler, Build > Compile.
;   5. Output/InstagramFollowerReport-Setup-v<version>.exe is the file you ship.
;
; What it does at install time:
;   * Program files (read-only) go to %PROGRAMFILES%\InstagramFollowerReport.
;   * Start Menu shortcut: "Instagram Follower Report" (link to the .exe).
;   * Optional Desktop shortcut (un-checked by default).
;   * Real uninstaller registered with Windows.
;   * If Microsoft WebView2 runtime is missing, silently download + install
;     the Evergreen Bootstrapper before continuing. Win11 ships WebView2 by
;     default; Win10 users get a one-time ~80MB download.
;   * [Run] entry launches the freshly-installed app immediately after the
;     wizard finishes, so the user gets the same "double-click the .exe, watch
;     the app pop up" feel they have from every other Windows app installer.
;
; ponytail: dual machine-install (HKLM + HKCU probing) because the WebView2
; Evergreen runtime is unusual -- it can land in either hive depending on how
; the user (or their IT shop) originally installed Edge.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppName=Instagram Follower Report
AppVersion={#AppVersion}
AppPublisher=Local-only
DefaultDirName={autopf}\InstagramFollowerReport
DefaultGroupName=Instagram Follower Report
OutputBaseFilename=InstagramFollowerReport-Setup-v{#AppVersion}
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
AppId={{B0F3153C-2C9B-4B0A-9F8E-INSTAGRAMREPORT}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The PyInstaller --onedir output: the .exe, _internal/, runtime DLLs, etc.
Source: "dist\InstagramFollowerReport\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

; ponytail: WebView2 evergreen bootstrapper, embedded so corporate-proxy /
; offline installs work without a runtime URL fetch.
; ExtractTemporaryFile pulls it out of the installer into {tmp} only when
; InitializeSetup detects WebView2 is missing; deleteafterinstall cleans
; it back out when the install completes so we don't leave an ~80MB EXE
; sitting in the user's temp dir. build.py downloads this from
; go.microsoft.com before invoking ISCC.
Source: "redist\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Run]
; ponytail: standard "normal Windows app" install experience -- launch the
; app once the wizard finishes. The Description string becomes the
; final-screen checkbox label; default ON so real users get the
; "double-click the .exe, watch the app pop up" feel they're used to from
; every other Windows installer.
;   postinstall  -- run only after files are committed to {app}, not before
;   nowait       -- don't hang the installer waiting for the launched window
;   skipifsilent -- skip when /VERYSILENT (test_install.ps1 uses this for CI)
; {app} resolves to %ProgramFiles%\InstagramFollowerReport on x64 installs --
; which is exactly where the recursesubdirs [Files] entry above places
; InstagramFollowerReport.exe.
Filename: "{app}\InstagramFollowerReport.exe"; Description: "Launch Instagram Follower Report when the install finishes"; Flags: nowait postinstall skipifsilent

[Icons]
Name: "{group}\Instagram Follower Report"; Filename: "{app}\InstagramFollowerReport.exe"
Name: "{commondesktop}\Instagram Follower Report"; Filename: "{app}\InstagramFollowerReport.exe"; Tasks: desktopicon

[Registry]
; The app reads this instead of guessing from files, so it only offers a newer release.
Root: HKCU; Subkey: "Software\InstagramFollowerReport"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"; Flags: uninsdeletevalue

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

; ponytail: user data lives in %APPDATA%\InstagramFollowerReport (the
; .installed marker ensures we never sweep that on uninstall -- the user
; might reinstall later and expect their snapshots to still be there).

[Code]
function IsWebView2Installed: Boolean;
var
  RegKey: String;
  Value: String;
begin
  Result := False;
  // WebView2 Evergreen Runtime Client GUID is stable across versions.
  RegKey := 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  if RegQueryStringValue(HKLM, RegKey, 'pv', Value) then
    Result := True
  else if RegQueryStringValue(HKCU, RegKey, 'pv', Value) then
    Result := True;
end;

function InitializeSetup: Boolean;
var
  ResultCode: Integer;
  BootPath: String;
begin
  Result := True;
  // Skip the whole dance if WebView2 is already on the box.
  if IsWebView2Installed then
    exit;
  if MsgBox('Microsoft WebView2 runtime is required for Instagram Follower Report.' + #13#10 +
            'It was not detected on this PC.' + #13#10#13#10 +
            'Install WebView2 now?',
            mbConfirmation, MB_YESNO) <> IDYES then
  begin
    if MsgBox('WebView2 is required to run the app. Continue installing anyway?',
              mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
    exit;
  end;
  // ponytail: try the bundled bootstrapper first. ExtractTemporaryFile is
  // a no-op when Source: "redist\..." is not declared in [Files] (e.g. the
  // developer skipped `python build.py` and is hand-running ISCC without
  // redist/ present), so we test FileExists after. deleteafterinstall
  // cleans the ~80MB EXE out of {tmp} when the install completes.
  BootPath := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if FileExists(BootPath) then
  begin
    if not Exec(BootPath, '/silent /install', '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
    begin
      MsgBox('The bundled WebView2 bootstrapper did not launch successfully.' + #13#10 +
             'Setup will continue, but Instagram Follower Report may not' + #13#10 +
             'run until you install WebView2 from microsoft.com/edge.',
             mbInformation, MB_OK);
    end;
    exit;
  end;
  // Fallback: bootstrapper wasn't bundled. Launch the Microsoft download
  // URL in the user's default browser. ShellExec (not Exec) is Inno's
  // URL-launch primitive; Exec would try to spawn a literal `httpsgo.exe`
  // from PATH and fail. First arg '' means "not a command, treat as a URL"
  // so the OS's default browser handler runs instead.
  if not ShellExec('', 'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
                   '', '', SW_SHOWNORMAL, ewNoWait, ResultCode) then
  begin
    MsgBox('Could not open the WebView2 download page in your browser.' + #13#10 +
           'Install WebView2 manually from https://developer.microsoft.com/' + #13#10 +
           'microsoft-edge/webview2/ before retrying, otherwise the app' + #13#10 +
           'will install but may not launch on first run.',
           mbInformation, MB_OK);
  end;
end;
