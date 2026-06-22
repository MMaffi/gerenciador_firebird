; Gerenciador Firebird - Instalador oficial
; Compatível com instalação limpa e atualização in-place

#define MyAppId "Gerenciador Firebird"
#define MyAppName "Gerenciador Firebird"
#define MyAppVersion "2025.12.29.1110"
#define MyAppPublisher "MMaffi"
#define MyAppURL "https://github.com/MMaffi/gerenciador_firebird"
#define MyAppExeName "GerenciadorFirebird.exe"
#define MyAppDataDir "GerenciadorFirebird"

[Setup]
; Não altere o AppId: ele identifica o produto e permite atualizar instalações antigas.
; O valor coincide com o AppId padrão usado pelos instaladores anteriores.
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Instalador do {#MyAppName}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCopyright=Copyright (C) 2025-2026 {#MyAppPublisher}

DefaultDirName={autopf}\Gerenciador Firebird
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes

OutputDir=installer
; Mantém o nome esperado por version.json e pelas Releases do GitHub.
OutputBaseFilename=GerenciadorFirebird_Installer
SetupIconFile=images\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
LicenseFile=LICENSE

Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
InternalCompressLevel=ultra

PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
WizardSizePercent=110

CloseApplications=yes
RestartApplications=no
AllowCancelDuringInstall=no
SetupLogging=yes
ShowLanguageDialog=auto

[Languages]
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar um atalho na Área de Trabalho"; GroupDescription: "Atalhos adicionais:"; Flags: unchecked

[Files]
; restartreplace atende o caso raro de um processo que não respondeu ao Restart Manager.
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion restartreplace
Source: "images\icon.ico"; DestDir: "{app}\images"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\images\icon.ico"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\images\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Executar {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
function GetInstalledVersion(var InstalledVersion: String): Boolean;
var
  UninstallKey: String;
begin
  UninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    '{#MyAppId}_is1';

  Result := RegQueryStringValue(
    HKLM64, UninstallKey, 'DisplayVersion', InstalledVersion);

  if not Result then
    Result := RegQueryStringValue(
      HKLM32, UninstallKey, 'DisplayVersion', InstalledVersion);
end;

procedure InitializeWizard;
var
  InstalledVersion: String;
begin
  if GetInstalledVersion(InstalledVersion) then
  begin
    WizardForm.WelcomeLabel1.Caption :=
      'Atualização do {#MyAppName}';
    WizardForm.WelcomeLabel2.Caption :=
      'A versão ' + InstalledVersion + ' está instalada.' + #13#10 + #13#10 +
      'O assistente atualizará o aplicativo para a versão {#MyAppVersion}. ' +
      'Suas configurações, usuários, agendamentos e backups serão preservados.';
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RemoveData: Integer;
begin
  if (CurUninstallStep = usUninstall) and (not UninstallSilent) then
  begin
    RemoveData := MsgBox(
      'Deseja também excluir configurações, usuários, logs, relatórios e backups ' +
      'armazenados neste computador?' + #13#10 + #13#10 +
      'Escolha Não se pretende reinstalar ou atualizar o aplicativo.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2);

    if RemoveData = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\{#MyAppDataDir}'), True, True, True);

      // Limpeza dos dados usados por versões antigas.
      DeleteFile(ExpandConstant('{app}\config.json'));
      DeleteFile(ExpandConstant('{app}\users.json'));
      DeleteFile(ExpandConstant('{app}\gerenciador_firebird.log'));
      DelTree(ExpandConstant('{app}\backups'), True, True, True);
      DelTree(ExpandConstant('{app}\Relatórios'), True, True, True);
    end;
  end;
end;
