from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ReleaseToolingTests(unittest.TestCase):
    def test_cfn_lint_pin_is_consistent_across_release_entrypoints(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        release_tools = project["project"]["optional-dependencies"]["release-tools"]
        self.assertIn("cfn-lint==1.57.2", release_tools)
        workflow = (ROOT / ".github" / "workflows" / "production-cd.yml").read_text(encoding="utf-8")
        self.assertIn('pip install --disable-pip-version-check -e ".[release-tools]"', workflow)
        self.assertIn("cfn-lint --non-zero-exit-code error", workflow)
        docs = (ROOT / "docs" / "continuous-delivery.md").read_text(encoding="utf-8")
        self.assertIn("cfn-lint==1.57.2", docs)


if __name__ == "__main__":
    unittest.main()
