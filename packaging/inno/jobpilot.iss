; JobPilot Windows 安装包脚本（Inno Setup 6）— 最小可编译版
;
; 【2026-11-13 第三次修】同一处失败两次之后不再猜，先砍到最小：
;   去掉 [Code]（Chrome 检测）、[Registry]（开机自启）、[Tasks]、
;   [UninstallDelete]、Architectures* 全部可疑项。
;   先把"能装"跑绿，再一项一项加回来 —— 每次只加一项，红就知道是谁。
;
; 【已踩过的两个坑，别再加回去】
;   1. Inno 的 section 条目不支持反斜杠续行（不是 shell），必须一条一行
;   2. ArchitecturesAllowed=x64compatible 要 Inno 6.3+，choco 装的可能是 6.2
;
; 装到 %LocalAppData% 而不是 Program Files：服务把 config.yaml 和 data\ 写在
; "当前工作目录"，Program Files 普通用户没写权限，首次运行就失败。per-user 还免 UAC。

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
AllowNoIcons=yes

[Files]
Source: "{#RepoRoot}\dist\jobpilot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\config.example.yaml"; DestDir: "{app}"; DestName: "config.yaml"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
