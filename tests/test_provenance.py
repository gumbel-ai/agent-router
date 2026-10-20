import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from router.process import creation_identity
from router.provenance import capture_startup_provenance


class FixtureDistribution:
    version = "0.4.0"

    def __init__(self, root: Path):
        self.root = root
        self.metadata = root / "agent_router-0.4.0.dist-info"

    def locate_file(self, path: str) -> Path:
        return self.root / path

    def read_text(self, name: str) -> str | None:
        path = self.metadata / name
        return path.read_text() if path.exists() else None


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-provenance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "router").mkdir()
        (self.root / "agent_router-0.4.0.dist-info").mkdir()
        self.dist = FixtureDistribution(self.root)
        self.write_installation(
            "a" * 40,
            {
                "router/__init__.py": b'__version__ = "A"\n',
                "router/daemon.py": b'VALUE = "A"\n',
            },
        )

    @staticmethod
    def record_hash(content: bytes) -> str:
        return (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .rstrip(b"=")
            .decode()
        )

    def write_installation(self, commit: str, files: dict[str, bytes]) -> None:
        package = self.root / "router"
        for path in sorted(package.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        rows = []
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            rows.append(f"{name},sha256={self.record_hash(content)},{len(content)}")
        direct_url = json.dumps(
            {
                "url": "https://example.invalid/agent-router",
                "vcs_info": {"vcs": "git", "commit_id": commit},
            }
        ).encode()
        (self.dist.metadata / "direct_url.json").write_bytes(direct_url)
        rows.append(
            "agent_router-0.4.0.dist-info/direct_url.json,"
            f"sha256={self.record_hash(direct_url)},{len(direct_url)}"
        )
        (self.dist.metadata / "RECORD").write_text("\n".join(rows) + "\n")

    def rewrite_direct_url(self, content: bytes) -> None:
        (self.dist.metadata / "direct_url.json").write_bytes(content)
        record = (self.dist.metadata / "RECORD").read_text().splitlines()
        prefix = "agent_router-0.4.0.dist-info/direct_url.json,"
        replacement = f"{prefix}sha256={self.record_hash(content)},{len(content)}"
        (self.dist.metadata / "RECORD").write_text(
            "\n".join(replacement if row.startswith(prefix) else row for row in record)
            + "\n"
        )

    def capture(self) -> dict:
        with (
            patch("router.provenance.distribution", return_value=self.dist),
            patch("router.provenance._verify_module_origins"),
        ):
            return capture_startup_provenance()

    def test_capture_binds_current_process_and_freezes_source_snapshot(self):
        first = self.capture()
        self.assertEqual(
            first,
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "creation_identity": creation_identity(os.getpid()),
                "source": {
                    "status": "verified",
                    "scheme": "python-record-v1",
                    "distribution": "agent-router",
                    "version": "0.4.0",
                    "commit": "a" * 40,
                    "content_sha256": first["source"]["content_sha256"],
                },
            },
        )
        self.assertEqual(len(first["source"]["content_sha256"]), 64)

        self.write_installation(
            "B" * 40,
            {
                "router/__init__.py": b'__version__ = "B"\n',
                "router/daemon.py": b'VALUE = "B"\n',
            },
        )
        second = self.capture()
        self.assertEqual(first["source"]["commit"], "a" * 40)
        self.assertEqual(second["source"]["commit"], "b" * 40)
        self.assertNotEqual(
            first["source"]["content_sha256"], second["source"]["content_sha256"]
        )
        self.assertEqual(first["creation_identity"], second["creation_identity"])

    def test_record_and_direct_url_malformed_variants_are_unavailable(self):
        valid_record = (self.dist.metadata / "RECORD").read_text()
        cases = {
            "tampered bytes": (
                "record_mismatch",
                lambda: (self.root / "router" / "daemon.py").write_text("tampered\n"),
            ),
            "missing hash": (
                "record_invalid",
                lambda: (self.dist.metadata / "RECORD").write_text(
                    valid_record.replace("sha256=", "", 1)
                ),
            ),
            "duplicate path": (
                "record_invalid",
                lambda: (self.dist.metadata / "RECORD").write_text(
                    valid_record + valid_record.splitlines()[0] + "\n"
                ),
            ),
            "unrecorded file": (
                "record_mismatch",
                lambda: (self.root / "router" / "extra.py").write_text("extra\n"),
            ),
            "invalid commit": (
                "direct_url_invalid",
                lambda: self.rewrite_direct_url(
                    b'{"vcs_info":{"vcs":"git","commit_id":"bad"}}'
                ),
            ),
            "duplicate JSON key": (
                "direct_url_invalid",
                lambda: self.rewrite_direct_url(b'{"url":"one","url":"two"}'),
            ),
        }
        for name, (reason, mutate) in cases.items():
            with self.subTest(name=name):
                self.write_installation(
                    "a" * 40,
                    {
                        "router/__init__.py": b'__version__ = "A"\n',
                        "router/daemon.py": b'VALUE = "A"\n',
                    },
                )
                mutate()
                self.assertEqual(
                    self.capture()["source"],
                    {
                        "status": "unavailable",
                        "scheme": "python-record-v1",
                        "distribution": "agent-router",
                        "reason": reason,
                    },
                )

    def test_loaded_module_origin_outside_distribution_is_unavailable(self):
        with patch("router.provenance.distribution", return_value=self.dist):
            self.assertEqual(
                capture_startup_provenance()["source"]["reason"],
                "module_origin_mismatch",
            )

    def test_mid_capture_record_change_is_unavailable(self):
        original = self.dist.read_text
        record_reads = 0

        def unstable(name: str) -> str | None:
            nonlocal record_reads
            content = original(name)
            if name == "RECORD":
                record_reads += 1
                if record_reads == 2:
                    return content + "changed"
            return content

        with (
            patch("router.provenance.distribution", return_value=self.dist),
            patch("router.provenance._verify_module_origins"),
            patch.object(self.dist, "read_text", side_effect=unstable),
        ):
            self.assertEqual(
                capture_startup_provenance()["source"]["reason"],
                "installation_changed",
            )

    def test_process_creation_identity_failure_is_not_hidden(self):
        with (
            patch("router.provenance.distribution", return_value=self.dist),
            patch(
                "router.provenance.creation_identity",
                side_effect=OSError("unavailable"),
            ),
            self.assertRaisesRegex(OSError, "unavailable"),
        ):
            capture_startup_provenance()


if __name__ == "__main__":
    unittest.main()
