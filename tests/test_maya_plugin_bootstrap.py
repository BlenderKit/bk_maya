"""Tests for Maya plug-in startup when Maya omits ``__file__``."""

import os
import sys
import types
import unittest
from unittest.mock import patch


class MayaPluginBootstrapTest(unittest.TestCase):
    def test_plugin_loads_without_file_global(self):
        plugin_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "bk_maya", "maya_plugin.py")
        )
        with open(plugin_path, encoding="utf-8") as source_file:
            source = source_file.read()

        maya = types.ModuleType("maya")
        maya.__path__ = []
        maya_api = types.ModuleType("maya.api")
        maya_api.__path__ = []
        openmaya = types.ModuleType("maya.api.OpenMaya")
        maya.api = maya_api
        maya_api.OpenMaya = openmaya

        original_path = sys.path[:]
        with patch.dict(
            sys.modules,
            {"maya": maya, "maya.api": maya_api, "maya.api.OpenMaya": openmaya},
        ):
            try:
                namespace = {"__name__": "maya_plugin_without_file"}
                exec(compile(source, plugin_path, "exec"), namespace)
                self.assertEqual(namespace["_BK_DIR"], os.path.dirname(plugin_path))

                namespace["_ensure_paths"]()
                self.assertIn(os.path.dirname(plugin_path), sys.path)
            finally:
                sys.path[:] = original_path


if __name__ == "__main__":
    unittest.main()
