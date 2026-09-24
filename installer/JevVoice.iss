; Jev Voice Control setup. Build with installer\build.ps1, which stages the files this script packs.
; Per-user install: no admin rights, the app goes in %LOCALAPPDATA%\Programs\Jev Voice Control and its
; key, settings, logs and speech models in %LOCALAPPDATA%\Jev Voice Control (see voice_control\paths.py).
;
; Unattended (what installer\install.ps1 runs):
;   JevVoiceSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART [/TASKS="desktopicon,startup,gpu"]
;                     [/KEYFILE="path\.env" | /KEY=sk-or-...] [/LOG="setup.log"]
; Without /KEY or /KEYFILE, Setup keeps a saved key, else uses the first key it finds in the environment or in
; .env files in the usual project folders (the same search as `python -m voice_control.configure find-keys`).

#define AppName "Jev Voice Control"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define Stage "build\stage"
#define Art "build\art"
#define DataDir "{localappdata}\Jev Voice Control"
#define OpenRouterKeysUrl "https://openrouter.ai/keys"
#define TypeSafeKeysUrl "https://console.typesafe.ai/"
; GpuSize and GpuWheelArgs, plus [Code] for AddGpuDownloads and GpuAlreadyInstalled, pinned by build.ps1.
#include "build\gpu.iss"

