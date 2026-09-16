from __future__ import annotations

import re
import unittest
from pathlib import Path


TEMPLATE = (
    Path(__file__).parents[1]
    / "infra"
    / "cloudformation"
    / "phase-03-knowledge-base.yaml"
)


class Phase03InfrastructureTests(unittest.TestCase):
    def test_knowledge_base_uses_supported_index_arn_configuration(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        storage = re.search(
            r"StorageConfiguration:\n(?P<body>[\s\S]*?)\n\n  LegalDeskS3DataSource:",
            template,
        )
        self.assertIsNotNone(storage)
        body = storage.group("body")
        self.assertEqual(
            re.findall(r"^\s+(IndexArn|IndexName|VectorBucketArn):", body, re.MULTILINE),
            ["IndexArn"],
        )
        self.assertIn("IndexArn: !GetAtt LegalDeskVectorIndex.IndexArn", body)

    def test_data_source_has_only_the_approved_fixed_size_chunking(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        chunking = re.search(
            r"ChunkingConfiguration:\n(?P<body>[\s\S]*?)\n\nOutputs:",
            template,
        )
        self.assertIsNotNone(chunking)
        body = chunking.group("body")
        self.assertIn("ChunkingStrategy: FIXED_SIZE", body)
        self.assertIn("FixedSizeChunkingConfiguration:", body)
        self.assertEqual(re.findall(r"MaxTokens: (\d+)", body), ["800"])
        self.assertIn("OverlapPercentage: 15", body)
        self.assertNotIn("HIERARCHICAL", template)
        self.assertNotIn("HierarchicalChunkingConfiguration", template)

    def test_vector_index_reserves_bedrock_internal_metadata_as_non_filterable(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        index = re.search(
            r"LegalDeskVectorIndex:\n(?P<body>[\s\S]*?)\n\n  KnowledgeBaseExecutionRole:",
            template,
        )
        self.assertIsNotNone(index)
        body = index.group("body")
        non_filterable = re.search(
            r"MetadataConfiguration:\n\s+NonFilterableMetadataKeys:\n(?P<keys>(?:\s+- [A-Z_]+\n)+)",
            body,
        )
        self.assertIsNotNone(non_filterable)
        keys = re.findall(r"- ([A-Z_]+)", non_filterable.group("keys"))
        self.assertEqual(
            keys,
            ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"],
        )
        self.assertNotIn("tenantId", keys)
        self.assertNotIn("matterId", keys)


if __name__ == "__main__":
    unittest.main()
