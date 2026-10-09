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

    def test_offline_ci_builds_and_smoke_tests_the_pinned_idp_artifact_without_aws(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "offline-ci.yml").read_text(encoding="utf-8")
        self.assertIn("packaging/constraints-python312-manylinux-x86_64.txt", workflow)
        self.assertIn("scripts/package_release.py", workflow)
        self.assertIn("legaldesk-python312-deps", workflow)
        self.assertIn("legaldesk-lambda-root", workflow)
        self.assertIn("archive.extractall", workflow)
        self.assertIn("python -S", workflow)
        self.assertIn("assert_from_artifact", workflow)
        self.assertIn("legaldesk.idp_lambda", workflow)
        self.assertIn("legaldesk.idp_ocr_lambda", workflow)
        self.assertIn('"pypdf": "6.20.0"', workflow)
        self.assertIn("prompts/idp-classifier.md", workflow)
        self.assertIn("prompts/idp-extractor.md", workflow)
        self.assertNotIn("aws sts", workflow.lower())

    def test_offline_ci_retains_only_the_validated_release_outputs(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "offline-ci.yml").read_text(encoding="utf-8")
        self.assertIn("uses: actions/upload-artifact@v4", workflow)
        self.assertIn("if: ${{ success() }}", workflow)
        self.assertIn("name: legaldesk-release-linux-python312-${{ github.sha }}", workflow)
        self.assertIn("${{ runner.temp }}/legaldesk-release-smoke/legaldesk-lambda.zip", workflow)
        self.assertIn("${{ runner.temp }}/legaldesk-release-smoke/legaldesk-frontend.zip", workflow)
        self.assertIn("${{ runner.temp }}/legaldesk-release-smoke/legaldesk-release-manifest.json", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn("retention-days: 3", workflow)
        self.assertNotIn("${{ github.workspace }}", workflow.split("uses: actions/upload-artifact@v4", 1)[1])


if __name__ == "__main__":
    unittest.main()
