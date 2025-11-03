; ==============================================
; Instalador Gerenciador Firebird
; Autor: MMaffi
; ==============================================

[Setup]
AppName=Gerenciador Firebird
AppVersion=2025.11.03.1019
DefaultDirName=C:\GerenciadorFirebird
DefaultGroupName=Gerenciador Firebird
OutputDir=.
OutputBaseFilename=GerenciadorFirebird_Installer
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
SetupIconFile=images\icon.ico

[Languages]
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"

[Files]
Source: "dist\GerenciadorFirebird.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "images\*"; DestDir: "{app}\images"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Atalho no menu iniciar
Name: "{group}\Gerenciador Firebird"; Filename: "{app}\GerenciadorFirebird.exe"; WorkingDir: "{app}"; IconFilename: "{app}\images\icon.ico"
; Atalho na Área de Trabalho (opcional)
Name: "{userdesktop}\Gerenciador Firebird"; Filename: "{app}\GerenciadorFirebird.exe"; WorkingDir: "{app}"; IconFilename: "{app}\images\icon.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Opções adicionais"; Flags: unchecked

[Run]
Filename: "{app}\GerenciadorFirebird.exe"; Description: "Executar Gerenciador Firebird"; Flags: nowait postinstall skipifsilent