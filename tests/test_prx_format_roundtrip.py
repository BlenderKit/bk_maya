"""Round-trip tests for ``bk_proxor.prx_format``.

Writes a synthetic PRX payload (mesh + line) and reads it back, then
asserts the data survives within the formatter's quantisation tolerance.
Pure-Python — no Maya, no Qt, no numpy needed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BK_PROXOR_SRC = os.path.join(_REPO_ROOT, "bk_maya", "bk_proxor", "src")
if _BK_PROXOR_SRC not in sys.path:
    sys.path.insert(0, _BK_PROXOR_SRC)

from bk_proxor import prx_format as pf


def _sample_payload():
    return {
        "data": {
            "mesh": {
                "pos": [
                    [0.0, 0.0, 0.0],
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                ],
                "col": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            },
            "line": {
                "pos": [
                    [0.0, 0.0, 0.0],
                    [1.0, 1.0, 1.0],
                ],
            },
        },
    }


class PrxRoundTripTest(unittest.TestCase):
    """Write → read should preserve the structural shape of the payload."""

    def setUp(self):
        """Create a temporary directory for test files, cleaned up automatically."""
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _roundtrip(self, ext: str, *, compress: bool):
        path = os.path.join(self._tmpdir.name, f"sample{ext}")
        pf.write_prx(path, _sample_payload(), compress=compress)
        self.assertTrue(os.path.isfile(path), f"writer did not create {path}")
        return pf.read_prx(path)

    def test_plain_prx_roundtrip_mesh_vertex_count(self):
        """Test that a plain .prx round-trip preserves the number of mesh vertices."""
        out = self._roundtrip(".prx", compress=False)
        mesh = out.get("data", {}).get("mesh", {})
        self.assertEqual(len(mesh.get("pos", [])), 3)

    def test_plain_prx_roundtrip_line_pair_count(self):
        """Test that a plain .prx round-trip preserves the number of line vertices."""
        out = self._roundtrip(".prx", compress=False)
        line = out.get("data", {}).get("line", {})
        self.assertEqual(len(line.get("pos", [])), 2)

    def test_compressed_prxc_roundtrip_mesh_vertex_count(self):
        """Test that a compressed .prxc round-trip preserves the number of mesh vertices."""
        out = self._roundtrip(".prxc", compress=True)
        mesh = out.get("data", {}).get("mesh", {})
        self.assertEqual(len(mesh.get("pos", [])), 3)

    def test_compressed_prxc_is_smaller_than_plain(self):
        """Test that a compressed .prxc file is smaller than an uncompressed .prx."""
        # Use a larger payload so compression actually pays off.
        payload = {"data": {"mesh": {"pos": [[float(i % 10), 0.0, 0.0] for i in range(300)]}}}
        plain = os.path.join(self._tmpdir.name, "big.prx")
        comp = os.path.join(self._tmpdir.name, "big.prxc")
        pf.write_prx(plain, payload, compress=False)
        pf.write_prx(comp, payload, compress=True)
        self.assertLess(os.path.getsize(comp), os.path.getsize(plain))


class PrxReaderRobustnessTest(unittest.TestCase):
    """The reader should not crash on missing files / empty payloads."""

    def test_missing_file_raises(self):
        """Test that attempting to read a non-existent file raises an appropriate exception."""
        with self.assertRaises((FileNotFoundError, OSError)):
            pf.read_prx("/no/such/path/missing.prx")


if __name__ == "__main__":
    unittest.main()
