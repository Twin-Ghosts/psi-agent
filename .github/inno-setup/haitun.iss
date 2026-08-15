; Inno Setup script for HaiTun Agent.
; Packages the entire haitun-workspace (including psi-agent.exe, copied in at build time).

#define MyAppName "HaiTun Agent"
#define MyAppVersion "1.0.4"
#define MyAppPublisher "Hefei Zhenzhi Artificial Intelligence Application Software Co., Ltd"
#define MyAppExeName "haitun.exe"

[Setup]
AppId={{234DFAA2-39F9-4E4C-92C7-680728ADDA4A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\haitun.ico
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=HaiTun Agent Setup
SetupIconFile=haitun.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

; 协议页文案写在这里而非 .isl —— ChineseSimplified.isl 是构建时下载且被 .gitignore,
; 不能指望它提供任何自定义键。
[CustomMessages]
chinesesimplified.LegalPageCaption=许可协议与隐私保护政策
chinesesimplified.LegalPageDesc=安装前请阅读并同意以下协议
chinesesimplified.LegalIntro=请点击下方链接阅读协议全文。勾选即表示您已阅读并同意两份协议的全部内容。
chinesesimplified.LegalTerms=《Haitun Agent 软件许可及服务协议》
chinesesimplified.LegalPrivacy=《Haitun Agent 隐私保护政策》
chinesesimplified.LegalAgree=我已阅读并同意上述协议
english.LegalPageCaption=License Agreement and Privacy Policy
english.LegalPageDesc=Please read and accept the agreements before installing
english.LegalIntro=Click the links below to read the full text. Checking the box means you have read and accepted both agreements. (Chinese only.)
english.LegalTerms=Haitun Agent Software License and Service Agreement
english.LegalPrivacy=Haitun Agent Privacy Policy
english.LegalAgree=I have read and agree to the agreements above

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\examples\haitun-workspace\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "haitun.ico"; DestDir: "{app}"
Source: "haitun.exe"; DestDir: "{app}"
; 协议页要读的三个文件。dontcopy = 只打进安装包供向导页临时解出, 不装到 {app}
; —— 产品内那份走 spa-v2/dist（vite 会把 public/* 拷进去）, 装两份必有一份过时。
; 这三个是 scripts/gen_legal_html.py 的产物, 改 docs/ 下的 md 后需重新生成。
Source: "..\..\src\psi_agent\gateway\spa-v2\public\terms.html"; Flags: dontcopy
Source: "..\..\src\psi_agent\gateway\spa-v2\public\privacy.html"; Flags: dontcopy
Source: "..\..\src\psi_agent\gateway\spa-v2\public\legal.css"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\haitun.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\haitun.ico"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: shellexec postinstall skipifsilent

[Code]
{ ---- 协议页 ----
  许可协议导言写明「您在本软件安装过程中勾选同意本协议, 即视为您同时同意隐私保护政策」,
  所以是一个勾选框覆盖两份, 而非各勾一个 —— 这也是不能用内置 LicenseFile 的原因:
  它是单选钮, 且一次只挂一份文件。

  不记录同意状态(无注册表、无标记文件): 团队决定每次安装都勾。自动更新走完整向导
  (haitun.c 用 ShellExecuteW 拉起 setup, 未带 /SILENT), 因此升级也会经过本页。
  副作用是好的: 换人用同一台机器不会静默跳过协议。

  /SILENT 与 /VERYSILENT 会跳过全部向导页含本页, 按决策「视为部署方已代为同意」,
  故不加 /ACCEPTTOS 参数。见 oss-publish.md。 }
var
  LegalPage: TWizardPage;
  LegalAgreeCheck: TNewCheckBox;
  LegalFilesExtracted: Boolean;
  PrevPageID: Integer;

{ 打开协议 HTML。ExtractTemporaryFile 对同一文件重复调用会报错, 故用标记只解一次;
  legal.css 必须一并解出, 否则浏览器拿到的是无样式裸文本。临时目录由 Inno 退出时自清。
  注意: 本行不能写花括号常量 —— Pascal 注释以花括号定界, 写进去会提前闭合注释。 }
procedure OpenLegalDoc(const FileName: String);
var
  ResultCode: Integer;
begin
  if not LegalFilesExtracted then
  begin
    ExtractTemporaryFile('legal.css');
    ExtractTemporaryFile('terms.html');
    ExtractTemporaryFile('privacy.html');
    LegalFilesExtracted := True;
  end;
  if not ShellExec('open', ExpandConstant('{tmp}\') + FileName,
                   '', '', SW_SHOWNORMAL, ewNoWait, ResultCode) then
    MsgBox('无法打开协议文件，请检查系统默认浏览器设置。', mbError, MB_OK);
end;

procedure LegalTermsClick(Sender: TObject);
begin
  OpenLegalDoc('terms.html');
end;

procedure LegalPrivacyClick(Sender: TObject);
begin
  OpenLegalDoc('privacy.html');
end;

{ 勾选状态直接驱动「下一步」的可用性。不用 NextButtonClick 返回 False 弹提示
  —— 那是先让人点了再拒绝; 禁用态更直白。 }
procedure UpdateNextButtonState;
begin
  WizardForm.NextButton.Enabled := LegalAgreeCheck.Checked;
end;

procedure LegalAgreeClick(Sender: TObject);
begin
  UpdateNextButtonState;
end;

{ 造一个下划线蓝色可点文本。不用 6.3 才有的 TNewLinkLabel: CI 里
  choco install innosetup 不锁版本, 拿到更早的 6.x 会编译失败。
  OnClick 由调用方赋值 —— 事件类型不作参数传, 少一处版本相关的写法。 }
function CreateLegalLink(const Caption: String; ATop: Integer): TNewStaticText;
begin
  Result := TNewStaticText.Create(LegalPage);
  Result.Parent := LegalPage.Surface;
  Result.Caption := Caption;
  Result.Top := ATop;
  Result.Left := ScaleX(8);
  Result.Cursor := crHand;
  Result.Font.Color := clBlue;
  Result.Font.Style := [fsUnderline];
end;

procedure CreateLegalPage;
var
  Intro: TNewStaticText;
  TermsLink: TNewStaticText;
  PrivacyLink: TNewStaticText;
begin
  LegalPage := CreateCustomPage(wpWelcome,
    ExpandConstant('{cm:LegalPageCaption}'), ExpandConstant('{cm:LegalPageDesc}'));

  Intro := TNewStaticText.Create(LegalPage);
  Intro.Parent := LegalPage.Surface;
  Intro.AutoSize := False;
  Intro.WordWrap := True;
  Intro.Left := 0;
  Intro.Top := 0;
  Intro.Width := LegalPage.SurfaceWidth;
  Intro.Height := ScaleY(34);
  Intro.Caption := ExpandConstant('{cm:LegalIntro}');

  TermsLink := CreateLegalLink(ExpandConstant('{cm:LegalTerms}'), ScaleY(48));
  TermsLink.OnClick := @LegalTermsClick;
  PrivacyLink := CreateLegalLink(ExpandConstant('{cm:LegalPrivacy}'), ScaleY(72));
  PrivacyLink.OnClick := @LegalPrivacyClick;

  LegalAgreeCheck := TNewCheckBox.Create(LegalPage);
  LegalAgreeCheck.Parent := LegalPage.Surface;
  LegalAgreeCheck.Left := 0;
  LegalAgreeCheck.Top := ScaleY(108);
  LegalAgreeCheck.Width := LegalPage.SurfaceWidth;
  LegalAgreeCheck.Height := ScaleY(20);
  LegalAgreeCheck.Caption := ExpandConstant('{cm:LegalAgree}');
  LegalAgreeCheck.OnClick := @LegalAgreeClick;
end;

procedure InitializeWizard;
begin
  LegalFilesExtracted := False;
  PrevPageID := -1;
  CreateLegalPage;
end;

{ 进本页时按勾选状态重算(用户可能勾了之后点「上一步」再回来, 那时按钮该是启用的);
  刚离开本页时把按钮交还给 Inno, 否则后续页面的「下一步」会被我们留在禁用态。

  只在「上一页是本页」时才恢复, 不能对所有其他页无条件置 True ——
  安装进行页等页面的按钮状态由 Inno 自己管, 每页都插一手会把它的状态覆盖掉。 }
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = LegalPage.ID then
    UpdateNextButtonState
  else if PrevPageID = LegalPage.ID then
    WizardForm.NextButton.Enabled := True;
  PrevPageID := CurPageID;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  NeedsRestart := False;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM haitun.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM psi-agent.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
