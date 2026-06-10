# SPDX-License-Identifier: MIT
"""Drift guards for the operational docs.

``documentation/PLUGIN_WORKFLOW.md`` is the agent-facing map of the plugin: it
must mention every CLI verb ``scripts/brandkit/cli.py`` actually exposes and
every slash command shipped in ``commands/``. These guards fail the build the
moment a verb or command is added (or renamed) without updating the map, so the
document can never silently rot.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DOC = ROOT / "documentation" / "PLUGIN_WORKFLOW.md"
CLI = ROOT / "scripts" / "brandkit" / "cli.py"
COMMANDS_DIR = ROOT / "commands"

_ADD_PARSER = re.compile(r'add_parser\(\s*"([a-z][a-z-]*)"')


def _cli_verbs() -> list[str]:
    verbs = _ADD_PARSER.findall(CLI.read_text(encoding="utf-8"))
    assert verbs, "no add_parser() verbs found in cli.py: regex drifted?"
    return sorted(set(verbs))


class TestPluginWorkflowDrift(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = WORKFLOW_DOC.read_text(encoding="utf-8")

    def test_every_cli_verb_is_documented(self) -> None:
        missing = [v for v in _cli_verbs() if f"`{v}`" not in self.doc]
        self.assertEqual(
            missing,
            [],
            f"CLI verbs missing from PLUGIN_WORKFLOW.md: {missing} "
            "(update the verb table when adding/renaming a verb)",
        )

    def test_every_slash_command_is_documented(self) -> None:
        stems = sorted(p.stem for p in COMMANDS_DIR.glob("*.md"))
        self.assertTrue(stems, "no commands found in commands/: layout drifted?")
        missing = [s for s in stems if f"`/{s}`" not in self.doc]
        self.assertEqual(
            missing,
            [],
            f"slash commands missing from PLUGIN_WORKFLOW.md: {missing} "
            "(update the command table when adding/renaming a command)",
        )


if __name__ == "__main__":
    unittest.main()
