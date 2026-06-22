"""Núcleo testável e independente da interface do Gerenciador Firebird."""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, Sequence


SECRET_ARGUMENTS = {"-pass", "-password", "--password"}
BACKUP_SUFFIXES = {".fbk", ".zip"}


def atomic_write_json(path: Path, data: Any) -> None:
    """Grava JSON no mesmo volume e só substitui o destino ao finalizar."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def redact_command(command: Sequence[object]) -> str:
    """Formata uma linha de comando sem expor argumentos secretos."""
    result: list[str] = []
    hide_next = False
    for argument in command:
        value = str(argument)
        if hide_next:
            result.append("********")
            hide_next = False
            continue
        result.append(value)
        hide_next = value.lower() in SECRET_ARGUMENTS
    return " ".join(result)


def cleanup_old_backups(backup_dir: Path, keep: int) -> list[Path]:
    """Mantém os backups mais recentes e retorna os arquivos removidos."""
    backup_dir = Path(backup_dir)
    keep = max(1, int(keep))
    if not backup_dir.is_dir():
        return []
    files = [
        item for item in backup_dir.iterdir()
        if item.is_file() and item.suffix.lower() in BACKUP_SUFFIXES
    ]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for item in files[keep:]:
        item.unlink()
        removed.append(item)
    return removed


def compress_backup(source: Path, destination: Path | None = None) -> Path:
    """Cria e valida um ZIP de forma transacional, preservando a origem em erro."""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"Backup não encontrado: {source}")
    destination = Path(destination) if destination else source.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.write(source, arcname=source.name)
        with zipfile.ZipFile(temporary, "r") as archive:
            if archive.testzip() is not None:
                raise zipfile.BadZipFile("O ZIP criado não passou na validação")
        os.replace(temporary, destination)
        source.unlink()
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _safe_destination(base: Path, member_name: str) -> Path:
    """Impede path traversal e caminhos absolutos durante extrações."""
    base = base.resolve()
    normalized_name = member_name.replace("\\", "/")
    candidate_path = Path(normalized_name)
    if candidate_path.is_absolute() or candidate_path.drive:
        raise ValueError(f"Caminho absoluto bloqueado no arquivo: {member_name}")
    destination = (base / candidate_path).resolve()
    try:
        destination.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Caminho inseguro bloqueado no arquivo: {member_name}") from exc
    return destination


def validate_archive_members(base: Path, member_names: Iterable[str]) -> None:
    for member_name in member_names:
        _safe_destination(Path(base), member_name)


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        validate_archive_members(destination, (item.filename for item in archive.infolist()))
        archive.extractall(destination)


def safe_extract_tar(archive_path: Path, destination: Path, mode: str = "r:*") -> None:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode) as archive:
        members = archive.getmembers()
        validate_archive_members(destination, (item.name for item in members))
        if any(item.issym() or item.islnk() for item in members):
            raise ValueError("Links simbólicos não são permitidos em arquivos TAR")
        archive.extractall(destination, members=members, filter="data")


def safe_extract_archive(archive_path: Path, destination: Path) -> None:
    """Extrai formatos suportados após validar todos os nomes internos."""
    archive_path = Path(archive_path)
    destination = Path(destination)
    lower_name = archive_path.name.lower()
    if lower_name.endswith(".zip"):
        safe_extract_zip(archive_path, destination)
    elif lower_name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
        safe_extract_tar(archive_path, destination)
    elif lower_name.endswith(".rar"):
        import rarfile

        with rarfile.RarFile(archive_path) as archive:
            validate_archive_members(destination, (item.filename for item in archive.infolist()))
            archive.extractall(destination)
    elif lower_name.endswith(".7z"):
        import py7zr

        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            validate_archive_members(destination, archive.getnames())
            archive.extractall(destination)
    else:
        raise ValueError(f"Formato de arquivo não suportado: {archive_path.suffix}")


def validate_password(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "A senha deve ter pelo menos 8 caracteres."
    if password.isspace():
        return False, "A senha não pode conter apenas espaços."
    return True, ""
