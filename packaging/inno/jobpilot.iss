; JobPilot Windows 安装包脚本（Inno Setup 6）
;
; 用法：双击 packaging\build_installer.bat，或
;       "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\inno\jobpilot.iss
; 产物：dist\installer\JobPilot-Setup-0.1.0.exe
;
; 【2026-11-13 修】第一次 CI 在这里 0 秒失败，两个原因：
;   1. Inno Setup 的 section 条目**不支持反斜杠续行** —— 我把它当成 shell 用了，
;      续行的后半截被当成新条目解析 → 语法错误。现在每条都写在一行里。
;   2. ArchitecturesAllowed=x64compatible 是 Inno 6.3 才有的值，choco 装的可能是 6.2
;      → 未知值报错。改成 6.0 起就有的 x64。
;
; 三个刻意的选择：
; 1. 装到 %LocalAppData%，不装 Program Files —— 服务把 config.yaml 和 data\
;    写在"当前工作目录"，装进 Program Files 后普通用户没有写权限，首次运行就失败。per-user 还免 UAC。
; 2. 必须自带一份 config.yaml —— 否则死循环：没有配置→服务起不来→向导没机会显示。
; 3. 不打包 Chrome，只检测并提示 —— 塞 Chromium 会改变指纹，等于换了个运行环境。

#define AppName "JobPilot"
#define AppVersion "0.1.0"
#define AppExe "jobpilot.exe"
#define RepoRoot "..\.."

[Setup]
AppId={{6C1B4A52-9F3E-4D71-8A2C-5E7D9B1F0A63}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=JobPilot
DefaultDirName={localappdata}\JobPilot
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#RepoRoot}\dist\installer
OutputBaseFilename=JobPilot-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
AllowNoIcons=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "autostart"; Description: "登录 Windows 后自动启动（推荐）"; GroupDescription: "附加任务："

[Files]
Source: "{#RepoRoot}\dist\jobpilot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\config.example.yaml"; DestDir: "{app}"; DestName: "config.yaml"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\打开控制台"; Filename: "http://127.0.0.1:8010"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JobPilot"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
Filename: "http://127.0.0.1:8010"; Description: "打开控制台（服务启动约需 3 秒，打不开就刷新一次）"; Flags: shellexec nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\*.log"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
function ChromePath(): String;
var
  Candidate: String;
begin
  Result := '';
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', Candidate) then
    Result := Candidate;
  if (Result = '') and RegQueryStringValue(HKEY_CURRENT_USER, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', Candidate) then
    Result := Candidate;
  if Result = '' then
  begin
    Candidate := ExpandConstant('{pf}\Google\Chrome\Application\chrome.exe');
    if FileExists(Candidate) then Result := Candidate;
  end;
  if Result = '' then
  begin
    Candidate := ExpandConstant('{pf32}\Google\Chrome\Application\chrome.exe');
    if FileExists(Candidate) then Result := Candidate;
  end;
  if Result = '' then
  begin
    Candidate := ExpandConstant('{localappdata}\Google\Chrome\Application\chrome.exe');
    if FileExists(Candidate) then Result := Candidate;
  end;
end;

procedure InitializeWizard();
begin
  if ChromePath() = '' then
    MsgBox('没有检测到 Google Chrome。' + #13#10 + #13#10 +
           'JobPilot 通过驱动真实的 Chrome 来登录 BOSS，所以必须先装 Chrome（Edge 不能替代）。' +
           #13#10 + '装完 Chrome 后重新运行本安装程序即可。', mbInformation, MB_OK);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;
