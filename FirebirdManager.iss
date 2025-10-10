; ==============================================
; Instalador Firebird Manager - Versão Final
; Autor: MMaffi
; ==============================================

[Setup]
AppName=Firebird Manager
AppVersion=1.0.0
DefaultDirName=C:\FirebirdManager
DefaultGroupName=Firebird Manager
OutputDir=.
OutputBaseFilename=FirebirdManager_Installer
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
SetupIconFile=images\icon.ico

[Languages]
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"

[Files]
Source: "dist\FirebirdManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "images\*"; DestDir: "{app}\images"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Atalho no menu iniciar
Name: "{group}\Firebird Manager"; Filename: "{app}\FirebirdManager.exe"; WorkingDir: "{app}"; IconFilename: "{app}\images\icon.ico"
; Atalho na Área de Trabalho (opcional)
Name: "{userdesktop}\Firebird Manager"; Filename: "{app}\FirebirdManager.exe"; WorkingDir: "{app}"; IconFilename: "{app}\images\icon.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Opções adicionais"; Flags: unchecked

[Run]
Filename: "{app}\FirebirdManager.exe"; Description: "Executar Firebird Manager"; Flags: nowait postinstall skipifsilent

[Code]

function DoubleBackslashes(S: string): string;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    Result := Result + S[I];
    if S[I] = '\' then
      Result := Result + '\';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  BackupDir, ConfigFile, LogFile: string;
  ConfigContent: string;
begin
  if CurStep = ssInstall then
  begin
    BackupDir := ExpandConstant('{app}\backups');
    ConfigFile := ExpandConstant('{app}\config.json');
    LogFile := ExpandConstant('{app}\firebird_manager.log');

    // Cria pasta de backups
    if not DirExists(BackupDir) then
      ForceDirectories(BackupDir);

    // Cria config.json padrão se não existir
    if not FileExists(ConfigFile) then
    begin
      ConfigContent :=
        '{' + #13#10 +
        '  "gbak_path": "",' + #13#10 +
        '  "gfix_path": "",' + #13#10 +
        '  "backup_dir": "' + DoubleBackslashes(BackupDir) + '",' + #13#10 +
        '  "keep_backups": 5,' + #13#10 +
        '  "firebird_user": "SYSDBA",' + #13#10 +
        '  "firebird_password": "masterkey",' + #13#10 +
        '  "firebird_host": "localhost"' + #13#10 +
        '}';
      SaveStringToFile(ConfigFile, ConfigContent, False);
    end;

    // Cria log vazio se não existir
    if not FileExists(LogFile) then
      SaveStringToFile(LogFile, '', False);
  end;
end;
