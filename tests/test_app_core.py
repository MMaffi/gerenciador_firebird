import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from app_core import (
    atomic_write_json,
    cleanup_old_backups,
    compress_backup,
    redact_command,
    safe_extract_zip,
    validate_password,
)
from secure_store import protect_secret, unprotect_secret


class AppCoreTests(unittest.TestCase):
    def test_atomic_write_json_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.json"
            atomic_write_json(target, {"descrição": "Backup diário"})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"descrição": "Backup diário"},
            )

    def test_redact_command_hides_password(self):
        rendered = redact_command(["gbak", "-user", "SYSDBA", "-pass", "segredo", "db.fdb"])
        self.assertNotIn("segredo", rendered)
        self.assertIn("********", rendered)

    def test_compress_backup_is_valid_and_removes_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "base.fbk"
            source.write_bytes(b"backup-test")
            result = compress_backup(source)
            self.assertFalse(source.exists())
            with zipfile.ZipFile(result) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read("base.fbk"), b"backup-test")

    def test_cleanup_keeps_at_least_one_recent_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            files = []
            for index in range(3):
                item = base / f"backup-{index}.fbk"
                item.write_text(str(index), encoding="utf-8")
                timestamp = time.time() + index
                os.utime(item, (timestamp, timestamp))
                files.append(item)
            removed = cleanup_old_backups(base, 0)
            self.assertEqual(len(removed), 2)
            self.assertTrue(files[-1].exists())

    def test_zip_path_traversal_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive_path = base / "malicious.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "blocked")
            with self.assertRaises(ValueError):
                safe_extract_zip(archive_path, base / "output")
            self.assertFalse((base / "escape.txt").exists())

    def test_password_policy(self):
        self.assertFalse(validate_password("curta")[0])
        self.assertTrue(validate_password("uma-senha-segura")[0])

    @unittest.skipUnless(os.name == "nt", "DPAPI existe apenas no Windows")
    def test_windows_dpapi_round_trip(self):
        protected = protect_secret("credencial de teste")
        self.assertTrue(protected.startswith("dpapi:"))
        self.assertEqual(unprotect_secret(protected), "credencial de teste")


if __name__ == "__main__":
    unittest.main()
