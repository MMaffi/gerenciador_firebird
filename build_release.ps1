[CmdletBinding()]
param(
    [switch]$SkipExecutable
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not $SkipExecutable) {
    Write-Host 'Gerando executável com PyInstaller...'
    & python -m PyInstaller --clean --noconfirm GerenciadorFirebird.spec
    if ($LASTEXITCODE -ne 0) {
        throw "O PyInstaller terminou com o código $LASTEXITCODE."
    }
}

$Executable = Join-Path $ProjectRoot 'dist\GerenciadorFirebird.exe'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw 'Executável não encontrado. Execute o script sem -SkipExecutable.'
}

$IsccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
$IsccCandidates = @(@(
    if ($IsccCommand) { $IsccCommand.Source }
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -Unique)

if (-not $IsccCandidates) {
    throw 'Inno Setup 6 não encontrado. Instale-o ou adicione ISCC.exe ao PATH.'
}

Write-Host 'Gerando instalador com Inno Setup...'
$IsccPath = $IsccCandidates | Select-Object -First 1
& $IsccPath (Join-Path $ProjectRoot 'GerenciadorFirebird.iss')
if ($LASTEXITCODE -ne 0) {
    throw "O Inno Setup terminou com o código $LASTEXITCODE."
}

Write-Host 'Build concluído. Consulte a pasta installer.'
