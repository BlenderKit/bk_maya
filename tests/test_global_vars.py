"""Tests for global variables in the bk_maya.core.global_vars module."""

import unittest

from bk_maya.core import global_vars


class TestProductionIsSet(unittest.TestCase):
    """Ensure that the SERVER variable in global_vars is set to the production URL."""

    def test_server_set_to_production(self):
        """Ensure the SERVER variable in global_vars is set to production."""
        expected_server = "https://www.blenderkit.com"
        self.assertEqual(
            global_vars.SERVER,
            expected_server,
            f"SERVER is not set to production. Expected: {expected_server}, Found: {global_vars.SERVER}",
        )
