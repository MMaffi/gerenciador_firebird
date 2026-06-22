# Gerenciador Firebird

Aplicativo desktop para administração, manutenção e automação de backups de bancos Firebird no Windows.

## Recursos

- Backup manual e agendado com `gbak`, retenção configurável e compactação validada
- Restauração de `.fbk`, `.zip`, `.rar`, `.7z` e arquivos TAR compactados
- Verificação, reparo, sweep, otimização e relatórios com as ferramentas Firebird
- Monitoramento de processos e espaço em disco
- Console SQL, gestão de usuários e permissões por perfil
- Login automático protegido pelo perfil do Windows e atualização automática

## Segurança e confiabilidade

- Credenciais persistidas com Windows DPAPI; não ficam mais em texto puro
- Senhas locais protegidas com bcrypt e troca obrigatória da credencial inicial
- Sem senha mestra ou acesso administrativo oculto
- Configurações e usuários gravados de forma atômica
- Senhas mascaradas nos logs de comandos
- ZIPs produzidos são validados antes da remoção do `.fbk`
- Extrações bloqueiam caminhos absolutos, `..` e links simbólicos inseguros
- Dados mutáveis do executável instalado ficam em `%LOCALAPPDATA%\GerenciadorFirebird`

## Desenvolvimento

Requer Python 3.11 ou superior, Windows e uma instalação compatível do Firebird.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python main.py
```

Use [config.example.json](config.example.json) como referência. O `config.json` real e o `users.json` são dados locais e não devem ser versionados.

## Testes e build

```powershell
python -m unittest discover -s tests -v
python -m PyInstaller --clean GerenciadorFirebird.spec
```

Para gerar o executável e o instalador em uma única etapa, com Inno Setup 6 instalado:

```powershell
.\build_release.ps1
```

O instalador final é criado na pasta `installer`. Use `-SkipExecutable` para recompilar somente o instalador.

## Instalação

Os instaladores publicados ficam na página de [Releases](https://github.com/MMaffi/gerenciador_firebird/releases).

## Licença

Distribuído sob a licença MIT. Consulte [LICENSE](LICENSE).
