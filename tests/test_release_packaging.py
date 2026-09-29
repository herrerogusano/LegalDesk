from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.package_release import PackagingError, _merge, _safe_relative, build_release


ROOT = Path(__file__).parents[1]


class ReleasePackagingTests(unittest.TestCase):
    def test_lambda_and_frontend_contents_are_bounded_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dependency_root = Path(temp) / "deps"
            (dependency_root / "boto3").mkdir(parents=True)
            (dependency_root / "boto3" / "__init__.py").write_text("__version__ = '1.43.97'\n", encoding="utf-8")
            (dependency_root / "tests").mkdir()
            (dependency_root / "tests" / "must-not-ship.py").write_text("", encoding="utf-8")
            (dependency_root / ".env.production").write_text("SECRET=never\n", encoding="utf-8")
            (dependency_root / "__pycache__").mkdir()
            (dependency_root / "__pycache__" / "ignored.pyc").write_bytes(b"x")

            manifest = build_release(
                repo_root=ROOT,
                dependency_root=dependency_root,
                output_dir=Path(temp) / "release",
            )
            release_dir = Path(temp) / "release"
            with zipfile.ZipFile(release_dir / "legaldesk-lambda.zip") as archive:
                names = archive.namelist()
            self.assertIn("legaldesk/api_gateway.py", names)
            self.assertIn("legaldesk_agent/client.py", names)
            self.assertIn("prompts/legaldesk-system.md", names)
            self.assertIn("boto3/__init__.py", names)
            self.assertIn("legaldesk/malware_scan_lambda.py", names)
            self.assertNotIn("tests/must-not-ship.py", names)
            self.assertNotIn(".env.production", names)
            self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

            with zipfile.ZipFile(release_dir / "legaldesk-frontend.zip") as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["app.js", "citations.js", "index.html", "styles.css"],
                )
            written = json.loads((release_dir / "legaldesk-release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(written, manifest)
            self.assertEqual({artifact["name"] for artifact in manifest["artifacts"]}, {"application", "frontend"})
            for artifact in manifest["artifacts"]:
                self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(all("sha256" in entry for entry in artifact["files"]))

    def test_artifacts_are_byte_for_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dependency_root = Path(temp) / "deps"
            (dependency_root / "example").mkdir(parents=True)
            (dependency_root / "example" / "module.py").write_text("value = 1\n", encoding="utf-8")
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            build_release(repo_root=ROOT, dependency_root=dependency_root, output_dir=first)
            build_release(repo_root=ROOT, dependency_root=dependency_root, output_dir=second)
            for name in ("legaldesk-lambda.zip", "legaldesk-frontend.zip", "legaldesk-release-manifest.json"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)
            with zipfile.ZipFile(first / "legaldesk-lambda.zip") as archive:
                self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))
                self.assertTrue(all((info.external_attr >> 16) & 0o777 == 0o644 for info in archive.infolist()))

    def test_rejects_unsafe_and_duplicate_paths(self) -> None:
        with self.assertRaises(PackagingError):
            _safe_relative("../escape.py")
        with self.assertRaises(PackagingError):
            _safe_relative("/absolute.py")
        with self.assertRaises(PackagingError):
            _safe_relative("C:/absolute.py")
        with self.assertRaises(PackagingError):
            _merge([("same.py", ROOT / "AGENTS.md"), ("same.py", ROOT / "AGENTS.md")])

    def test_rejects_symlinked_dependency_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            real_root = Path(temp) / "real"
            real_root.mkdir()
            link = Path(temp) / "link"
            try:
                link.symlink_to(real_root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this host")
            with self.assertRaises(PackagingError):
                build_release(repo_root=ROOT, dependency_root=link, output_dir=Path(temp) / "out")


if __name__ == "__main__":
    unittest.main()
