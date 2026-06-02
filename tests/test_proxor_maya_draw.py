"""Tests for ``bk_proxor._maya.draw`` PRX → Maya coordinate converters.

These helpers are pure-Python (no Maya, no Qt, no numpy) so they run
under stock CPython in CI.
"""

from __future__ import annotations

import os
import sys
import unittest

# Make the vendored bk_proxor source importable without installing it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BK_PROXOR_SRC = os.path.join(_REPO_ROOT, "bk_maya", "bk_proxor", "src")
if _BK_PROXOR_SRC not in sys.path:
    sys.path.insert(0, _BK_PROXOR_SRC)

from bk_proxor._maya.draw import (
    prx_to_line_segments,
    prx_to_mesh_normals,
    prx_to_mesh_triangles,
)


def _payload(lines=None, mesh_pos=None, mesh_nrm=None):
    """Build a minimal PRX-style payload."""
    data: dict = {}
    if lines is not None:
        data["line"] = {"pos": lines}
    if mesh_pos is not None or mesh_nrm is not None:
        data["mesh"] = {}
        if mesh_pos is not None:
            data["mesh"]["pos"] = mesh_pos
        if mesh_nrm is not None:
            data["mesh"]["nrm"] = mesh_nrm
    return {"data": data}


class PrxLineSegmentsTest(unittest.TestCase):
    """``prx_to_line_segments`` — flat pair → ``[(a, b), ...]`` segments."""

    def test_empty_payload_returns_empty(self):
        """Missing or malformed data should not crash, just return an empty list."""
        self.assertEqual(prx_to_line_segments({}), [])
        self.assertEqual(prx_to_line_segments({"data": {}}), [])
        self.assertEqual(prx_to_line_segments(_payload(lines=[])), [])

    def test_non_dict_returns_empty(self):
        """A non-dict payload is malformed, should return an empty list."""
        self.assertEqual(prx_to_line_segments(None), [])  # type: ignore[arg-type]
        self.assertEqual(prx_to_line_segments("not a dict"), [])  # type: ignore[arg-type]

    def test_blender_zup_swap_yz_default(self):
        """Default ``axis_swap_yz=True`` swaps Y/Z (Blender-style)."""
        payload = _payload(lines=[(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        segs = prx_to_line_segments(payload, world_scale=100.0, axis_swap_yz=True)
        # 1 segment, swapped Y/Z, scaled by world_scale * 0.01 = 1.0
        self.assertEqual(segs, [[(1.0, 3.0, 2.0), (4.0, 6.0, 5.0)]])

    def test_maya_yup_negates_z(self):
        """``axis_swap_yz=False`` keeps Y, negates Z for Maya front-axis."""
        payload = _payload(lines=[(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        segs = prx_to_line_segments(payload, world_scale=100.0, axis_swap_yz=False)
        # Z is negated so Blender +Y (back) → Maya -Z (back)
        self.assertEqual(segs, [[(1.0, 2.0, -3.0), (4.0, 5.0, -6.0)]])

    def test_world_scale_combines_with_prx_metres_factor(self):
        """``world_scale * 0.01`` is the final multiplier (PRX→metres→host)."""
        payload = _payload(lines=[(100.0, 200.0, 300.0), (0.0, 0.0, 0.0)])
        segs = prx_to_line_segments(payload, world_scale=1.0, axis_swap_yz=False)
        # world_scale=1.0 → 1.0 * 0.01 = 0.01 (metres directly)
        self.assertEqual(segs[0][0], (1.0, 2.0, -3.0))

    def test_odd_endpoint_dropped(self):
        """A dangling endpoint (odd count) does not produce a segment."""
        payload = _payload(lines=[(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)])
        segs = prx_to_line_segments(payload, world_scale=100.0)
        self.assertEqual(len(segs), 1)

    def test_malformed_endpoint_skipped(self):
        """A malformed endpoint should be skipped, not crash the function."""
        payload = _payload(lines=[(1.0, 2.0, 3.0), ("bad",), (1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        segs = prx_to_line_segments(payload, world_scale=100.0, axis_swap_yz=False)
        # 1st pair malformed, 2nd OK
        self.assertEqual(len(segs), 1)


class PrxMeshTrianglesTest(unittest.TestCase):
    """``prx_to_mesh_triangles`` — flat tri vertices, 3 verts = 1 triangle."""

    def test_empty_payload(self):
        """Missing or malformed data should not crash, just return an empty list."""
        self.assertEqual(prx_to_mesh_triangles({}), [])
        self.assertEqual(prx_to_mesh_triangles(_payload(mesh_pos=[])), [])

    def test_maya_negates_z(self):
        """Maya Y-up with front-axis negation (``axis_swap_yz=False``) negates Z."""
        positions = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]
        verts = prx_to_mesh_triangles(_payload(mesh_pos=positions), world_scale=100.0, axis_swap_yz=False)
        self.assertEqual(verts, [(1.0, 2.0, -3.0), (4.0, 5.0, -6.0), (7.0, 8.0, -9.0)])

    def test_blender_swap(self):
        """Blender Z-up with Y/Z swap (``axis_swap_yz=True``) swaps Y/Z, no negation."""
        positions = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]
        verts = prx_to_mesh_triangles(_payload(mesh_pos=positions), world_scale=100.0, axis_swap_yz=True)
        self.assertEqual(verts, [(1.0, 3.0, 2.0), (4.0, 6.0, 5.0), (7.0, 9.0, 8.0)])

    def test_trailing_dangling_verts_discarded(self):
        """If the vertex count is not a multiple of 3, discard the trailing 1 or 2 verts."""
        # 5 verts → 1 complete triangle (3 verts), 2 dangling.
        positions = [(0, 0, 0)] * 5
        verts = prx_to_mesh_triangles(_payload(mesh_pos=positions), world_scale=100.0)
        self.assertEqual(len(verts), 3)

    def test_world_scale_factor(self):
        """``world_scale * 0.01`` is the final multiplier (PRX→metres→host)."""
        positions = [(100.0, 0.0, 0.0), (0.0, 100.0, 0.0), (0.0, 0.0, 100.0)]
        verts = prx_to_mesh_triangles(_payload(mesh_pos=positions), world_scale=100.0, axis_swap_yz=False)
        # 100 * (100 * 0.01) = 100.0 in Maya cm (i.e. 1 m)
        self.assertEqual(verts[0], (100.0, 0.0, 0.0))
        self.assertEqual(verts[1], (0.0, 100.0, 0.0))
        self.assertEqual(verts[2], (0.0, 0.0, -100.0))


class PrxMeshNormalsTest(unittest.TestCase):
    """``prx_to_mesh_normals`` — axis-swap only, no scaling."""

    def test_empty_payload(self):
        """Missing or malformed data should not crash, just return an empty list."""
        self.assertEqual(prx_to_mesh_normals({}), [])
        self.assertEqual(prx_to_mesh_normals(_payload(mesh_nrm=[])), [])

    def test_blender_swap(self):
        """Blender Z-up with Y/Z swap (``axis_swap_yz=True``) swaps Y/Z, no negation."""
        nrms = [(0.0, 0.0, 1.0), (1.0, 0.0, 0.0)]
        out = prx_to_mesh_normals(_payload(mesh_nrm=nrms), axis_swap_yz=True)
        self.assertEqual(out, [(0.0, 1.0, 0.0), (1.0, 0.0, 0.0)])

    def test_no_swap(self):
        """Maya Y-up with front-axis negation (``axis_swap_yz=False``) negates Z."""
        nrms = [(0.0, 0.0, 1.0), (1.0, 0.0, 0.0)]
        out = prx_to_mesh_normals(_payload(mesh_nrm=nrms), axis_swap_yz=False)
        self.assertEqual(out, nrms)


if __name__ == "__main__":
    unittest.main()
