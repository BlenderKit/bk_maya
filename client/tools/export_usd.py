"""Tools/export_usd.py - bundled Blendkit-client recipe.

Open a downloaded Blendkit .blend, unpack its packed textures into a
resolution-specific subfolder next to the .blend (mirroring the Blendkit
Blender addon's behaviour from ``unpack_asset_bg.py``), flatten any shader
node groups so the USD exporter can see Image Texture nodes, and export
the whole scene to a self-contained UsdPreviewSurface .usd file.

Recipe ABI:
    sys.argv = [..., "--", <params.json>]
    params.json keys:
        blend_path     : str  (required) - input .blend
        out_usd        : str  (required) - destination .usd
        max_resolution : str  (optional) - "512"/"1024"/"2048"/"4096"/"8192"/"ORIGINAL"
                                          used to pick the textures subfolder
                                          suffix (mirrors Blendkit addon)

Stdout protocol (consumed by bk_maya.core.blender_runner)::
    BK_STATUS   <stage>
    BK_PROGRESS <0..1> <msg>
    BK_DONE     <path>
    BK_ERROR    <msg>
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback

import bpy  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# Stdout protocol helpers
# ---------------------------------------------------------------------------


def _emit(tag: str, *parts: object) -> None:
    sys.stdout.write(f"{tag} {' '.join(str(p) for p in parts)}\n")
    sys.stdout.flush()


def status(s: str) -> None:
    """Emit a status update with the given stage name.

    Attributes:
        s: A string representing the current stage of the export process.
    """
    _emit("BK_STATUS", s)


def progress(frac: float, msg: str = "") -> None:
    """Emit a progress update with the given fraction (0..1) and optional message.

    Attributes:
        frac: A float between 0 and 1 indicating the completion percentage.
        msg: An optional string message providing additional context about the progress.
    """
    _emit("BK_PROGRESS", f"{frac:.3f}", msg)


def done(path: str) -> None:
    """Emit a done update with the given path.

    Attributes:
        path: A string representing the path of the completed export.
    """
    _emit("BK_DONE", path)


def error(msg: str) -> None:
    """Emit an error update with the given message.

    Attributes:
        msg: A string representing the error message.
    """
    _emit("BK_ERROR", msg)


def log(msg: str) -> None:
    """Helper to log a message with the BK_PROGRESS tag (for diagnostic purposes).

    Attributes:
        msg: A string representing the log message.
    """
    print(f"[export_usd] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Resolution → textures subfolder suffix (mirrors blendkit addon paths.py)
# ---------------------------------------------------------------------------

# Same detection tokens as blendkit_addon/unpack_asset_bg.py::get_resolution_from_file_path
_RES_FROM_PATH_TOKENS = {
    "_0_5K_": "resolution_0_5K",
    "_1K_": "resolution_1K",
    "_2K_": "resolution_2K",
    "_4K_": "resolution_4K",
    "_8K_": "resolution_8K",
}


def _texture_subdir(blend_path: str) -> tuple[str, str]:
    """Return ``(rel_dir, abs_dir)`` for the textures subfolder next to *blend_path*.

    Always ``//textures/`` (no resolution suffix). The Blendkit-Maya cache
    is one folder per (asset, resolution), so resolution disambiguation is
    already handled at the directory level. Crucially, Blender's
    ``wm.usd_export`` MaterialX path always authors texture asset paths as
    ``./textures/<basename>`` regardless of ``image.filepath``, so the
    unpacked files MUST land in a folder literally named ``textures``.
    """
    rel_dir = "//textures/"
    abs_dir = bpy.path.abspath(rel_dir, start=os.path.dirname(blend_path) + os.sep)
    return rel_dir, abs_dir


# ---------------------------------------------------------------------------
# Texture unpacking (mirrors blendkit_addon/unpack_asset_bg.py::unpack_asset)
# ---------------------------------------------------------------------------


def _resolve_target_path(tex_rel_dir: str, image: bpy.types.Image, source_path: str = "") -> str:
    """Return a ``//``-relative target path for *image* inside *tex_rel_dir*.

    Mirrors ``unpack_asset_bg.py::get_texture_filepath`` — collision-resolves
    by appending ``000``, ``001``, ... when another image already claims the
    same filepath. Returned path uses forward slashes (Blender convention)
    and starts with ``//`` so it stays relative to the .blend.

    Attributes:
        tex_rel_dir: The relative directory (e.g., "//textures/") where the image should be placed.
        image: The Blender image object for which to resolve the target path.
        source_path: An optional string representing the original source path of the image,
            used for UDIM/sequence support.

    Returns:
        A string representing the resolved target path for the image, relative to the .blend file.
    """
    if source_path:
        path = source_path
    elif len(image.packed_files) > 0:
        path = image.packed_files[0].filepath
    else:
        path = image.filepath
    path = (path or "").replace("\\", "/")
    base_name = bpy.path.basename(path) or image.name.split(".")[0]

    # Always forward-slash, always blend-relative.
    original = tex_rel_dir.rstrip("/") + "/" + base_name
    final = original
    i = 0
    while True:
        clash = any(other is not image and other.filepath == final for other in bpy.data.images)
        if not clash:
            return final
        stem, ext = os.path.splitext(original)
        final = f"{stem}{str(i).zfill(3)}{ext}"
        i += 1


def unpack_textures(blend_path: str) -> str:
    """Unpack packed images and relocate them into ``//textures<suffix>/``.

    Returns the absolute textures directory path (for diagnostic only —
    Blender-side image paths stay ``//``-relative).
    """
    rel_dir, abs_dir = _texture_subdir(blend_path)
    os.makedirs(abs_dir, exist_ok=True)
    log(f"unpack: rel_dir={rel_dir} abs_dir={abs_dir}")

    bpy.data.use_autopack = False

    unpacked = 0
    relocated = 0
    for image in bpy.data.images:
        if image.name == "Render Result":
            continue

        if len(image.packed_files) > 0:
            # UDIM / sequence support: per-packed-file target paths.
            paths = []
            for pf in image.packed_files:
                pf_path = _resolve_target_path(rel_dir, image, source_path=pf.filepath)
                pf.filepath = pf_path
                paths.append(pf_path)

            image_path = _resolve_target_path(rel_dir, image, source_path=image.filepath)
            image.filepath = image_path
            image.filepath_raw = image_path
            try:
                image.unpack(method="WRITE_ORIGINAL")
                unpacked += 1
                log(f"unpack: {image.name} -> {image_path} ({len(paths)} packed file(s))")
            except Exception as exc:
                log(f"unpack: FAILED for {image.name}: {exc}")
        else:
            fp = _resolve_target_path(rel_dir, image, source_path=image.filepath)
            if fp != image.filepath:
                image.filepath = fp
                image.filepath_raw = fp
                relocated += 1

    log(f"unpack: unpacked={unpacked} relocated={relocated}")
    return abs_dir


# ---------------------------------------------------------------------------
# Node group flattening
# ---------------------------------------------------------------------------
# Blender's wm.usd_export does NOT traverse inside material node groups.
# Many Blendkit assets wrap their shader in a "Blendkit Mat" group, so
# any Image Texture inside would be invisible to the exporter. Flatten every
# ShaderNodeGroup in every material before exporting.


def _flatten_material_node_groups() -> None:
    total = 0
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        n = _ungroup_all(mat.node_tree, mat)
        if n:
            log(f"flatten: {n} group(s) in material {mat.name!r}")
            total += n
    log(f"flatten: total groups expanded = {total}")


def _rename_shader_nodes_to_material() -> None:
    """Rename key shader nodes to carry the material name.

    Blender's USD exporter names emitted ``UsdPreviewSurface`` / ``UsdShade``
    prims after the *node name* — not the material datablock name. Default
    names like "Principled BSDF" / "Material Output" collide across materials
    in Maya/USD and make assets unreadable in the outliner. We rename:

    - the active output node           -> ``<Material>_Output``
    - the surface shader feeding it    -> ``<Material>_Surface``
    - every ShaderNodeBsdf*/Emission   -> ``<Material>_<orig-name-cleaned>``
    - every ShaderNodeTexImage         -> ``<Material>_<image-or-label>``
    """
    shader_ids = {
        "ShaderNodeBsdfPrincipled",
        "ShaderNodeBsdfDiffuse",
        "ShaderNodeBsdfGlossy",
        "ShaderNodeBsdfTransparent",
        "ShaderNodeBsdfGlass",
        "ShaderNodeBsdfRefraction",
        "ShaderNodeBsdfAnisotropic",
        "ShaderNodeBsdfHair",
        "ShaderNodeBsdfHairPrincipled",
        "ShaderNodeBsdfToon",
        "ShaderNodeBsdfVelvet",
        "ShaderNodeBsdfSheen",
        "ShaderNodeEmission",
        "ShaderNodeSubsurfaceScattering",
        "ShaderNodeMixShader",
        "ShaderNodeAddShader",
    }

    def _safe(name: str) -> str:
        # USD prim names: ascii, no spaces / dots / dashes.
        out = []
        for ch in name:
            if ch.isalnum() or ch == "_":
                out.append(ch)
            else:
                out.append("_")
        s = "".join(out).strip("_")
        return s or "Unnamed"

    total = 0
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        base = _safe(mat.name)
        nt = mat.node_tree

        # Find the active output node (the one Blender will export).
        output = None
        for n in nt.nodes:
            if n.bl_idname == "ShaderNodeOutputMaterial" and getattr(n, "is_active_output", False):
                output = n
                break
        if output is None:
            for n in nt.nodes:
                if n.bl_idname == "ShaderNodeOutputMaterial":
                    output = n
                    break

        if output is not None:
            output.name = f"{base}_Output"
            output.label = output.label or output.name
            total += 1
            # The node connected to its Surface input is the "main" shader.
            surf_in = output.inputs.get("Surface")
            if surf_in and surf_in.is_linked:
                src = surf_in.links[0].from_node
                src.name = f"{base}_Surface"
                src.label = src.label or src.name
                total += 1

        for n in nt.nodes:
            if n.bl_idname in shader_ids and not n.name.startswith(base + "_"):
                n.name = f"{base}_{_safe(n.bl_idname.replace('ShaderNode', ''))}"
                total += 1
            elif n.bl_idname == "ShaderNodeTexImage":
                img_name = ""
                if getattr(n, "image", None) is not None:
                    img_name = n.image.name
                tag = _safe(img_name or n.label or "Tex")
                if not n.name.startswith(base + "_"):
                    n.name = f"{base}_{tag}"
                    total += 1

    log(f"rename: shader nodes renamed = {total}")


def _ungroup_all(node_tree, owner) -> int:
    """Recursively ungroup all ShaderNodeGroups in *node_tree*.

    Attributes:
        node_tree: A Blender node tree to process.
        owner: The owner of the node tree (e.g., a Material) for context during ungrouping.

    Returns:
        The total number of groups expanded.
    """
    expanded = 0
    safety = 32  # avoid infinite loops on pathological graphs
    while safety > 0:
        groups = [n for n in node_tree.nodes if n.bl_idname == "ShaderNodeGroup" and n.node_tree]
        if not groups:
            break
        safety -= 1
        for gn in groups:
            try:
                with bpy.context.temp_override(
                    window=bpy.context.window,
                    area=None,
                    region=None,
                    material=owner if isinstance(owner, bpy.types.Material) else None,
                    edit_tree=node_tree,
                    active_node=gn,
                    selected_nodes=[gn],
                ):
                    for n in node_tree.nodes:
                        n.select = False
                    gn.select = True
                    node_tree.nodes.active = gn
                    bpy.ops.node.group_ungroup()
                expanded += 1
            except Exception as exc:
                log(f"flatten: ungroup op failed for {gn.name!r}: {exc}; manual inline")
                if _manual_inline(node_tree, gn):
                    expanded += 1
    return expanded


def _manual_inline(parent_tree, group_node) -> bool:
    """Fallback manual inlining if the group_ungroup operator fails (e.g. on complex node groups with reroutes).

    Attributes:
        parent_tree: The node tree into which the group should be inlined.
        group_node: The ShaderNodeGroup to be inlined.

    Returns:
        True if the group was successfully inlined, False otherwise.
    """
    try:
        inner = group_node.node_tree
        if inner is None:
            return False
        copies = {}
        for n in inner.nodes:
            if n.bl_idname in ("NodeGroupInput", "NodeGroupOutput"):
                continue
            new = parent_tree.nodes.new(n.bl_idname)
            for attr in ("label", "location", "width", "height"):
                with contextlib.suppress(Exception):
                    setattr(new, attr, getattr(n, attr))
            for prop in n.bl_rna.properties:
                if prop.is_readonly or prop.identifier in ("rna_type", "name"):
                    continue
                with contextlib.suppress(Exception):
                    setattr(new, prop.identifier, getattr(n, prop.identifier))
            copies[n] = new

        for lnk in inner.links:
            fn, fs = lnk.from_node, lnk.from_socket
            tn, ts = lnk.to_node, lnk.to_socket
            if fn.bl_idname == "NodeGroupInput":
                ext = group_node.inputs.get(fs.name)
                if ext and ext.is_linked:
                    parent_tree.links.new(ext.links[0].from_socket, copies[tn].inputs[ts.name])
                elif ext is not None:
                    with contextlib.suppress(Exception):
                        copies[tn].inputs[ts.name].default_value = ext.default_value

            elif tn.bl_idname == "NodeGroupOutput":
                ext = group_node.outputs.get(ts.name)
                if ext:
                    for ext_link in list(ext.links):
                        parent_tree.links.new(copies[fn].outputs[fs.name], ext_link.to_socket)
            else:
                parent_tree.links.new(copies[fn].outputs[fs.name], copies[tn].inputs[ts.name])

        parent_tree.nodes.remove(group_node)
        return True
    except Exception as exc:
        log(f"flatten: manual inline failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Single-material-per-mesh enforcement
# ---------------------------------------------------------------------------


def _is_rigged_or_unsplittable(obj) -> str:
    """Return a non-empty reason string if *obj* must NOT be split, else ''.

    Splitting a mesh that participates in a rig destroys deformation
    fidelity (shape keys are outright refused by ``mesh.separate``;
    multires data is lost; cross-piece smooth deformation breaks at the
    new seams). For those meshes we leave the multi-material binding
    intact and rely on USD ``GeomSubset`` instead.

    Attributes:
        obj: A Blender object to check for rigging or unsplittable features.

    Returns:
        A non-empty string describing the reason why the object cannot be split, or an empty string
    """
    me = obj.data
    if me is None:
        return "no mesh data"
    if getattr(me, "shape_keys", None) is not None:
        return "has shape keys"
    for mod in obj.modifiers:
        mtype = getattr(mod, "type", "")
        if mtype in {"ARMATURE", "MULTIRES", "CLOTH", "SOFT_BODY", "MESH_DEFORM", "SURFACE_DEFORM"}:
            return f"has {mtype} modifier"
    if obj.parent is not None and obj.parent.type == "ARMATURE":
        return "parented to armature"
    if obj.parent_type in {"BONE", "VERTEX", "VERTEX_3"}:
        return f"parent_type={obj.parent_type}"
    return ""


def _split_meshes_by_material() -> None:
    """Ensure every (safely-splittable) mesh has exactly one material slot.

    Blender's ``wm.usd_export`` writes per-face material binding via
    ``GeomSubset``, but Maya's mayaUsd sometimes drops slots 1..N and shows
    everything with slot 0's material. Splitting works around that — but
    must NOT be done to rigged/deformed meshes (see _is_rigged_or_unsplittable).

    Purely in-memory: the source .blend was already saved earlier so the
    cache stays intact.
    """
    mesh_objs: list = []
    skipped: list[tuple[str, str]] = []
    for obj in list(bpy.context.scene.objects):
        if obj.type != "MESH" or obj.data is None:
            continue
        if len(obj.material_slots) <= 1:
            continue
        reason = _is_rigged_or_unsplittable(obj)
        if reason:
            skipped.append((obj.name, reason))
            continue
        mesh_objs.append(obj)

    for name, reason in skipped:
        log(f"split: SKIP {name!r} — {reason} (relying on USD GeomSubset)")

    if not mesh_objs:
        log("split: no splittable multi-material meshes")
        return

    with contextlib.suppress(Exception):
        bpy.ops.object.mode_set(mode="OBJECT")

    split_count = 0
    for obj in mesh_objs:
        n_slots = len(obj.material_slots)
        try:
            for o in bpy.context.scene.objects:
                with contextlib.suppress(Exception):
                    o.select_set(False)
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj

            with bpy.context.temp_override(
                window=bpy.context.window,
                scene=bpy.context.scene,
                view_layer=bpy.context.view_layer,
                active_object=obj,
                selected_objects=[obj],
                selected_editable_objects=[obj],
                object=obj,
                edit_object=obj,
            ):
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.mesh.separate(type="MATERIAL")
                bpy.ops.object.mode_set(mode="OBJECT")
            split_count += 1
            log(f"split: {obj.name!r} ({n_slots} slots)")
        except Exception as exc:
            log(f"split: FAILED for {obj.name!r}: {exc}")
            with contextlib.suppress(Exception):
                bpy.ops.object.mode_set(mode="OBJECT")

    # Collapse each resulting mesh down to its single actually-used material.
    # IMPORTANT: pick by the face's material_index (Blender's canonical
    # face→slot mapping). NEVER assume slot 0 — the assigned material is
    # frequently slot 3/5/7 etc.
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        # Don't touch meshes we deliberately left alone above.
        if _is_rigged_or_unsplittable(obj):
            continue
        try:
            polys = obj.data.polygons
            if len(polys) == 0:
                continue
            used_idx = {p.material_index for p in polys}
            # Resolve to actual material objects (multiple indices can map
            # to the same material if the source had duplicate slots).
            used_mats = []
            seen = set()
            for idx in used_idx:
                if 0 <= idx < len(obj.material_slots):  # noqa: SIM108
                    m = obj.material_slots[idx].material
                else:
                    m = None
                key = m.name if m else None
                if key in seen:
                    continue
                seen.add(key)
                used_mats.append((idx, m))

            if len(used_mats) == 1:
                kept_idx, kept_mat = used_mats[0]
                kept_name = kept_mat.name if kept_mat else "<None>"
                obj.data.materials.clear()
                if kept_mat is not None:
                    obj.data.materials.append(kept_mat)
                # Reset every polygon to slot 0 since we just cleared.
                for p in polys:
                    p.material_index = 0
                log(f"split: collapsed {obj.name!r} -> slot[{kept_idx}] {kept_name!r}")
            else:
                mat_list = [f"slot[{i}]={m.name if m else '<None>'}" for i, m in used_mats]
                log(
                    f"split: {obj.name!r} still uses {len(used_mats)} materials "
                    f"after separate — leaving slots intact ({', '.join(mat_list)})",
                )
        except Exception as exc:
            log(f"split: slot cleanup failed for {obj.name!r}: {exc}")

    log(f"split: processed {split_count} multi-material mesh(es), skipped {len(skipped)}")


def _force_single_material_per_mesh() -> None:
    """Reduce every mesh to EXACTLY one material slot.

    Runs after the split pass to catch:
    - meshes whose ``mesh.separate`` left multiple slots referenced
      (Blender refuses to split when only one slot is actually used
      and we don't catch that earlier);
    - rigged meshes we deliberately did NOT split (those still may have
      unused slots that Maya/USD mis-bind to).

    Strategy:
      - count faces per material_index;
      - pick the slot with the most faces (dominant material) — this is
        a destructive choice for genuinely multi-material rigged meshes,
        but per user request: "keep only one material per mesh, remove
        the assignments of others" because Maya GeomSubset binding is
        unreliable;
      - reassign all faces to slot 0, keep only that one material.
    """
    from collections import Counter

    forced = 0
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        slots = obj.material_slots
        if len(slots) <= 1:
            continue
        polys = obj.data.polygons
        if len(polys) == 0:
            continue

        hist = Counter(p.material_index for p in polys)
        # Dominant slot index (the one most faces use).
        dom_idx, dom_count = hist.most_common(1)[0]
        dom_mat = slots[dom_idx].material if 0 <= dom_idx < len(slots) else None
        dom_name = dom_mat.name if dom_mat else "<None>"
        total = len(polys)
        lost = total - dom_count

        # Clear all slots then re-append only the dominant material.
        obj.data.materials.clear()
        if dom_mat is not None:
            obj.data.materials.append(dom_mat)
        for p in polys:
            p.material_index = 0
        forced += 1

        msg = f"force-single: {obj.name!r} -> {dom_name!r} (was slot[{dom_idx}], {dom_count}/{total} faces"
        if lost > 0:
            msg += f", {lost} face(s) reassigned from other slots"
        msg += ")"
        log(msg)

    log(f"force-single: reduced {forced} mesh(es) to single material")


def _remove_empty_uv_layers() -> None:
    """Drop UV layers that contain no usable data.

    A layer is considered empty if every UV is at (0, 0) or every face
    has zero UV area (a bake/scratch layer that was never unwrapped).
    Never removes the last remaining layer, and never removes a layer
    referenced by a ``ShaderNodeUVMap`` in any of the mesh's materials.
    """
    removed_total = 0
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        mesh = obj.data
        layers = mesh.uv_layers
        if len(layers) <= 1 or not mesh.polygons:
            continue

        # Names referenced by shader UV-Map nodes (never delete these).
        referenced: set[str] = set()
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or not mat.use_nodes or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.bl_idname == "ShaderNodeUVMap" and node.uv_map:
                    referenced.add(node.uv_map)

        to_remove = []
        for lyr in list(layers):
            if lyr.name in referenced:
                continue
            data = lyr.data
            # All-zero check: very cheap, catches the common case.
            all_zero = True
            for d in data:
                u, v = d.uv
                if u != 0.0 or v != 0.0:
                    all_zero = False
                    break
            if all_zero:
                to_remove.append(lyr.name)
                continue
            # Zero-area check: every face has degenerate UVs.
            any_area = False
            for poly in mesh.polygons:
                start = poly.loop_start
                n = poly.loop_total
                if n < 3:
                    continue
                # Shoelace area.
                area = 0.0
                u0, v0 = data[start].uv
                for i in range(1, n - 1):
                    u1, v1 = data[start + i].uv
                    u2, v2 = data[start + i + 1].uv
                    area += abs((u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0))
                if area > 1e-12:
                    any_area = True
                    break
            if not any_area:
                to_remove.append(lyr.name)

        # Keep at least one layer.
        if len(to_remove) >= len(layers):
            to_remove = to_remove[: len(layers) - 1]

        for name in to_remove:
            try:
                layers.remove(layers[name])
                removed_total += 1
            except Exception as exc:
                log(f"remove-empty-uv: failed on {obj.name!r}/{name!r}: {exc}")

        if to_remove:
            log(f"remove-empty-uv: {obj.name!r} dropped {to_remove}")

    log(f"remove-empty-uv: removed {removed_total} empty layer(s)")


def _fix_primary_uv_set() -> None:
    """Ensure the *correct* UV layer is marked ``active_render`` per mesh.

    Blender's ``wm.usd_export`` exports every UV layer, but the one
    flagged ``active_render`` becomes the primary ``st`` set in USD —
    i.e. the one Maya's shaders bind to by default. If an asset's
    ``active_render`` happens to be a bake/scratch UV (``Bake_Iris``,
    ``Gradient``, ...), the imported mesh in Maya will use that wrong
    set and the textures will look mangled.

    Heuristic, in order:
      1. The UV layer name referenced by a ``ShaderNodeUVMap`` that
         feeds an Image Texture in any material on the mesh.
      2. Otherwise the layer currently marked ``active`` (what the
         artist sees in the UV editor).
      3. Otherwise ``uv_layers[0]``.
    """
    from collections import Counter

    fixed = 0
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        mesh = obj.data
        layers = mesh.uv_layers
        if len(layers) == 0:
            continue

        # 1) gather UV-Map node names driving image textures
        used_names: Counter = Counter()
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or not mat.use_nodes or mat.node_tree is None:
                continue
            nt = mat.node_tree
            # Find image-texture nodes; walk back along their Vector input
            # to spot the UVMap node feeding them.
            for node in nt.nodes:
                if node.bl_idname != "ShaderNodeTexImage":
                    continue
                vec_in = node.inputs.get("Vector")
                if vec_in is None or not vec_in.is_linked:
                    continue
                src = vec_in.links[0].from_node
                # Walk through Mapping nodes if present
                while src.bl_idname == "ShaderNodeMapping":
                    m_in = src.inputs.get("Vector")
                    if m_in is None or not m_in.is_linked:
                        src = None  # type: ignore[assignment]
                        break
                    src = m_in.links[0].from_node
                if src is None:
                    continue
                if src.bl_idname == "ShaderNodeUVMap" and src.uv_map:
                    used_names[src.uv_map] += 1

        chosen = ""
        for name, _ in used_names.most_common():
            if name in layers:
                chosen = name
                break

        # 2) fallback: currently active UV layer
        if not chosen and layers.active is not None:
            chosen = layers.active.name

        # 3) fallback: first layer
        if not chosen:
            chosen = layers[0].name

        # Apply: mark chosen as both active and active_render.
        for lyr in layers:
            lyr.active_render = lyr.name == chosen
        with contextlib.suppress(Exception):
            layers.active = layers[chosen]

        fixed += 1
        if used_names:
            log(f"primary-uv: {obj.name!r} -> {chosen!r} (from material nodes; candidates={dict(used_names)})")
        else:
            log(f"primary-uv: {obj.name!r} -> {chosen!r} (fallback)")

    log(f"primary-uv: fixed {fixed} mesh(es)")


def _sanitize_meshes_for_usd_export() -> None:
    """Rebuild mesh CustomData so the USD exporter writes valid UV indices.

    Blender's ``wm.usd_export`` deduplicates faceVarying UVs into a
    ``values`` + ``indices`` pair. After destructive mesh ops (separate
    by material, slot pruning) the CustomData layout can get into a
    state where the exporter writes the compressed ``values`` array but
    silently omits the ``indices`` array — making the UVs unusable in
    Maya (values count != loop count, no indices to gather from).

    Round-tripping through ``object.convert(target='MESH')`` rebuilds
    the mesh data block from the evaluated depsgraph, producing a clean
    CustomData layout that the exporter handles correctly.

    Also calls ``mesh.validate()`` + ``mesh.update()`` as belt-and-braces.
    """
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer

    # Work on a stable snapshot of mesh objects.
    mesh_objs = [o for o in scene.objects if o.type == "MESH" and o.data]
    if not mesh_objs:
        return

    # Deselect all, then convert each mesh individually to keep memory
    # bounded and isolate failures.
    with contextlib.suppress(Exception):
        bpy.ops.object.select_all(action="DESELECT")

    rebuilt = 0
    failed = 0
    for obj in mesh_objs:
        try:
            # Skip linked / library data we can't convert.
            if obj.data.library is not None:
                continue
            # Select & activate just this object.
            with contextlib.suppress(Exception):
                bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            view_layer.objects.active = obj
            # Re-bake the mesh from the evaluated depsgraph.
            bpy.ops.object.convert(target="MESH")
            mesh = obj.data
            mesh.validate(verbose=False)
            mesh.update()
            rebuilt += 1
        except Exception as exc:
            failed += 1
            log(f"sanitize: convert failed for {obj.name!r}: {exc}")

    with contextlib.suppress(Exception):
        bpy.ops.object.select_all(action="DESELECT")

    log(f"sanitize: rebuilt {rebuilt} mesh(es), {failed} failed")


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


def _prepare_material_asset(asset_name: str = "", asset_id: str = "") -> bool:
    """Ensure a material asset is bound to a visible mesh before export.

    Blendkit material ``.blend`` files contain the material datablock but
    frequently have **no mesh** using it (the addon appends the material and
    assigns it to a user-picked object at import time). ``wm.usd_export`` only
    writes materials that are bound to exported geometry, so without this step
    the USD comes out empty and the Maya side reports "no material found".

    We locate the asset material and, if no visible mesh already uses it,
    create a simple plane and assign it. The plane's geometry/UVs are
    irrelevant: the Maya side only lifts the resulting ``shadingEngine`` and
    assigns it to the real target mesh, discarding the preview geometry.

    Returns True when a material was found (and bound), False otherwise.
    """
    mats = list(bpy.data.materials)
    if not mats:
        log("material asset: no materials in blend")
        return False

    # Pick the asset material: prefer one marked as an asset, then match by
    # Blendkit id, then by name, finally fall back to the first material.
    chosen = None
    for m in mats:
        if getattr(m, "asset_data", None) is not None:
            chosen = m
            break
    if chosen is None and asset_id:
        for m in mats:
            bk = getattr(m, "blenderkit", None)
            if bk is not None and getattr(bk, "id", "") == asset_id:
                chosen = m
                break
    if chosen is None and asset_name:
        for m in mats:
            if m.name == asset_name:
                chosen = m
                break
    if chosen is None:
        chosen = mats[0]
    log(f"material asset: chosen material = {chosen.name!r}")

    # Already bound to a visible mesh? Then there's nothing to do.
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if any(slot and slot.name == chosen.name for slot in obj.data.materials):
            log(f"material asset: already on mesh {obj.name!r}")
            return True

    # Build a unit plane and assign the material.
    mesh = bpy.data.meshes.new(f"{chosen.name}_preview")
    verts = [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]
    faces = [(0, 1, 2, 3)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    # A 0..1 UV map so any image textures resolve during export.
    uv = mesh.uv_layers.new(name="UVMap")
    uv_coords = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    for loop in mesh.loops:
        uv.data[loop.index].uv = uv_coords[loop.vertex_index]
    mesh.materials.append(chosen)

    obj = bpy.data.objects.new(f"{chosen.name}_preview", mesh)
    bpy.context.scene.collection.objects.link(obj)
    log(f"material asset: created preview plane for {chosen.name!r}")
    return True


def export_to_usd(
    blend_path: str, out_usd: str, asset_type: str = "model", asset_name: str = "", asset_id: str = ""
) -> None:
    """Export a .blend to .usd with the necessary pre-processing for Maya compatibility.

    Attributes:
      - blend_path: path to the source .blend file to export
      - out_usd: path to write the resulting .usd file
      - asset_type: Blendkit asset type ("material" gets a preview-mesh step)
      - asset_name: asset display name (used to locate the asset material)
      - asset_id: Blendkit asset id (used to locate the asset material)
    """
    status("Opening blend")
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    progress(0.05, "Opening blend")

    # Material assets carry the material datablock but usually no mesh — bind
    # it to a preview plane so wm.usd_export actually writes the material.
    if asset_type.lower() == "material":
        status("Preparing material")
        if not _prepare_material_asset(asset_name, asset_id):
            log("material asset: no material to bind (export may be empty)")

    # Make everything visible so the export captures the full asset.
    for obj in bpy.context.scene.objects:
        with contextlib.suppress(Exception):
            obj.hide_set(False)
        obj.hide_render = False
        obj.hide_viewport = False

    status("Unpacking textures")
    unpack_textures(blend_path)
    progress(0.30, "Unpacked textures")

    status("Flattening materials")
    _flatten_material_node_groups()
    progress(0.45, "Flattened materials")

    status("Renaming shader nodes")
    _rename_shader_nodes_to_material()
    progress(0.48, "Renamed shader nodes")

    # Save the relocated paths so wm.usd_export resolves them off disk
    # (no longer reads from packed bytes).
    try:
        bpy.ops.wm.save_as_mainfile(filepath=blend_path, compress=False)
        # Clean up Blender's automatic backup.
        with contextlib.suppress(Exception):
            os.remove(blend_path + "1")
    except Exception as exc:
        log(f"save_as_mainfile failed (non-fatal): {exc}")

    # IMPORTANT: do the destructive single-material split AFTER saving the
    # .blend, so the cached file remains intact for future re-exports.
    status("Splitting multi-material meshes")
    _split_meshes_by_material()
    progress(0.55, "Split materials")

    status("Forcing single material per mesh")
    _force_single_material_per_mesh()
    progress(0.58, "Single-material per mesh")

    status("Removing empty UV layers")
    _remove_empty_uv_layers()
    progress(0.59, "Removed empty UV layers")

    status("Fixing primary UV set")
    _fix_primary_uv_set()
    progress(0.60, "Primary UV set fixed")

    status("Sanitizing meshes for USD export")
    _sanitize_meshes_for_usd_export()
    progress(0.62, "Sanitized meshes")

    status("Generating USD")
    out_dir = os.path.dirname(out_usd) or "."
    os.makedirs(out_dir, exist_ok=True)

    desired = {
        "filepath": out_usd,
        "selected_objects_only": False,
        "visible_objects_only": True,
        "export_animation": True,
        "export_hair": True,
        "export_uvmaps": True,
        "export_normals": True,
        "export_materials": True,
        # Textures are already on disk via our unpack step — no need to
        # copy them again. This also keeps the USD asset-paths pointing at
        # the resolution-specific ``textures_2k/`` next to the blend.
        "export_textures": False,
        "overwrite_textures": False,
        "relative_paths": True,
        "generate_preview_surface": True,
        # Emit MaterialX network alongside UsdPreviewSurface. Maya's mayaUsd
        # reads MaterialX with higher PBR fidelity (roughness / metallic /
        # normal). Both networks coexist in the .usd.
        "generate_materialx_network": True,
        "root_prim_path": "/root",
        "default_prim_path": "/root",
        "export_global_forward_selection": "Y",
        "export_global_up_selection": "Z",
    }
    try:
        rna_props = bpy.ops.wm.usd_export.get_rna_type().properties
        rna = set(rna_props.keys())
    except Exception as exc:
        log(f"usd_export: rna introspection failed ({exc}); passing all kwargs")
        rna = set(desired)

    accepted = {k: v for k, v in desired.items() if k in rna}
    skipped = sorted(set(desired) - set(accepted))
    if skipped:
        log(f"usd_export: skipped unknown kwargs = {skipped}")
    # Loud diagnostic: confirm what we actually pass for texture-handling.
    tex_flags = {k: v for k, v in accepted.items() if "texture" in k.lower() or "relative" in k.lower()}
    log(f"usd_export: texture-related kwargs accepted = {tex_flags}")

    bpy.ops.wm.usd_export(**accepted)
    progress(0.99, "exported usd")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> dict:
    if "--" not in sys.argv:
        raise RuntimeError("missing '--' separator in argv")
    args = sys.argv[sys.argv.index("--") + 1 :]
    if not args:
        raise RuntimeError("missing args JSON path after '--'")
    with open(args[-1], encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    """Main entry point when run as a Blender script.

    Expects a single JSON file argument with the following keys:
      - blend_path: path to the source .blend file to export
      - out_usd: path to write the resulting .usd file
      - max_resolution: optional max texture resolution (e.g. "2048")
    """
    try:
        params = _parse_args()
    except Exception as exc:
        error(f"argument parsing failed: {exc}")
        return 2

    blend_path = params.get("blend_path") or ""
    out_usd = params.get("out_usd") or ""

    if not blend_path or not os.path.isfile(blend_path):
        error(f"blend_path not found: {blend_path!r}")
        return 1
    if not out_usd:
        error("out_usd not provided")
        return 1

    try:
        export_to_usd(
            blend_path,
            out_usd,
            asset_type=str(params.get("asset_type") or "model"),
            asset_name=str(params.get("asset_name") or ""),
            asset_id=str(params.get("asset_id") or ""),
        )
    except Exception as exc:
        error(f"export failed: {exc}")
        traceback.print_exc(file=sys.stdout)
        return 1

    progress(1.0, "done")
    done(out_usd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