[Setup]
AppId={{6CBE9AD2-A5C6-4961-8BD2-DFAEAEFAF4C3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Jev
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern dynamic
; The same art in light and dark mode (the dark panel suits both; dynamic mode would otherwise swap in stock art).
WizardImageFile={#Art}\wizard.png
WizardImageFileDynamicDark={#Art}\wizard.png
WizardSmallImageFile={#Art}\wizard-small.png
WizardSmallImageFileDynamicDark={#Art}\wizard-small.png
WizardImageBackColor=#16161b
WizardImageBackColorDynamicDark=#16161b
SetupIconFile=..\voice_control\assets\jev-voice-logo.ico
UninstallDisplayIcon={app}\JevVoice.exe
UninstallDisplayName={#AppName}
SetupMutex=JevVoiceControlSetup
OutputDir=dist
OutputBaseFilename=JevVoiceSetup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nHold Right Ctrl, say what you want, and Jev clicks, types and scrolls in the app in front of you.%n%nYou'll need an OpenRouter or TypeSafe AI API key. If one is already on this PC, Setup will find it.
FinishedLabel=Jev Voice is installed.%n%nLook for the small pill at the bottom of your screen and the Jev icon in the system tray. Hold Right Ctrl, speak, and let go.%n%nThe first start downloads the speech model, which can take a few minutes.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Jev Voice automatically when I sign in"; GroupDescription: "Shortcuts:"
Name: "gpu"; Description: "Use my NVIDIA graphics card for faster, more accurate speech recognition (downloads about {#GpuSize})"; GroupDescription: "Speech recognition:"; Check: HasNvidiaGpu

[InstallDelete]
; A clean runtime and app on every install, so upgrades never mix package versions.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\voice_control"
Type: filesandordirs; Name: "{app}\gpu"; Tasks: not gpu

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\JevVoice.exe"; WorkingDir: "{app}"; Comment: "Push-to-talk control for Windows apps"; AppUserModelID: "Jev.VoiceControl"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\JevVoice.exe"; WorkingDir: "{app}"; Comment: "Push-to-talk control for Windows apps"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\JevVoice.exe"; WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{app}\runtime\python.exe"; Parameters: "-E -s -m pip install --disable-pip-version-check --no-index --no-deps --no-compile --upgrade --target ""{app}\gpu"" {#GpuWheelArgs}"; StatusMsg: "Setting up GPU speech recognition..."; Flags: runhidden; Check: GpuDownloaded
Filename: "{app}\JevVoice.exe"; Description: "Start Jev Voice now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Byte-code caches and the GPU libraries are created after install, so they aren't in the uninstall log.
Type: filesandordirs; Name: "{app}"

[Code]
var
  KeyPage: TWizardPage;
  KeyEdit: TPasswordEdit;
  ShowKeyCheck: TNewCheckBox;
  FoundCombo: TNewComboBox;
  DetectedText: TNewStaticText;
  DownloadPage: TDownloadWizardPage;
  GpuReady: Boolean;
  FoundKeys, FoundSources: TArrayOfString;
  KeySource, SourceKey: String;  { where the key in the box came from, while the box still holds SourceKey }

function HasNvidiaGpu: Boolean;
begin
  { The NVIDIA driver installs the CUDA driver library; the app still tests the GPU and falls back to the CPU. }
  Result := FileExists(ExpandConstant('{sys}\nvcuda.dll'));
end;

function GpuDownloaded: Boolean;
begin
  Result := GpuReady;
end;

function KeyFile(const Name: String): String;
begin
  Result := ExpandConstant('{#DataDir}\') + Name;
end;

function SavedKeyExists: Boolean;
begin
  Result := FileExists(KeyFile('.env.openrouter')) or FileExists(KeyFile('.env.typesafe'));
end;

function IsOpenRouterKey(const Key: String): Boolean;
begin
  Result := Pos('sk-or-', Key) = 1;
end;

function Provider(const Key: String): String;
begin
  if IsOpenRouterKey(Key) then Result := 'OpenRouter' else Result := 'TypeSafe AI';
end;

function Masked(const Key: String): String;
begin
  if Length(Key) > 12 then
    Result := Copy(Key, 1, 6) + '...' + Copy(Key, Length(Key) - 3, 4)
  else
    Result := '...' + Copy(Key, Length(Key) - 1, 2);
end;

{ ---- Finding a key that is already on this PC (mirrors voice_control\configure.py) ---- }

{ The key on a .env line: OPENROUTER_API_KEY / TYPESAFE_API_KEY, or any value that is an OpenRouter key. }
function KeyFromLine(Line: String): String;
var
  Name, Value, Quote: String;
  P: Integer;
begin
  Result := '';
  Line := Trim(Line);
  if Pos('export ', Line) = 1 then
    Line := Trim(Copy(Line, 8, MaxInt));
  P := Pos('=', Line);
  if (P = 0) or (Copy(Line, 1, 1) = '#') then
    exit;
  Name := Trim(Copy(Line, 1, P - 1));
  Value := Trim(Copy(Line, P + 1, MaxInt));
  Quote := Copy(Value, 1, 1);
  if (Quote = '"') or (Quote = '''') then begin
    Value := Copy(Value, 2, MaxInt);
    P := Pos(Quote, Value);
    if P > 0 then
      Value := Copy(Value, 1, P - 1);
  end else begin
    P := Pos(' #', Value);
    if P > 0 then
      Value := Trim(Copy(Value, 1, P - 1));
  end;
  if (Value <> '') and (Pos(' ', Value) = 0) and
     ((Name = 'OPENROUTER_API_KEY') or (Name = 'TYPESAFE_API_KEY') or IsOpenRouterKey(Value)) then
    Result := Value;
end;

procedure AddFoundKey(const Key, Source: String);
var
  I: Integer;
begin
  if Key = '' then
    exit;
  for I := 0 to GetArrayLength(FoundKeys) - 1 do
    if FoundKeys[I] = Key then
      exit;
  I := GetArrayLength(FoundKeys);
  SetArrayLength(FoundKeys, I + 1);
  SetArrayLength(FoundSources, I + 1);
  FoundKeys[I] := Key;
  FoundSources[I] := Source;
end;

procedure ScanEnvFile(const Path: String);
var
  Lines: TArrayOfString;
  I: Integer;
begin
  if FileExists(Path) and LoadStringsFromFile(Path, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      AddFoundKey(KeyFromLine(Lines[I]), Path);
end;

procedure ScanFolder(const Dir: String);
begin
  ScanEnvFile(Dir + '\.env');
  ScanEnvFile(Dir + '\.env.local');
  ScanEnvFile(Dir + '\.env.openrouter');
  ScanEnvFile(Dir + '\.env.typesafe');
end;

{ A folder and its immediate subfolders: one project deep. }
procedure ScanProjects(const Dir: String);
var
  FindRec: TFindRec;
  Count: Integer;
begin
  if (Dir = '') or not DirExists(Dir) then
    exit;
  ScanFolder(Dir);
  Count := 0;
  if FindFirst(Dir + '\*', FindRec) then
    try
      repeat
        if ((FindRec.Attributes and $10) <> 0) and (FindRec.Name <> '.') and (FindRec.Name <> '..') and
           (Copy(FindRec.Name, 1, 1) <> '$') and (CompareText(FindRec.Name, 'node_modules') <> 0) then begin
          ScanFolder(Dir + '\' + FindRec.Name);
          Count := Count + 1;
        end;
      until (Count >= 400) or not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
end;

procedure FindKeys;
var
  Home, Docs: String;
begin
  AddFoundKey(Trim(GetEnv('OPENROUTER_API_KEY')), 'environment variable OPENROUTER_API_KEY');
  AddFoundKey(Trim(GetEnv('TYPESAFE_API_KEY')), 'environment variable TYPESAFE_API_KEY');
  Home := GetEnv('USERPROFILE');
  Docs := ExpandConstant('{userdocs}');
  ScanProjects(ExpandConstant('{src}'));
  ScanProjects(ExtractFileDir(ExpandConstant('{src}')));
  ScanProjects(Home);
  ScanProjects(ExpandConstant('{userdesktop}'));
  ScanProjects(Docs);
  ScanProjects(Docs + '\GitHub');
  ScanProjects(Home + '\source\repos');
  ScanProjects(Home + '\projects');
  ScanProjects(Home + '\code');
  ScanProjects(Home + '\dev');
  ScanProjects(Home + '\repos');
  ScanProjects(Home + '\src');
  ScanProjects(Home + '\git');
  ScanProjects(Home + '\workspace');
end;

{ /KEY=... or the first key in /KEYFILE=... (a .env file, or a file holding just the key). }
function CommandLineKey: String;
var
  Path: String;
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := Trim(ExpandConstant('{param:KEY|}'));
  if Result <> '' then begin
    KeySource := 'the /KEY switch';
    exit;
  end;
  Path := ExpandConstant('{param:KEYFILE|}');
  if Path = '' then
    exit;
  if not LoadStringsFromFile(Path, Lines) then begin
    Log('Could not read /KEYFILE ' + Path);
    exit;
  end;
  for I := 0 to GetArrayLength(Lines) - 1 do begin
    Result := KeyFromLine(Lines[I]);
    if (Result = '') and (Pos('=', Lines[I]) = 0) and (Pos(' ', Trim(Lines[I])) = 0) then
      Result := Trim(Lines[I]);
    if Result <> '' then begin
      KeySource := Path;
      exit;
    end;
  end;
  Log('No key found in /KEYFILE ' + Path);
end;

{ ---- The "Connect to Jev" page ---- }

procedure StopRunningApp;
var
  AppDir, Command: String;
  ResultCode: Integer;
begin
  { Stops a running copy of the installed app so its files can be replaced or removed. }
  AppDir := ExpandConstant('{app}') + '\';
  StringChangeEx(AppDir, '''', '''''', True);
  Command := '-NoProfile -NonInteractive -Command "Get-Process pythonw, python -ErrorAction SilentlyContinue | ' +
             'Where-Object { $_.Path -and $_.Path.StartsWith(''' + AppDir + ''', ''OrdinalIgnoreCase'') } | Stop-Process -Force"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Command, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure LinkClick(Sender: TObject; const Link: string; LinkType: TSysLinkType);
var
  ErrorCode: Integer;
begin
  ShellExecAsOriginalUser('open', Link, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure KeyChanged(Sender: TObject);
var
  Key: String;
begin
  Key := Trim(KeyEdit.Text);
  if Key <> SourceKey then
    KeySource := '';
  if Key = '' then
    DetectedText.Caption := ''
  else
    DetectedText.Caption := Provider(Key) + ' key';
end;

procedure ShowKeyClick(Sender: TObject);
begin
  KeyEdit.Password := not ShowKeyCheck.Checked;
end;

procedure FoundChanged(Sender: TObject);
begin
  if FoundCombo.ItemIndex < GetArrayLength(FoundKeys) then begin
    SourceKey := FoundKeys[FoundCombo.ItemIndex];
    KeySource := FoundSources[FoundCombo.ItemIndex];
    KeyEdit.Text := SourceKey;
  end else begin
    KeyEdit.Text := '';
    KeySource := '';
    WizardForm.ActiveControl := KeyEdit;
  end;
end;

procedure CreateKeyPage;
var
  Intro, FoundLabel, KeyLabel, Hint: TNewStaticText;
  Links: TNewLinkLabel;
  Top, I: Integer;
  Preset: String;
begin
  KeyPage := CreateCustomPage(wpSelectTasks, 'Connect to Jev',
    'Jev Voice needs an OpenRouter or TypeSafe AI key to understand what''s on your screen.');

  Intro := TNewStaticText.Create(KeyPage);
  Intro.AutoSize := False;
  Intro.WordWrap := True;
  Intro.Width := KeyPage.SurfaceWidth;
  Intro.Height := ScaleY(30);
  Intro.Caption := 'When you speak, Jev Voice sends what you said and the text of the window you''re talking to, ' +
                   'to Jev. The key stays on this PC.';
  Intro.Parent := KeyPage.Surface;
  Top := Intro.Top + Intro.Height + ScaleY(8);

  FoundCombo := TNewComboBox.Create(KeyPage);
  if GetArrayLength(FoundKeys) > 0 then begin
    FoundLabel := TNewStaticText.Create(KeyPage);
    FoundLabel.Caption := 'Keys found on this PC:';
    FoundLabel.Top := Top;
    FoundLabel.Parent := KeyPage.Surface;
    FoundCombo.Top := FoundLabel.Top + FoundLabel.Height + ScaleY(4);
    FoundCombo.Width := KeyPage.SurfaceWidth;
    FoundCombo.Style := csDropDownList;
    FoundCombo.Parent := KeyPage.Surface;
    for I := 0 to GetArrayLength(FoundKeys) - 1 do
      FoundCombo.Items.Add(Provider(FoundKeys[I]) + ' key ' + Masked(FoundKeys[I]) + '   from ' + FoundSources[I]);
    FoundCombo.Items.Add('Type or paste a different key');
    FoundCombo.OnChange := @FoundChanged;
    Top := FoundCombo.Top + FoundCombo.Height + ScaleY(10);
  end;

  KeyLabel := TNewStaticText.Create(KeyPage);
  KeyLabel.Caption := 'API &key:';
  KeyLabel.Top := Top;
  KeyLabel.Parent := KeyPage.Surface;

  KeyEdit := TPasswordEdit.Create(KeyPage);
  KeyEdit.Top := KeyLabel.Top + KeyLabel.Height + ScaleY(4);
  KeyEdit.Width := KeyPage.SurfaceWidth;
  KeyEdit.OnChange := @KeyChanged;
  KeyEdit.Parent := KeyPage.Surface;
  KeyLabel.FocusControl := KeyEdit;

  ShowKeyCheck := TNewCheckBox.Create(KeyPage);
  ShowKeyCheck.Caption := '&Show key';
  ShowKeyCheck.Top := KeyEdit.Top + KeyEdit.Height + ScaleY(6);
  ShowKeyCheck.Width := KeyPage.SurfaceWidth div 2;
  ShowKeyCheck.Height := ScaleY(17);
  ShowKeyCheck.OnClick := @ShowKeyClick;
  ShowKeyCheck.Parent := KeyPage.Surface;

  DetectedText := TNewStaticText.Create(KeyPage);
  DetectedText.AutoSize := False;
  DetectedText.Alignment := taRightJustify;
  DetectedText.Left := KeyPage.SurfaceWidth div 2;
  DetectedText.Width := KeyPage.SurfaceWidth div 2;
  DetectedText.Top := ShowKeyCheck.Top + ScaleY(1);
  DetectedText.Parent := KeyPage.Surface;

  Links := TNewLinkLabel.Create(KeyPage);
  Links.Caption := 'Don''t have one?  <a href="{#OpenRouterKeysUrl}">Get an OpenRouter key</a>   or   ' +
                   '<a href="{#TypeSafeKeysUrl}">get a TypeSafe AI key</a>';
  Links.Top := ShowKeyCheck.Top + ShowKeyCheck.Height + ScaleY(12);
  Links.Width := KeyPage.SurfaceWidth;
  Links.UseVisualStyle := HighContrastActive;
  Links.OnLinkClick := @LinkClick;
  Links.Parent := KeyPage.Surface;

  Hint := TNewStaticText.Create(KeyPage);
  Hint.AutoSize := False;
  Hint.WordWrap := True;
  Hint.Width := KeyPage.SurfaceWidth;
  Hint.Top := Links.Top + ScaleY(24);
  Hint.Height := ScaleY(30);
  if SavedKeyExists then
    Hint.Caption := 'A key is already saved on this PC. Leave the box empty to keep it.'
  else
    Hint.Caption := 'You can also leave it empty and add it later: right-click the Jev pill and choose API key.';
  Hint.Parent := KeyPage.Surface;

  { A key from the command line wins; otherwise offer the first key found, unless one is saved already. }
  Preset := CommandLineKey;
  if (Preset = '') and (GetArrayLength(FoundKeys) > 0) and not SavedKeyExists then begin
    Preset := FoundKeys[0];
    KeySource := FoundSources[0];
    FoundCombo.ItemIndex := 0;
  end else if GetArrayLength(FoundKeys) > 0 then
    FoundCombo.ItemIndex := FoundCombo.Items.Count - 1;
  SourceKey := Preset;
  KeyEdit.Text := Preset;
  KeyChanged(nil);
end;

function ValidateKey: Boolean;
var
  Key: String;
begin
  Result := False;
  Key := Trim(KeyEdit.Text);
  if Key = '' then begin
    Result := SavedKeyExists or WizardSilent or
      (MsgBox('Continue without an API key?' + #13#10#13#10 +
              'Jev Voice will ask for it when it starts.', mbConfirmation, MB_YESNO) = IDYES);
    exit;
  end;
  if (Pos(' ', Key) > 0) or (Pos(#9, Key) > 0) then
    SuppressibleMsgBox('An API key has no spaces. Check what was pasted.', mbError, MB_OK, IDOK)
  else
    Result := True;
end;

procedure SaveKey;
var
  Key: String;
begin
  Key := Trim(KeyEdit.Text);
  if Key = '' then
    exit;
  ForceDirectories(ExpandConstant('{#DataDir}'));
  { The app reads .env.openrouter first, so a stale key for the other service must not linger. }
  if IsOpenRouterKey(Key) then begin
    SaveStringToFile(KeyFile('.env.openrouter'), 'OPENROUTER_API_KEY=' + Key + #13#10, False);
    DeleteFile(KeyFile('.env.typesafe'));
  end else begin
    SaveStringToFile(KeyFile('.env.typesafe'), 'TYPESAFE_API_KEY=' + Key + #13#10, False);
    DeleteFile(KeyFile('.env.openrouter'));
  end;
  Log('Saved ' + Provider(Key) + ' key ' + Masked(Key) + ' from ' + KeySource);
end;

{ ---- Wizard flow ---- }

procedure InitializeWizard;
begin
  FindKeys;
  CreateKeyPage;
  DownloadPage := CreateDownloadPage('Downloading GPU speech recognition',
    'Setup is downloading the NVIDIA libraries for faster speech recognition.', nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function DownloadGpu: Boolean;
begin
  Result := True;
  GpuReady := False;
  if not WizardIsTaskSelected('gpu') or GpuAlreadyInstalled then
    exit;
  DownloadPage.Clear;
  AddGpuDownloads(DownloadPage);
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
      GpuReady := True;
    except
      Log('GPU download failed: ' + GetExceptionMessage);
      if DownloadPage.AbortedByUser then
        Result := MsgBox('Continue without GPU speech recognition?' + #13#10#13#10 +
                         'Jev Voice will use the processor instead. Run Setup again later to add it.',
                         mbConfirmation, MB_YESNO) = IDYES
      else
        SuppressibleMsgBox('The GPU download failed: ' + GetExceptionMessage + #13#10#13#10 +
                           'Jev Voice will use the processor instead. Run Setup again later to add it.',
                           mbInformation, MB_OK, IDOK);
    end;
  finally
    DownloadPage.Hide;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = KeyPage.ID then
    Result := ValidateKey
  else if CurPageID = wpReady then
    Result := DownloadGpu;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
var
  Key, Summary: String;
begin
  Key := Trim(KeyEdit.Text);
  if Key <> '' then begin
    Summary := Provider(Key) + ' key ' + Masked(Key);
    if KeySource <> '' then
      Summary := Summary + NewLine + Space + '(from ' + KeySource + ')';
  end else if SavedKeyExists then
    Summary := 'Keep the key already saved'
  else
    Summary := 'None yet; Jev Voice will ask for it';
  Result := 'API key:' + NewLine + Space + Summary;
  if MemoDirInfo <> '' then
    Result := MemoDirInfo + NewLine + NewLine + Result;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp;
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveKey;
end;

function InitializeUninstall: Boolean;
begin
  StopRunningApp;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent and DirExists(ExpandConstant('{#DataDir}')) then
    if MsgBox('Also delete your Jev Voice settings, API key, activity logs and downloaded speech models?' + #13#10#13#10 +
              'Choose No to keep them for a later reinstall.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{#DataDir}'), True, True, True);
end;
