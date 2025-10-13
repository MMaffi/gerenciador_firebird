; ==============================================
; Instalador Firebird Manager
; Autor: MMaffi
; ==============================================

[Setup]
AppName=Firebird Manager
AppVersion=2025.10.11.1453
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