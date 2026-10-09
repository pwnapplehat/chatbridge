; Inno Setup script for the ChatBridge Windows installer (per-user, no administrator needed).
;   iscc /DAppVersion=1.2.0 /DArch=x64   packaging\windows\chatbridge.iss      (or /DArch=arm64)
; Input: dist-win\ChatBridge (PyInstaller output). Output: dist-release\ChatBridge-<version>-<arch>-setup.exe

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif
#ifndef Arch
  #define Arch "x64"
#endif

[Setup]
AppId={{287B184E-4DB9-54B6-865A-B32ED0924283}
AppName=ChatBridge
AppVersion={#AppVersion}
AppVerName=ChatBridge {#AppVersion}
AppPublisher=ChatBridge contributors
AppPublisherURL=https://github.com/pwnapplehat/chatbridge
AppSupportURL=https://github.com/pwnapplehat/chatbridge/issues
DefaultDirName={autopf}\ChatBridge
DefaultGroupName=ChatBridge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no
OutputDir=..\..\dist-release
OutputBaseFilename=ChatBridge-{#AppVersion}-{#Arch}-setup
SetupIconFile=..\..\chatbridge\data\chatbridge.ico
UninstallDisplayIcon={app}\chatbridge-gui.exe
LicenseFile=..\..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
#if Arch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif

[Tasks]
Name: "addpath"; Description: "Add the ""chatbridge"" command to my PATH"; GroupDescription: "Command line:"
Name: "autosync"; Description: "Start background auto-sync (hidden) when I sign in"; GroupDescription: "Auto-sync:"; Flags: unchecked

[Files]
Source: "..\..\dist-win\ChatBridge\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\ChatBridge"; Filename: "{app}\chatbridge-gui.exe"; Comment: "Two-way chat history sync between Cursor and Claude"

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath; Tasks: addpath
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ChatBridge Auto-Sync"; ValueData: """{app}\chatbridge-sync.exe"""; Flags: uninsdeletevalue; Tasks: autosync

[Run]
Filename: "{app}\chatbridge-sync.exe"; Description: "Start auto-sync now"; Flags: nowait postinstall skipifsilent runhidden; Tasks: autosync
Filename: "{app}\chatbridge-gui.exe"; Description: "Open ChatBridge"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM chatbridge-sync.exe"; Flags: runhidden; RunOnceId: "StopAutoSync"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM chatbridge-gui.exe"; Flags: runhidden; RunOnceId: "StopGui"

[Code]
const
  EnvKey = 'Environment';

function NeedsAddPath: Boolean;
var
  Existing: string;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Existing) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(ExpandConstant('{app}')) + ';', ';' + Uppercase(Existing) + ';') = 0;
end;

procedure RemoveFromPath(Dir: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then
    exit;
  P := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';');
  if P = 0 then
    exit;
  if P = 1 then
    Delete(Paths, 1, Length(Dir) + 1)
  else
    Delete(Paths, P - 1, Length(Dir) + 1);
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
