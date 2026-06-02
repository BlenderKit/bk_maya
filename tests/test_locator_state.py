"""Tests for ``bk_maya.core.locator_state`` — pure-Python shared state.

Validates the per-locator registries used to bridge the plug-in's private
namespace and the addon's normal-import namespace.
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bk_maya.core import locator_state as plc


class _RegistryCase(unittest.TestCase):
    """Reset registries between tests so they don't leak across cases."""

    def setUp(self):
        plc.proxor_registry.clear()
        plc.proxor_mesh_registry.clear()
        plc.label_registry.clear()


class ProxorLinesRegistryTest(_RegistryCase):
    """Tests for the Proxor Lines registry, which stores line data for the drag-to-place visual."""

    def test_get_missing_returns_empty_list(self):
        """Requesting lines for an unknown node should return an empty list, not raise."""
        self.assertEqual(plc.get_proxor_lines("missing"), [])

    def test_set_then_get_roundtrip(self):
        """Setting lines for a node and then getting them should return the same data."""
        lines = [[(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]]
        plc.set_proxor_lines("loc1", lines)
        self.assertEqual(plc.get_proxor_lines("loc1"), lines)

    def test_set_ignores_empty_node_name(self):
        """Setting lines for an empty node name should be ignored, not stored."""
        plc.set_proxor_lines("", [[(0, 0, 0), (1, 1, 1)]])
        self.assertEqual(plc.proxor_registry, {})

    def test_clear_removes_entry(self):
        """Clearing lines for a node should remove its entry from the registry."""
        plc.set_proxor_lines("loc1", [[(0, 0, 0), (1, 1, 1)]])
        plc.clear_proxor_lines("loc1")
        self.assertEqual(plc.get_proxor_lines("loc1"), [])

    def test_clear_unknown_is_noop(self):
        """Clearing lines for a non-existent node should be a no-op, not raise."""
        plc.clear_proxor_lines("never-existed")  # must not raise


class ProxorMeshRegistryTest(_RegistryCase):
    """Tests for the Proxor Mesh registry, which stores mesh vertex data for the drag-to-place visual."""

    def test_get_missing_returns_empty_list(self):
        """Requesting mesh for an unknown node should return an empty list, not raise."""
        self.assertEqual(plc.get_proxor_mesh("missing"), [])

    def test_set_then_get_roundtrip(self):
        """Setting mesh for a node and then getting it should return the same data."""
        verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        plc.set_proxor_mesh("loc1", verts)
        self.assertEqual(plc.get_proxor_mesh("loc1"), verts)

    def test_set_ignores_empty_node_name(self):
        """Setting mesh for an empty node name should be ignored, not stored."""
        plc.set_proxor_mesh("", [(0, 0, 0)])
        self.assertEqual(plc.proxor_mesh_registry, {})

    def test_clear_removes_entry(self):
        """Clearing mesh for a node should remove its entry from the registry."""
        plc.set_proxor_mesh("loc1", [(0, 0, 0)])
        plc.clear_proxor_mesh("loc1")
        self.assertEqual(plc.get_proxor_mesh("loc1"), [])

    def test_lines_and_mesh_are_independent(self):
        """Setting/clearing lines should not affect mesh, and vice versa."""
        plc.set_proxor_lines("loc1", [[(0, 0, 0), (1, 1, 1)]])
        plc.set_proxor_mesh("loc1", [(2, 2, 2)])
        plc.clear_proxor_lines("loc1")
        self.assertEqual(plc.get_proxor_lines("loc1"), [])
        self.assertEqual(plc.get_proxor_mesh("loc1"), [(2, 2, 2)])


class LabelRegistryTest(_RegistryCase):
    """Tests for the Label registry, which stores name/status pairs for the UI label on each locator."""

    def test_get_missing_returns_blank_dict(self):
        """Requesting a label for an unknown node should return a blank dict, not raise."""
        self.assertEqual(plc.get_label("missing"), {"name": "", "status": ""})

    def test_set_name_only_preserves_status(self):
        """Setting only the name should preserve the existing status."""
        plc.set_label("loc1", name="Foo", status="loading")
        plc.set_label("loc1", name="Bar")
        entry = plc.get_label("loc1")
        self.assertEqual(entry["name"], "Bar")
        self.assertEqual(entry["status"], "loading")

    def test_set_status_only_preserves_name(self):
        """Setting only the status should preserve the existing name."""
        plc.set_label("loc1", name="Foo")
        plc.set_label("loc1", status="done")
        entry = plc.get_label("loc1")
        self.assertEqual(entry["name"], "Foo")
        self.assertEqual(entry["status"], "done")

    def test_clear_removes_entry(self):
        """Clearing a label for a node should remove its entry from the registry."""
        plc.set_label("loc1", name="Foo")
        plc.clear_label("loc1")
        self.assertEqual(plc.get_label("loc1"), {"name": "", "status": ""})


if __name__ == "__main__":
    unittest.main()
