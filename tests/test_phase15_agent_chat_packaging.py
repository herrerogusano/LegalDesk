"""Phase 15: the worker finds its agent prompt inside the packaged Lambda artifact (PB-003)."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.package_release import build_release

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.agent_chat_lambda import default_agent_prompt_path

PROBE = r"""
import hashlib, sys
sys.path.insert(0, sys.argv[1])
from legaldesk import agent_chat_lambda as module
from legaldesk.prompts import FileSystemSystemPromptProvider
assert module.__file__.startswith(sys.argv[1]), module.__file__
path = module.default_agent_prompt_path()
artifact = FileSystemSystemPromptProvider(path).load()
print(path.relative_to(sys.argv[1]).as_posix(), artifact.prompt_id, artifact.version, artifact.sha256)
"""


class AgentPromptPathTests(unittest.TestCase):
    def test_path_resolves_for_the_zip_layout_and_for_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "task"
            (task / "legaldesk").mkdir(parents=True)
            (task / "prompts").mkdir()
            (task / "prompts" / "legaldesk-agent.md").write_text("x", encoding="utf-8")
            module = task / "legaldesk" / "agent_chat_lambda.py"
            module.write_text("", encoding="utf-8")
            self.assertEqual(default_agent_prompt_path(module), (task / "prompts" / "legaldesk-agent.md").resolve())
        checkout = ROOT / "backend" / "src" / "legaldesk" / "agent_chat_lambda.py"
        self.assertEqual(default_agent_prompt_path(checkout), ROOT / "prompts" / "legaldesk-agent.md")

    def test_a_shallow_path_does_not_crash(self) -> None:
        self.assertEqual(default_agent_prompt_path(Path("/agent_chat_lambda.py")).name, "legaldesk-agent.md")

    def test_worker_loads_its_prompt_from_the_extracted_zip_not_from_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dependency_root = Path(temp) / "deps"
            dependency_root.mkdir()
            release_dir = Path(temp) / "release"
            build_release(repo_root=ROOT, dependency_root=dependency_root, output_dir=release_dir)
            extracted = Path(temp) / "var-task"
            with zipfile.ZipFile(release_dir / "legaldesk-lambda.zip") as archive:
                archive.extractall(extracted)
            # -I: ignore PYTHONPATH and the working directory, so only the zip contents can satisfy the import.
            result = subprocess.run(
                [sys.executable, "-I", "-c", PROBE, str(extracted)],
                capture_output=True, text=True, cwd=temp, timeout=60, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            relative, prompt_id, version, sha = result.stdout.split()
            self.assertEqual((relative, prompt_id), ("prompts/legaldesk-agent.md", "legaldesk-agent"))
            expected = hashlib.sha256((ROOT / "prompts" / "legaldesk-agent.md").read_bytes()).hexdigest()
            self.assertEqual(sha, expected)


if __name__ == "__main__":
    unittest.main()
