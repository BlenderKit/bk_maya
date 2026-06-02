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

from bk_maya.core import locator_state as plc  # noqa: E402


class _RegistryCase(unittest.TestCase):
    """Reset registries between tests so they don't leak across cases."""

    def setUp(self):
        plc.proxor_registry.clear()
        plc.proxor_mesh_registry.clear()
        plc.label_registry.clear()


class ProxorLinesRegistryTest(_RegistryCase):
    def test_get_missing_returns_empty_list(self):
        self.assertEqual(plc.get_proxor_lines("missing"), [])

    def test_set_then_get_roundtrip(self):
        lines = [[(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]]
        plc.set_proxor_lines("loc1", lines)
        self.assertEqual(plc.get_proxor_lines("loc1"), lines)

    def test_set_ignores_empty_node_name(self):
        plc.set_proxor_lines("", [[(0, 0, 0), (1, 1, 1)]])
        self.assertEqual(plc.proxor_registry, {})

    def test_clear_removes_entry(self):
        plc.set_proxor_lines("loc1", [[(0, 0, 0), (1, 1, 1)]])
        plc.clear_proxor_lines("loc1")
        self.assertEqual(plc.get_proxor_lines("loc1"), [])

    def test_clear_unknown_is_noop(self):
        plc.clear_proxor_lines("never-existed")  # must not raise


class ProxorMeshRegistryTest(_RegistryCase):
    def test_get_missing_returns_empty_list(self):
        self.assertEqual(plc.get_proxor_mesh("missing"), [])

    def test_set_then_get_roundtrip(self):
        verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        plc.set_proxor_mesh("loc1", verts)
        self.assertEqual(plc.get_proxor_mesh("loc1"), verts)

    def test_set_ignores_empty_node_name(self):
        plc.set_proxor_mesh("", [(0, 0, 0)])
        self.assertEqual(plc.proxor_mesh_registry, {})

    def test_clear_removes_entry(self):
        plc.set_proxor_mesh("loc1", [(0, 0, 0)])
        plc.clear_proxor_mesh("loc1")
        self.assertEqual(plc.get_proxor_mesh("loc1"), [])

    def test_lines_and_mesh_are_independent(self):
        plc.set_proxor_lines("loc1", [[(0, 0, 0), (1, 1, 1)]])
        plc.set_proxor_mesh("loc1", [(2, 2, 2)])
        plc.clear_proxor_lines("loc1")
        self.assertEqual(plc.get_proxor_lines("loc1"), [])
        self.assertEqual(plc.get_proxor_mesh("loc1"), [(2, 2, 2)])


class LabelRegistryTest(_RegistryCase):
    def test_get_missing_returns_blank_dict(self):
        self.assertEqual(plc.get_label("missing"), {"name": "", "status": ""})

    def test_set_name_only_preserves_status(self):
        plc.set_label("loc1", name="Foo", status="loading")
        plc.set_label("loc1", name="Bar")
        entry = plc.get_label("loc1")
        self.assertEqual(entry["name"], "Bar")
        self.assertEqual(entry["status"], "loading")

    def test_set_status_only_preserves_name(self):
        plc.set_label("loc1", name="Foo")
        plc.set_label("loc1", status="done")
        entry = plc.get_label("loc1")
        self.assertEqual(entry["name"], "Foo")
        self.assertEqual(entry["status"], "done")

    def test_clear_removes_entry(self):
        plc.set_label("loc1", name="Foo")
        plc.clear_label("loc1")
        self.assertEqual(plc.get_label("loc1"), {"name": "", "status": ""})


if __name__ == "__main__":
    unittest.main()
