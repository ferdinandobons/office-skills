# SPDX-License-Identifier: MIT
"""Guard against proprietary / vendor leaks in the tracked repository.

Two invariants, both scoped to *git-tracked* files (so gitignored scratch such as
``brand-kit/`` and ``generated/`` is ignored):

1. No Office binary (``.docx``/``.pptx``/``.xlsx``/legacy) is tracked anywhere
   except ``tests/fixtures/`` (test data) or ``examples/`` (curated, 100% synthetic
   BrandDocs demo templates, each with a reproducible builder) - real company
   templates and generated samples must never be committed, regardless of filename.
2. No tracked source imports Bedrock/boto3 or a vendored proprietary Office helper
   package (``office.*``) - the engine is self-contained.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

FORBIDDEN_RUNTIME_PATTERNS = (
    "import boto3",
    "from boto3",
    "bedrock-runtime",
    "from office.",
    "import office.",
)
OFFICE_SUFFIXES = {".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".svg", ".yml", ".yaml", ""}
FIXTURES = ("tests", "fixtures")
EXAMPLES = ("examples",)  # curated synthetic BrandDocs demo templates (built by examples/builders/*)


def _tracked_files(root: Path) -> list[Path]:
    out = subprocess.check_output(["git", "ls-files"], cwd=str(root), text=True)
    return [root / line for line in out.splitlines() if line]


class NoProprietaryTest(unittest.TestCase):
    def test_no_proprietary_or_bedrock_leaks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self_path = Path(__file__).resolve()
        offenders: list[str] = []
        for path in _tracked_files(root):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            suffix = path.suffix.lower()
            in_fixtures = tuple(rel.parts[:2]) == FIXTURES
            in_examples = rel.parts[:1] == EXAMPLES
            if suffix in OFFICE_SUFFIXES and not (in_fixtures or in_examples):
                offenders.append(f"{rel}: tracked Office asset outside tests/fixtures or examples/")
                continue
            if path == self_path or suffix not in TEXT_SUFFIXES:
                continue
            lowered = path.read_text(encoding="utf-8", errors="ignore").lower()
            for token in FORBIDDEN_RUNTIME_PATTERNS:
                if token in lowered:
                    offenders.append(f"{rel}: contains {token!r}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
