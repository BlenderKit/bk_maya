"""tools/export_usd.py - bundled BlenderKit-client recipe.

Open a downloaded BlenderKit .blend, unpack its packed textures into a
resolution-specific subfolder next to the .blend (mirroring the BlenderKit
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
                                          suffix (mirrors blenderkit addon)

Stdout protocol (consumed by bk_maya.core.blender_runner)::
    BK_STATUS   <stage>
    BK_PROGRESS <0..1> <msg>
    BK_DONE     <path>
    BK_ERROR    <msg>
"""

from __future__ import annotations

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
    _emit("BK_STATUS", s)


def progress(frac: float, msg: str = "") -> None:
    _emit("BK_PROGRESS", f"{frac:.3f}", msg)


def done(path: str) -> None:
    _emit("BK_DONE", path)


def error(msg: str) -> None:
    _emit("BK_ERROR", msg)


def log(msg: str) -> None:
    print(f"[export_usd] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Resolution → textures subfolder suffix (mirrors blenderkit addon paths.py)
# ---------------------------------------------------------------------------

_RES_PROP_TO_KEY = {
    "512":  "resolution_0_5K",
    "1024": "resolution_1K",
    "2048": "resolution_2K",
    "4096": "resolution_4K",
    "8192": "resolution_8K",
    "ORIGINAL": "blend",
}

_RES_SUFFIX = {
    "blend":           "",
    "resolution_0_5K": "_05k",
    "resolution_1K":   "_1k",
    "resolution_2K":   "_2k",
    "resolution_4K":   "_4k",
    "resolution_8K":   "_8k",
}

# Same detection tokens as blenderkit_addon/unpack_asset_bg.py::get_resolution_from_file_path
_RES_FROM_PATH_TOKENS = {
    "_0_5K_": "resolution_0_5K",
    "_1K_":   "resolution_1K",
    "_2K_":   "resolution_2K",
    "_4K_":   "resolution_4K",
    "_8K_":   "resolution_8K",
}


def _resolution_from_path(path: str) -> str:
    for token, key in _RES_FROM_PATH_TOKENS.items():
        if token in path:
            return key
    return "blend"


def _texture_subdir(blend_path: str, max_resolution: str = "") -> tuple[str, str]:
    """Return ``(rel_dir, abs_dir)`` for the textures subfolder next to *blend_path*.

    ``rel_dir`` uses Blender's ``//``-relative-to-blend convention (e.g.
    ``//textures_2k/``). Matches addon behaviour exactly — keeps the .blend
    portable AND tells ``wm.usd_export`` the path is already next to the
    blend, so it doesn't create its own ``textures/`` folder and copy files.
    """
    res = _resolution_from_path(blend_path)
    if res == "blend" and max_resolution:
        res = _RES_PROP_TO_KEY.get(str(max_resolution), "blend")
    suffix = _RES_SUFFIX.get(res, "")
    rel_dir = f"//textures{suffix}/"
    abs_dir = bpy.path.abspath(rel_dir, start=os.path.dirname(blend_path) + os.sep)
    return rel_dir, abs_dir


# ---------------------------------------------------------------------------
# Texture unpacking (mirrors blenderkit_addon/unpack_asset_bg.py::unpack_asset)
# ---------------------------------------------------------------------------

def _resolve_target_path(tex_rel_dir: str, image: "bpy.types.Image", source_path: str = "") -> str:
    """Return a ``//``-relative target path for *image* inside *tex_rel_dir*.

    Mirrors ``unpack_asset_bg.py::get_texture_filepath`` — collision-resolves
    by appending ``000``, ``001``, ... when another image already claims the
    same filepath. Returned path uses forward slashes (Blender convention)
    and starts with ``//`` so it stays relative to the .blend.
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
        clash = any(
            other is not image and other.filepath == final
            for other in bpy.data.images
        )
        if not clash:
            return final
        stem, ext = os.path.splitext(original)
        final = f"{stem}{str(i).zfill(3)}{ext}"
        i += 1


def unpack_textures(blend_path: str, max_resolution: str = "") -> str:
    """Unpack packed images and relocate them into ``//textures<suffix>/``.

    Returns the absolute textures directory path (for diagnostic only —
    Blender-side image paths stay ``//``-relative).
    """
    rel_dir, abs_dir = _texture_subdir(blend_path, max_resolution)
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
            except Exception as exc:  # noqa: BLE001
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
# Many BlenderKit assets wrap their shader in a "BlenderKit Mat" group, so
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
    SHADER_IDS = {
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
            if n.bl_idname in SHADER_IDS and not n.name.startswith(base + "_"):
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
            except Exception as exc:  # noqa: BLE001
                log(f"flatten: ungroup op failed for {gn.name!r}: {exc}; manual inline")
                if _manual_inline(node_tree, gn):
                    expanded += 1
    return expanded


def _manual_inline(parent_tree, group_node) -> bool:
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
                try:
                    setattr(new, attr, getattr(n, attr))
                except Exception:
                    pass
            for prop in n.bl_rna.properties:
                if prop.is_readonly or prop.identifier in ("rna_type", "name"):
                    continue
                try:
                    setattr(new, prop.identifier, getattr(n, prop.identifier))
                except Exception:
                    pass
            copies[n] = new

        for lnk in inner.links:
            fn, fs = lnk.from_node, lnk.from_socket
            tn, ts = lnk.to_node, lnk.to_socket
            if fn.bl_idname == "NodeGroupInput":
                ext = group_node.inputs.get(fs.name)
                if ext and ext.is_linked:
                    parent_tree.links.new(ext.links[0].from_socket, copies[tn].inputs[ts.name])
                elif ext is not None:
                    try:
                        copies[tn].inputs[ts.name].default_value = ext.default_value
                    except Exception:
                        pass
            elif tn.bl_idname == "NodeGroupOutput":
                ext = group_node.outputs.get(ts.name)
                if ext:
                    for ext_link in list(ext.links):
                        parent_tree.links.new(copies[fn].outputs[fs.name], ext_link.to_socket)
            else:
                parent_tree.links.new(copies[fn].outputs[fs.name], copies[tn].inputs[ts.name])

        parent_tree.nodes.remove(group_node)
        return True
    except Exception as exc:  # noqa: BLE001
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

    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass

    split_count = 0
    for obj in mesh_objs:
        n_slots = len(obj.material_slots)
        try:
            for o in bpy.context.scene.objects:
                try:
                    o.select_set(False)
                except Exception:
                    pass
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
        except Exception as exc:  # noqa: BLE001
            log(f"split: FAILED for {obj.name!r}: {exc}")
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

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
                if 0 <= idx < len(obj.material_slots):
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
                mat_list = [
                    f"slot[{i}]={m.name if m else '<None>'}" for i, m in used_mats
                ]
                log(
                    f"split: {obj.name!r} still uses {len(used_mats)} materials "
                    f"after separate — leaving slots intact ({', '.join(mat_list)})"
                )
        except Exception as exc:  # noqa: BLE001
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
        dom_mat = (
            slots[dom_idx].material if 0 <= dom_idx < len(slots) else None
        )
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

        msg = (
            f"force-single: {obj.name!r} -> {dom_name!r} "
            f"(was slot[{dom_idx}], {dom_count}/{total} faces"
        )
        if lost > 0:
            msg += f", {lost} face(s) reassigned from other slots"
        msg += ")"
        log(msg)

    log(f"force-single: reduced {forced} mesh(es) to single material")


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export_to_usd(blend_path: str, out_usd: str, max_resolution: str = "") -> None:
    status("Opening blend")
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    progress(0.05, "Opening blend")

    # Make everything visible so the export captures the full asset.
    for obj in bpy.context.scene.objects:
        try:
            obj.hide_set(False)
        except Exception:
            pass
        obj.hide_render = False
        obj.hide_viewport = False

    status("Unpacking textures")
    unpack_textures(blend_path, max_resolution=max_resolution)
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
        try:
            os.remove(blend_path + "1")
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        log(f"save_as_mainfile failed (non-fatal): {exc}")

    # IMPORTANT: do the destructive single-material split AFTER saving the
    # .blend, so the cached file remains intact for future re-exports.
    status("Splitting multi-material meshes")
    _split_meshes_by_material()
    progress(0.55, "Split materials")

    status("Forcing single material per mesh")
    _force_single_material_per_mesh()
    progress(0.58, "Single-material per mesh")

    # Diagnostic dump: save the post-destructive state next to the source
    # so the user can open it in Blender and inspect what the USD exporter
    # was actually fed. Never overwrites the cached canonical .blend.
    try:
        mod_path = os.path.join(
            os.path.dirname(blend_path) or ".", "modified.blend"
        )
        bpy.ops.wm.save_as_mainfile(filepath=mod_path, copy=True, compress=False)
        try:
            os.remove(mod_path + "1")
        except OSError:
            pass
        log(f"diagnostic: wrote modified scene -> {mod_path}")
    except Exception as exc:  # noqa: BLE001
        log(f"diagnostic: failed to write modified.blend ({exc}); non-fatal")

    status("Generating USD")
    out_dir = os.path.dirname(out_usd) or "."
    os.makedirs(out_dir, exist_ok=True)

    desired = dict(
        filepath=out_usd,
        selected_objects_only=False,
        visible_objects_only=True,
        export_animation=False,
        export_hair=False,
        export_uvmaps=True,
        export_normals=True,
        export_materials=True,
        # Textures are already on disk via our unpack step — no need to
        # copy them again. This also keeps the USD asset-paths pointing at
        # the resolution-specific ``textures_2k/`` next to the blend.
        export_textures=False,
        overwrite_textures=False,
        relative_paths=True,
        generate_preview_surface=True,
        # Emit MaterialX network alongside UsdPreviewSurface. Maya's mayaUsd
        # reads MaterialX with higher PBR fidelity (roughness / metallic /
        # normal). Both networks coexist in the .usd.
        generate_materialx_network=True,
        root_prim_path="/root",
        default_prim_path="/root",
        export_global_forward_selection="Y",
        export_global_up_selection="Z",
    )
    try:
        rna_props = bpy.ops.wm.usd_export.get_rna_type().properties
        rna = set(rna_props.keys())
    except Exception as exc:  # noqa: BLE001
        log(f"usd_export: rna introspection failed ({exc}); passing all kwargs")
        rna = set(desired)

    accepted = {k: v for k, v in desired.items() if k in rna}
    skipped = sorted(set(desired) - set(accepted))
    if skipped:
        log(f"usd_export: skipped unknown kwargs = {skipped}")
    # Loud diagnostic: confirm what we actually pass for texture-handling.
    tex_flags = {k: v for k, v in accepted.items()
                 if "texture" in k.lower() or "relative" in k.lower()}
    log(f"usd_export: texture-related kwargs accepted = {tex_flags}")

    bpy.ops.wm.usd_export(**accepted)
    progress(0.99, "exported usd")

    # ------------------------------------------------------------------
    # Post-export sweep: remove any spurious ``textures/`` folder that
    # ``wm.usd_export`` may have created next to the .usd despite our
    # ``export_textures=False`` request. We already wrote a complete
    # resolution-specific ``textures<suffix>/`` next to the .blend
    # (which sits in the same directory as the .usd in our cache layout),
    # so a generic ``textures/`` here is always redundant.
    # ------------------------------------------------------------------
    try:
        _rel_dir, abs_dir = _texture_subdir(blend_path, max_resolution)
        our_name = os.path.basename(abs_dir.rstrip(os.sep).rstrip("/"))
        spurious = os.path.join(out_dir, "textures")
        if (
            our_name
            and our_name != "textures"
            and os.path.isdir(spurious)
            and os.path.isdir(abs_dir)
        ):
            import shutil as _shutil
            _shutil.rmtree(spurious, ignore_errors=True)
            log(f"post-export: removed spurious {spurious!r} "
                f"(canonical textures live in {our_name!r})")
    except Exception as exc:  # noqa: BLE001
        log(f"post-export: textures sweep failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> dict:
    if "--" not in sys.argv:
        raise RuntimeError("missing '--' separator in argv")
    args = sys.argv[sys.argv.index("--") + 1:]
    if not args:
        raise RuntimeError("missing args JSON path after '--'")
    with open(args[-1], encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    try:
        params = _parse_args()
    except Exception as exc:
        error(f"argument parsing failed: {exc}")
        return 2

    blend_path = params.get("blend_path") or ""
    out_usd    = params.get("out_usd") or ""
    max_res    = str(params.get("max_resolution") or "")

    if not blend_path or not os.path.isfile(blend_path):
        error(f"blend_path not found: {blend_path!r}")
        return 1
    if not out_usd:
        error("out_usd not provided")
        return 1

    try:
        export_to_usd(blend_path, out_usd, max_resolution=max_res)
    except Exception as exc:
        error(f"export failed: {exc}")
        traceback.print_exc(file=sys.stdout)
        return 1

    progress(1.0, "done")
    done(out_usd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
