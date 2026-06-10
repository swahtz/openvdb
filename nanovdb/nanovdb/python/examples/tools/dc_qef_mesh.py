# Copyright Contributors to the OpenVDB Project
# SPDX-License-Identifier: Apache-2.0
"""GPU Dual-Contouring (QEF) surface mesher for NanoVDB SDFs -- prototype.

A device-side `VolumeToMesh`-style tool, prototyped in pure CuPy on top of the
NanoVDB Python GPU bindings (feature/nanovdb_python_gpu). The input is an SDF
narrow-band level set (a FloatGrid, or an OnIndex grid carrying the SDF in blind
channel 0); the output is a triangulated OBJ or PLY.

Why this maps cleanly onto the bindings (see NanoVDB 2.0 VBM paper, sec. 4):
  * The IndexGrid's forward map (coord -> sequential active-voxel index) is the
    vertex compaction a GPU dual-contourer needs: each surface cell's value index
    IS its mesh-vertex slot, with no dedup pass.
  * tools.cuda.gatherBoxStencilColumns(grid, arange, ...) gives, per active voxel,
    the value index of chosen 3x3x3 box-stencil neighbours -- the inverse map the
    VBM provides. From that ONE gather we get (a) connectivity, (b) the 8 cell-
    corner SDF values (sdf[neighbor_index]), and (c) corner gradients.
  * tools.cuda.activeVoxelCoords(grid) recovers index-space coords for placing
    vertices in world space.

Algorithm (uniform-resolution Dual Contouring, no seam pass):
  1. cell(k) = the cube [p, p+1]^3 anchored at active voxel k (p = coord[k]); its
     8 corners are 8 box-stencil spokes. A cell is a *surface cell* if its corner
     signs straddle the isovalue AND all 8 corners are active (the latter keeps
     the mesh watertight within the narrow band -- needs a >=3-voxel band).
  2. one vertex per surface cell, placed by minimising a QEF built from the edge
     zero-crossings and their SDF-gradient normals (regularised toward the
     crossing centroid, clamped to the cell -> robust, sharp-feature preserving).
  3. one quad per bipolar minimal grid-edge, joining the 4 cells around it;
     vertices resolved through the neighbour-index table. Triangulated + each
     triangle oriented outward by the SDF gradient.

Run:
    python dc_qef_mesh.py --shape sphere   out.obj          # synthetic self-test
    python dc_qef_mesh.py --shape csg      out.obj          # box n sphere (sharp)
    python dc_qef_mesh.py in.nvdb          out.{obj,ply} [--iso 0] [--method qef|nets]

Vertex-count control (cluster-collapse decimation; default = one vertex/surface cell):
    --reduce F          uniform F x F x F collapse  (~F^2 fewer vertices)
    --adaptivity A      curvature-adaptive (A in [0,1.5]): collapse flat blocks, keep
                        features (higher A = more aggressive; 1.5 = collapse all)

Requires: a CUDA build of nanovdb with tools.cuda.{gatherBoxStencilColumns,
activeVoxelCoords}, CuPy, and a GPU. Env on this branch:
    PYTHONPATH=/workspace/build_pygpu/py_import \
    /home/vscode/miniforge3/envs/nanovdb/bin/python dc_qef_mesh.py ...
"""
import argparse
import os
import sys
import tempfile
import time

import numpy as np

import nanovdb


# ---- box-stencil geometry ------------------------------------------------
# A 3x3x3 box-stencil neighbour is addressed by a "spoke" index in [0, 27):
# spoke = (di+1)*9 + (dj+1)*3 + (dk+1) for offsets di,dj,dk in {-1,0,+1}. The
# centre (the voxel itself) is spoke 13.
def _spoke(di, dj, dk):
    return (di + 1) * 9 + (dj + 1) * 3 + (dk + 1)


# The 8 corners of a cell -- the cube [0,1]^3 anchored at the voxel -- as offsets
# and as the box-stencil spokes that fetch each corner's value/neighbour.
CORNER_OFFSETS = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1),
                           (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1)], dtype=np.float64)
CORNER_SPOKES = np.array([_spoke(*offset.astype(int)) for offset in CORNER_OFFSETS])

# The 12 edges of the cube, each as a pair of corner indices (a unit step apart).
CUBE_EDGES = np.array([(0, 1), (2, 4), (3, 5), (6, 7),     # the 4 edges along x
                       (0, 2), (1, 4), (3, 6), (5, 7),     # the 4 edges along y
                       (0, 3), (1, 5), (2, 6), (4, 7)],    # the 4 edges along z
                      dtype=np.int64)

# The 6 face-neighbour spokes, in -x,+x,-y,+y,-z,+z order, for central-difference
# gradients.
FACE_SPOKES = np.array([_spoke(-1, 0, 0), _spoke(1, 0, 0),
                        _spoke(0, -1, 0), _spoke(0, 1, 0),
                        _spoke(0, 0, -1), _spoke(0, 0, 1)])

# Connectivity: for each minimal grid edge leaving a voxel p in the +x/+y/+z
# direction, this gives (the forward-neighbour spoke, [the 4 cells sharing that
# edge, as spokes of p, in rotational order]). Spoke 13 in a fan is p itself.
EDGE_FANS = {
    "x": (_spoke(1, 0, 0), [13, _spoke(0, 0, -1), _spoke(0, -1, -1), _spoke(0, -1, 0)]),
    "y": (_spoke(0, 1, 0), [13, _spoke(0, 0, -1), _spoke(-1, 0, -1), _spoke(-1, 0, 0)]),
    "z": (_spoke(0, 0, 1), [13, _spoke(0, -1, 0), _spoke(-1, -1, 0), _spoke(-1, 0, 0)]),
}

# The mesher only ever touches ~14 of the 27 spokes (the 8 corners, 6 faces, and
# the connectivity fans), so it gathers JUST those columns -- an (N, len) table
# instead of (N, 27), the dominant array. SPOKE_TO_COLUMN maps a spoke index to
# its column in that compact table, and the *_COLUMNS constants are the spoke
# constants above pre-mapped to those columns.
USED_SPOKES = sorted(set(int(spoke) for spoke in FACE_SPOKES)
                     | set(int(spoke) for spoke in CORNER_SPOKES)
                     | {forward for forward, _fan in EDGE_FANS.values()}
                     | {spoke for _forward, fan in EDGE_FANS.values() for spoke in fan})
USED_SPOKES_ARRAY = np.asarray(USED_SPOKES, dtype=np.int32)
SPOKE_TO_COLUMN = {spoke: column for column, spoke in enumerate(USED_SPOKES)}
FACE_COLUMNS = [SPOKE_TO_COLUMN[int(spoke)] for spoke in FACE_SPOKES]
CORNER_COLUMNS = [SPOKE_TO_COLUMN[int(spoke)] for spoke in CORNER_SPOKES]
CENTRE_COLUMN = SPOKE_TO_COLUMN[13]
EDGE_FAN_COLUMNS = [(SPOKE_TO_COLUMN[forward], [SPOKE_TO_COLUMN[spoke] for spoke in fan])
                    for forward, fan in EDGE_FANS.values()]


# ---- input: any .nvdb SDF -> device OnIndex grid + value-indexed SDF ------
def read_sdf_to_device(cp, path):
    """Read a FloatGrid (or OnIndex+SDF) .nvdb -> (device_handle, device_grid,
    sdf, voxel_size, world_translation).

    A FloatGrid is first baked to an OnIndex grid with the SDF in blind channel 0
    (the form the VBM gather operates on), then round-tripped to the device. `sdf`
    is the value-indexed signed distance field (length active_voxel_count + 1;
    slot 0 is the background). `world_translation` is the index->world offset
    (this assumes an axis-aligned uniform map)."""
    host_handle = nanovdb.io.readGrid(path)
    grid_type = host_handle.gridType(0)
    host_grid = host_handle.grid(0)
    voxel_size = float(host_grid.voxelSize()[0])
    world_translation = _world_translation(host_grid, voxel_size)
    if grid_type == nanovdb.GridType.Float:
        onindex_handle = nanovdb.tools.createOnIndexGrid(
            host_grid, channels=1, include_stats=False, include_tiles=False)
        sdf = np.asarray(onindex_handle.grid(0).getBlindData(0), dtype=np.float32)
        tmp = tempfile.NamedTemporaryFile(suffix=".nvdb", delete=False); tmp.close()
        nanovdb.io.writeGrid(tmp.name, onindex_handle)
        device_handle = nanovdb.io.deviceReadGrid(tmp.name)
        os.unlink(tmp.name)
    elif grid_type == nanovdb.GridType.OnIndex:
        if host_grid.blindDataCount() == 0:
            raise SystemExit(f"{path}: OnIndex grid has no blind-data SDF channel.")
        sdf = np.asarray(host_grid.getBlindData(0), dtype=np.float32)
        device_handle = nanovdb.io.deviceReadGrid(path)
    else:
        raise SystemExit(f"{path}: unsupported grid type {grid_type} (expected Float or OnIndex).")
    device_handle.deviceUpload(0, True)
    device_grid = device_handle.deviceGrid(0)
    return device_handle, device_grid, cp.asarray(sdf), voxel_size, world_translation


def _world_translation(host_grid, voxel_size):
    """index->world translation for an axis-aligned uniform map, or (0,0,0)."""
    try:
        world_bbox, index_bbox = host_grid.worldBBox(), host_grid.indexBBox()
        world_min = np.array([world_bbox[0][0], world_bbox[0][1], world_bbox[0][2]], dtype=np.float64)
        index_min = np.array([index_bbox[0][0], index_bbox[0][1], index_bbox[0][2]], dtype=np.float64)
        return world_min - index_min * voxel_size
    except Exception:
        return np.zeros(3, dtype=np.float64)


def sphere_to_device(cp, radius=20.0, voxel_size=1.0):
    """A level-set sphere baked to OnIndex+SDF on the device (synthetic self-test)."""
    sphere = nanovdb.tools.createLevelSetSphere(radius=radius, voxelSize=voxel_size, name="sphere")
    return _bake_float_to_device(cp, sphere, voxel_size)


def csg_to_device(cp, voxel_size=0.05):
    """A box-intersect-sphere SDF (a sphere with three flat sharp faces) so the
    QEF's sharp-feature handling is visible. Built on the host via build.FloatGrid."""
    import math
    sphere_radius, box_half = 0.8, 0.6                # world units
    lo, hi = int(-1.0 / voxel_size), int(1.0 / voxel_size)
    band = 3.0 * voxel_size
    builder = nanovdb.tools.build.FloatGrid(band)     # background = +band (outside)
    builder.setName("csg")
    builder.setTransform(voxel_size)
    accessor = builder.getAccessor()
    for i in range(lo, hi + 1):
        for j in range(lo, hi + 1):
            for k in range(lo, hi + 1):
                x, y, z = i * voxel_size, j * voxel_size, k * voxel_size
                dist_sphere = math.sqrt(x * x + y * y + z * z) - sphere_radius
                dist_box = max(abs(x) - box_half, abs(y) - box_half, abs(z) - box_half)
                signed_dist = max(dist_sphere, dist_box)          # CSG intersection
                if abs(signed_dist) <= band:
                    accessor.setValue(nanovdb.math.Coord(i, j, k), float(signed_dist))
    return _bake_float_to_device(cp, builder.to_nanovdb(), voxel_size)


def _bake_float_to_device(cp, float_handle, voxel_size):
    """FloatGrid handle -> (device_handle, device_grid, sdf, voxel_size, zero
    translation), going through an OnIndex grid with the SDF in blind channel 0."""
    onindex_handle = nanovdb.tools.createOnIndexGrid(
        float_handle.grid(0), channels=1, include_stats=False, include_tiles=False)
    sdf = np.asarray(onindex_handle.grid(0).getBlindData(0), dtype=np.float32)
    tmp = tempfile.NamedTemporaryFile(suffix=".nvdb", delete=False); tmp.close()
    nanovdb.io.writeGrid(tmp.name, onindex_handle)
    device_handle = nanovdb.io.deviceReadGrid(tmp.name)
    os.unlink(tmp.name)
    device_handle.deviceUpload(0, True)
    return device_handle, device_handle.deviceGrid(0), cp.asarray(sdf), voxel_size, np.zeros(3)


# ---- the mesher ----------------------------------------------------------
def _build_triangles(cp, neighbor_index, sdf, cell_vertex, iso, value_count):
    """Dual-contouring connectivity. For each bipolar minimal +x/+y/+z grid edge,
    emit one quad joining the 4 cells around that edge (kept only if all 4 are
    surface cells), with cells mapped to vertex rows through `cell_vertex`. Only
    the bipolar edges are gathered (far fewer than every edge). Returns the quads
    triangulated into an (T,3) array of whatever vertex rows `cell_vertex` holds."""
    value_index = cp.arange(value_count, dtype=cp.int32)
    quads = []
    for forward_column, fan_columns in EDGE_FAN_COLUMNS:
        forward_neighbor = neighbor_index[:, forward_column]
        edge_is_bipolar = ((value_index > 0) & (forward_neighbor > 0)
                           & ((sdf < iso) != (sdf[forward_neighbor] < iso)))
        edge_voxels = cp.nonzero(edge_is_bipolar)[0].astype(cp.int32)        # (e,)
        surrounding_cells = cp.stack(
            [edge_voxels if column == CENTRE_COLUMN else neighbor_index[edge_voxels, column]
             for column in fan_columns], axis=1)                            # (e,4)
        cell_vertices = cell_vertex[surrounding_cells]                       # (e,4)
        quads.append(cell_vertices[(cell_vertices >= 0).all(axis=1)])
        del forward_neighbor, edge_is_bipolar, edge_voxels, surrounding_cells, cell_vertices
    quads = cp.concatenate(quads, axis=0)                                   # (Q,4)
    return _triangulate_quads(cp, quads)                                    # (T,3)


def _triangulate_quads(cp, quads):
    """Split each cyclic quad (v0,v1,v2,v3) into two triangles, choosing the diagonal
    that is NOT collapsed onto a single vertex. Decimation contracts cells onto shared
    cluster vertices, so a quad can come back as e.g. [A,B,A,C]: the default v0-v2
    diagonal is then degenerate on BOTH triangles -> both dropped -> a hole. When v0==v2
    (and v1!=v3) we instead split on the v1-v3 diagonal, which still yields two good
    triangles. (For the full-res mesh all four are distinct, so this is a no-op there.)
    Winding is made consistent by _orient_triangles downstream regardless."""
    v0, v1, v2, v3 = quads[:, 0], quads[:, 1], quads[:, 2], quads[:, 3]
    use_diagonal_13 = (v0 == v2) & (v1 != v3)
    first = cp.where(use_diagonal_13[:, None],
                     cp.stack([v0, v1, v3], axis=1), cp.stack([v0, v1, v2], axis=1))
    second = cp.where(use_diagonal_13[:, None],
                      cp.stack([v1, v2, v3], axis=1), cp.stack([v0, v2, v3], axis=1))
    return cp.concatenate([first, second], axis=0)


def _clean_triangles(cp, triangles):
    """For decimated meshes only: drop degenerate triangles -- those with a repeated
    vertex, left when all the cells of a contracted quad fall in one cluster.

    We deliberately do NOT deduplicate coincident triangles. Several fine quads can
    legitimately contract onto the same 3 clusters at a fold or pinch (common on thin
    features like a neuron's axon), and each is still needed to keep the surface
    closed: removing the 'duplicate' tears a hole there (a boundary edge -- a
    see-through broken polygon). Keeping them leaves benign coincident faces instead
    of holes (verified on T1: dedup -> 7.7k boundary edges; no dedup -> 0)."""
    v0, v1, v2 = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    return triangles[(v0 != v1) & (v1 != v2) & (v0 != v2)]


def _prune_unreferenced(cp, vertices, triangles):
    """Drop vertices that no triangle references (free points left behind when
    decimation collapses/dedups every triangle that used a cluster) and reindex the
    triangles. Returns (vertices, triangles). A no-op when every vertex is used."""
    if triangles.shape[0] == 0:
        return vertices[:0], triangles
    is_used = cp.zeros(vertices.shape[0], dtype=cp.bool_)
    is_used[triangles.reshape(-1)] = True
    if bool(is_used.all()):
        return vertices, triangles
    kept = cp.nonzero(is_used)[0]
    remap = cp.empty(vertices.shape[0], dtype=cp.int32)
    remap[kept] = cp.arange(kept.shape[0], dtype=cp.int32)
    return vertices[kept], remap[triangles].astype(cp.int32)


def _pack_coords(cp, coords):
    """Pack a nonnegative (n,3) int64 coordinate into one int64 key per row
    (mixed-radix on the per-axis spans, so distinct coords get distinct keys)."""
    span = coords.max(axis=0) + 1
    return (coords[:, 0] * span[1] + coords[:, 1]) * span[2] + coords[:, 2]


def _dense_ids(cp, key):
    """Map arbitrary int64 keys to a dense [0, num_unique) labelling. Returns
    (label_per_row int32, num_unique)."""
    unique_keys, label = cp.unique(key, return_inverse=True)
    return label.astype(cp.int32), int(unique_keys.shape[0])


def _cell_geometry(cp, corner_sdf, corner_index, gradient,
                   edge_a, edge_b, corner_a_offset, edge_vector, iso):
    """Per-cell dual-contouring geometry for a slice of surface cells. Returns
    (edge_normal, edge_is_bipolar, bipolar_mask, crossing_local):
      edge_normal     (c,12,3)  normalised SDF-gradient normal at each edge
                                crossing, zeroed on non-bipolar edges
      edge_is_bipolar (c,12)    True where the edge's two corners straddle iso
      bipolar_mask    (c,12,1)  edge_is_bipolar as float, for masked sums
      crossing_local  (c,12,3)  zero-crossing point in the cell's [0,1]^3 frame"""
    sdf_a, sdf_b = corner_sdf[:, edge_a], corner_sdf[:, edge_b]             # (c,12)
    edge_is_bipolar = (sdf_a < iso) != (sdf_b < iso)
    sdf_difference = sdf_b - sdf_a
    crossing_t = cp.clip(cp.where(cp.abs(sdf_difference) > 1e-12,
                                  (iso - sdf_a) / sdf_difference, cp.float32(0.5)),
                         0.0, 1.0).astype(cp.float32)
    crossing_local = corner_a_offset[None] + crossing_t[..., None] * edge_vector   # (c,12,3)
    corner_gradient = gradient[corner_index]                               # (c,8,3)
    gradient_a, gradient_b = corner_gradient[:, edge_a], corner_gradient[:, edge_b]
    edge_normal = gradient_a + crossing_t[..., None] * (gradient_b - gradient_a)
    edge_normal = edge_normal / (cp.linalg.norm(edge_normal, axis=2, keepdims=True) + 1e-12)
    bipolar_mask = edge_is_bipolar[..., None].astype(cp.float32)
    return edge_normal * bipolar_mask, edge_is_bipolar, bipolar_mask, crossing_local


def _assign_clusters(cp, cell_origin, cell_normal, reduce_factor, adaptivity, num_cells):
    """Group surface cells into clusters for decimation. Returns (cluster_id (m,)
    int32, num_clusters, block_size).

    Phase 1 (adaptivity <= 0): uniform block_size^3 blocks, block_size = --reduce.

    Phase 2 (adaptivity > 0): a cell joins its block^3 block only if that block is
    flat (its summed unit normals have magnitude/count >= cos(adaptivity*60deg));
    cells in non-flat (feature) blocks stay at full resolution. Either way every
    cell maps to exactly one cluster, i.e. the fine cell set is *contracted* -- so
    the connectivity stays watertight."""
    import cupyx
    if adaptivity > 0 and int(reduce_factor) <= 1:
        block_size = 8                                # default coarse block in adaptive mode
    else:
        block_size = max(1, int(reduce_factor))
    local_coord = cell_origin.astype(cp.int64)
    local_coord = local_coord - local_coord.min(axis=0)          # (m,3) >= 0
    block_coord = local_coord // block_size
    if adaptivity <= 0.0:                             # Phase 1: uniform blocks
        cluster_id, num_clusters = _dense_ids(cp, _pack_coords(cp, block_coord))
        return cluster_id, num_clusters, block_size

    # Phase 2: keep fine detail where the surface curves. A block is "flat" if its
    # member cell-normals point the same way (the summed unit normals stay long).
    flat_cos_threshold = float(np.cos(np.radians(adaptivity * 60.0)))
    _unique_blocks, block_of_cell = cp.unique(_pack_coords(cp, block_coord), return_inverse=True)
    num_blocks = int(_unique_blocks.shape[0])
    normal_sum = cp.zeros((num_blocks, 3), dtype=cp.float32)
    cell_count = cp.zeros(num_blocks, dtype=cp.float32)
    cupyx.scatter_add(normal_sum, block_of_cell, cell_normal)
    cupyx.scatter_add(cell_count, block_of_cell, cp.ones(num_cells, dtype=cp.float32))
    block_is_flat = (cp.linalg.norm(normal_sum, axis=1) / cp.maximum(cell_count, 1.0)) >= flat_cos_threshold
    cell_in_flat_block = block_is_flat[block_of_cell]            # (m,)
    # Build one key space holding both coarse-block ids (flat cells) and fine-cell
    # ids (feature cells); a constant offset keeps the two from colliding.
    span = local_coord.max(axis=0) + 1
    fine_key_count = int(span[0] * span[1] * span[2])
    coarse_key = (block_coord[:, 0] * span[1] + block_coord[:, 1]) * span[2] + block_coord[:, 2]
    fine_key = (local_coord[:, 0] * span[1] + local_coord[:, 1]) * span[2] + local_coord[:, 2]
    cluster_key = cp.where(cell_in_flat_block, fine_key_count + coarse_key, fine_key)
    cluster_id, num_clusters = _dense_ids(cp, cluster_key)
    return cluster_id, num_clusters, block_size


def _orient_triangles(cp, vertices, triangles, vertex_normal):
    """Orient each triangle so its geometric normal points the same way as the
    per-vertex SDF-gradient reference normal `vertex_normal`; returns int32 (T,3)."""
    p0, p1, p2 = vertices[triangles[:, 0]], vertices[triangles[:, 1]], vertices[triangles[:, 2]]
    face_normal = cp.cross(p1 - p0, p2 - p0)
    outward_reference = (vertex_normal[triangles[:, 0]] + vertex_normal[triangles[:, 1]]
                         + vertex_normal[triangles[:, 2]])
    flip = (face_normal * outward_reference).sum(axis=1) < 0
    return cp.where(flip[:, None], triangles[:, (0, 2, 1)], triangles).astype(cp.int32)


def dual_contour(cp, grid, sdf, voxel_size, world_translation, iso=0.0, method="qef",
                 reduce=1, adaptivity=0.0):
    """Mesh an OnIndex+SDF device grid. Returns (vertices world-space float32
    (V,3), triangles int32 (T,3)). method='qef' => Dual Contouring with QEF vertex
    placement; 'nets' => naive Surface Nets (vertex at the crossing centroid).

    Vertex-count control (reduce/adaptivity; default = none = one vertex per
    surface cell): surface cells are grouped into clusters, each cell's
    QEF is summed into its cluster, one vertex is solved per cluster, and the fine
    connectivity is CONTRACTED onto the cluster vertices (cell -> cluster vertex).
    The contraction keeps the surface CLOSED (0 boundary edges) on both manifold and
    non-manifold inputs: it drops only degenerate (collapsed) triangles -- never
    deduplicates coincident ones (deduping tears holes at folds/pinches) -- and prunes
    vertices left unreferenced (otherwise they show as free points). Where the *full*
    mesh is already non-manifold (vanilla DC leaves non-manifold edges on thin/complex
    shapes like a neuron's axon), contracting around those loci leaves coincident faces
    / non-manifold edges -- benign for viewing (they render solid, no see-through),
    reported by mesh_stats -- but NOT holes or free points. True manifoldness needs a
    Manifold-DC pass. Higher --adaptivity is more aggressive (matches OpenVDB's).

    Memory: the (N,~14) neighbour-index table is the dominant array, so corner
    classification is a running reduction, connectivity gathers only the bipolar
    edges, the table is freed before the per-cell/cluster QEF, and the QEF is
    chunked over surface cells (scatter-added straight into per-cluster
    accumulators), keeping peak bounded regardless of mesh size."""
    if method not in ("qef", "nets"):
        raise SystemExit(f"unknown method {method!r} (qef|nets)")
    decimate = (int(reduce) != 1) or (adaptivity > 0.0)
    memory_pool = cp.get_default_memory_pool()
    active_voxel_count = int(
        nanovdb.tools.cuda.buildVoxelBlockManager(grid, log2_block_width=9).lastOffset())
    value_count = active_voxel_count + 1             # value index 0 is the background slot
    sdf = sdf.astype(cp.float32)
    if sdf.shape[0] < value_count:
        raise SystemExit(f"SDF channel len {sdf.shape[0]} < valueCount {value_count}")

    # (1) ONE int32 box-stencil gather, of only the ~14 columns the mesher uses:
    # neighbor_index[k, column] = value index of voxel k's neighbour at spoke
    # USED_SPOKES[column] (0 = inactive / background).
    if value_count > (1 << 31) - 1:
        raise SystemExit(f"valueCount {value_count} exceeds int32 range; widen the gather to int64.")
    value_index = cp.arange(value_count, dtype=cp.int32)
    neighbor_index = cp.empty((value_count, len(USED_SPOKES)), dtype=cp.int32)
    nanovdb.tools.cuda.gatherBoxStencilColumns(grid, value_index, neighbor_index, USED_SPOKES_ARRAY)
    del value_index

    voxel_coord = cp.empty((value_count, 3), dtype=cp.int32)
    nanovdb.tools.cuda.activeVoxelCoords(grid, voxel_coord)

    # (2) per-voxel central-difference SDF gradient (one-sided where a face is
    # inactive). It is normalised downstream, so the voxel-unit scale is fine.
    def face_value(column):
        neighbor = neighbor_index[:, column]
        return cp.where(neighbor > 0, sdf[neighbor], sdf)        # inactive face -> centre value
    gradient = cp.empty((value_count, 3), dtype=cp.float32)
    gradient[:, 0] = 0.5 * (face_value(FACE_COLUMNS[1]) - face_value(FACE_COLUMNS[0]))
    gradient[:, 1] = 0.5 * (face_value(FACE_COLUMNS[3]) - face_value(FACE_COLUMNS[2]))
    gradient[:, 2] = 0.5 * (face_value(FACE_COLUMNS[5]) - face_value(FACE_COLUMNS[4]))
    gradient[0] = 0.0

    # (3) classify surface cells as a RUNNING reduction over the 8 corners (avoids
    # a dense (N,8) array): a cell is a surface cell when its corner signs straddle
    # iso (0 < inside count < 8) and all 8 corners are active.
    num_inside_corners = cp.zeros(value_count, dtype=cp.int16)
    all_corners_active = cp.ones(value_count, dtype=cp.bool_)
    for corner, column in enumerate(CORNER_COLUMNS):
        corner_value_index = neighbor_index[:, column]
        num_inside_corners += sdf[corner_value_index] < iso
        if corner:                                   # corner 0 is the voxel itself (always active)
            all_corners_active &= corner_value_index > 0
    is_surface_cell = all_corners_active & (num_inside_corners > 0) & (num_inside_corners < 8)
    is_surface_cell[0] = False
    surface_cell = cp.nonzero(is_surface_cell)[0]    # (m,) value indices of surface cells
    num_cells = int(surface_cell.shape[0])
    del num_inside_corners, all_corners_active, is_surface_cell
    if num_cells == 0:
        raise SystemExit("no surface cells found (check isovalue / narrow band).")

    # Keep only the (m, *) subset each surface cell needs for vertex placement.
    cell_corner_index = cp.stack([neighbor_index[surface_cell, column]
                                  for column in CORNER_COLUMNS], axis=1)    # (m,8)
    cell_corner_sdf = sdf[cell_corner_index]                                # (m,8)
    cell_origin = voxel_coord[surface_cell].astype(cp.float32)              # (m,3) cell min-corner
    del voxel_coord

    edge_a, edge_b = cp.asarray(CUBE_EDGES[:, 0]), cp.asarray(CUBE_EDGES[:, 1])
    corner_a_offset = cp.asarray(CORNER_OFFSETS, dtype=cp.float32)[edge_a]          # (12,3)
    edge_vector = (cp.asarray(CORNER_OFFSETS, dtype=cp.float32)[edge_b] - corner_a_offset)[None]
    qef_regularizer = cp.float32(0.05) * cp.eye(3, dtype=cp.float32)[None]
    CHUNK = 8_000_000

    if not decimate:
        # ===== DEFAULT PATH: one vertex per surface cell =====
        cell_vertex = cp.full(value_count, -1, dtype=cp.int32)
        cell_vertex[surface_cell] = cp.arange(num_cells, dtype=cp.int32)
        triangles = _build_triangles(cp, neighbor_index, sdf, cell_vertex, iso, value_count)
        del cell_vertex, neighbor_index, sdf
        memory_pool.free_all_blocks()                # free the big (N,~14) table before the QEF

        local_vertex = cp.empty((num_cells, 3), dtype=cp.float32)   # vertex in cell [0,1]^3 frame
        for start in range(0, num_cells, CHUNK):
            stop = min(start + CHUNK, num_cells)
            edge_normal, edge_is_bipolar, bipolar_mask, crossing_local = _cell_geometry(
                cp, cell_corner_sdf[start:stop], cell_corner_index[start:stop], gradient,
                edge_a, edge_b, corner_a_offset, edge_vector, iso)
            crossing_centroid = ((crossing_local * bipolar_mask).sum(axis=1)
                                 / edge_is_bipolar.sum(axis=1, keepdims=True).astype(cp.float32))
            if method == "nets":
                local_vertex[start:stop] = crossing_centroid
                continue
            # QEF in centroid-relative coords: minimise sum_e (n_e . y - n_e . (p_e - centroid))^2.
            offset_dot_normal = ((crossing_local - crossing_centroid[:, None]) * edge_normal).sum(axis=2)
            normal_matrix = cp.einsum('cei,cej->cij', edge_normal, edge_normal) + qef_regularizer
            rhs = cp.einsum('cei,ce->ci', edge_normal, offset_dot_normal)
            local_vertex[start:stop] = cp.clip(
                crossing_centroid + cp.linalg.solve(normal_matrix, rhs[..., None])[..., 0], 0.0, 1.0)
        vertices = ((cell_origin + local_vertex) * voxel_size
                    + cp.asarray(world_translation, dtype=cp.float32)).astype(cp.float32)
        del cell_corner_sdf, cell_corner_index, cell_origin, local_vertex
        triangles = _orient_triangles(cp, vertices, triangles, gradient[surface_cell])
        return _prune_unreferenced(cp, vertices, triangles)

    # ===== CLUSTERING PATH: one vertex per cluster of surface cells =====
    import cupyx
    cell_normal = None
    if adaptivity > 0.0:                             # per-cell normal for the flatness test
        cell_normal = cp.empty((num_cells, 3), dtype=cp.float32)
        for start in range(0, num_cells, CHUNK):
            stop = min(start + CHUNK, num_cells)
            edge_normal, _bip, _mask, _cross = _cell_geometry(
                cp, cell_corner_sdf[start:stop], cell_corner_index[start:stop], gradient,
                edge_a, edge_b, corner_a_offset, edge_vector, iso)
            summed_normal = edge_normal.sum(axis=1)                          # (c,3)
            cell_normal[start:stop] = (summed_normal
                                       / (cp.linalg.norm(summed_normal, axis=1, keepdims=True) + 1e-12))
    cluster_id, num_clusters, block_size = _assign_clusters(
        cp, cell_origin, cell_normal, reduce, adaptivity, num_cells)

    # Connectivity CONTRACTED onto clusters (cell -> cluster vertex), then cleaned.
    cell_vertex = cp.full(value_count, -1, dtype=cp.int32)
    cell_vertex[surface_cell] = cluster_id
    triangles = _build_triangles(cp, neighbor_index, sdf, cell_vertex, iso, value_count)
    del cell_vertex, neighbor_index, sdf
    memory_pool.free_all_blocks()
    triangles = _clean_triangles(cp, triangles)
    if triangles.shape[0] == 0:
        raise SystemExit("decimation collapsed the mesh; lower --reduce / --adaptivity.")

    # Per-cluster QEF via scatter-add, in an origin-shifted index frame (small
    # magnitudes), accumulated in float64; one vertex solved per cluster.
    coord_origin = cell_origin.min(axis=0).astype(cp.float64)
    normal_matrix = cp.zeros((num_clusters, 3, 3), dtype=cp.float64)         # sum of n_e n_e^T
    rhs = cp.zeros((num_clusters, 3), dtype=cp.float64)                      # sum of n_e (n_e . p_e)
    crossing_sum = cp.zeros((num_clusters, 3), dtype=cp.float64)             # sum of crossings
    crossing_count = cp.zeros(num_clusters, dtype=cp.float64)
    normal_sum = cp.zeros((num_clusters, 3), dtype=cp.float64)               # outward reference
    for start in range(0, num_cells, CHUNK):
        stop = min(start + CHUNK, num_cells)
        cluster = cluster_id[start:stop]
        edge_normal, edge_is_bipolar, bipolar_mask, crossing_local = _cell_geometry(
            cp, cell_corner_sdf[start:stop], cell_corner_index[start:stop], gradient,
            edge_a, edge_b, corner_a_offset, edge_vector, iso)
        crossing_index = ((cell_origin[start:stop].astype(cp.float64) - coord_origin)[:, None, :]
                          + crossing_local.astype(cp.float64))              # (c,12,3) shifted index coords
        normal64 = edge_normal.astype(cp.float64)
        normal_dot_point = (normal64 * crossing_index).sum(axis=2)          # (c,12)
        cupyx.scatter_add(normal_matrix, cluster, cp.einsum('cei,cej->cij', normal64, normal64))
        cupyx.scatter_add(rhs, cluster, cp.einsum('cei,ce->ci', normal64, normal_dot_point))
        cupyx.scatter_add(crossing_sum, cluster, (crossing_index * bipolar_mask.astype(cp.float64)).sum(axis=1))
        cupyx.scatter_add(crossing_count, cluster, edge_is_bipolar.sum(axis=1).astype(cp.float64))
        cupyx.scatter_add(normal_sum, cluster, normal64.sum(axis=1))
    cluster_centroid = crossing_sum / cp.maximum(crossing_count, 1.0)[:, None]   # (num_clusters,3)
    if method == "nets":
        vertex_local = cluster_centroid
    else:
        regularized_matrix = normal_matrix + 0.05 * cp.eye(3, dtype=cp.float64)[None]
        regularized_rhs = rhs + 0.05 * cluster_centroid                     # pull toward the centroid
        vertex_local = cp.linalg.solve(regularized_matrix, regularized_rhs[..., None])[..., 0]
        vertex_local = cp.clip(vertex_local, cluster_centroid - block_size, cluster_centroid + block_size)
    vertices = ((vertex_local + coord_origin) * voxel_size
                + cp.asarray(world_translation, dtype=cp.float64)).astype(cp.float32)
    vertex_normal = (normal_sum / (cp.linalg.norm(normal_sum, axis=1, keepdims=True) + 1e-12)).astype(cp.float32)
    triangles = _orient_triangles(cp, vertices, triangles, vertex_normal)
    return _prune_unreferenced(cp, vertices, triangles)


# ---- minimal GPU rasterizer (so the result can be eyeballed, no deps) -----
_RASTER_SRC = r"""
extern "C" __global__ void raster(
    const float* sx, const float* sy, const float* sz,   // screen-space verts (W,H px ; z view)
    const int* tri,                                       // (T*3) vertex indices
    unsigned long long* zbuf,                             // (W*H) packed depth<<32 | faceid
    int T, int W, int H)
{
    int f = blockIdx.x * blockDim.x + threadIdx.x;
    if (f >= T) return;
    int a = tri[3*f], b = tri[3*f+1], c = tri[3*f+2];
    float ax=sx[a],ay=sy[a],bx=sx[b],by=sy[b],cx=sx[c],cy=sy[c];
    float area = (bx-ax)*(cy-ay) - (by-ay)*(cx-ax);
    if (fabsf(area) < 1e-9f) return;
    float inv = 1.0f/area;
    int xmin = max(0, (int)floorf(fminf(ax,fminf(bx,cx))));
    int xmax = min(W-1,(int)ceilf (fmaxf(ax,fmaxf(bx,cx))));
    int ymin = max(0, (int)floorf(fminf(ay,fminf(by,cy))));
    int ymax = min(H-1,(int)ceilf (fmaxf(ay,fmaxf(by,cy))));
    for (int py=ymin; py<=ymax; ++py){
      for (int px=xmin; px<=xmax; ++px){
        float fx=px+0.5f, fy=py+0.5f;
        float w0=((bx-fx)*(cy-fy)-(by-fy)*(cx-fx))*inv;
        float w1=((cx-fx)*(ay-fy)-(cy-fy)*(ax-fx))*inv;
        float w2=1.0f-w0-w1;
        if (w0<0||w1<0||w2<0) continue;
        float z=w0*sz[a]+w1*sz[b]+w2*sz[c];           // view depth in [0,1], 0=near
        unsigned int zi=(unsigned int)(z*4294967295.0f);
        unsigned long long key=((unsigned long long)zi<<32)|(unsigned int)f;
        atomicMin(&zbuf[py*W+px], key);
      }
    }
}
"""


def render_png(cp, vertices, triangles, path, width=1100, height=1100, yaw=0.6, pitch=0.5):
    """Headlight-shaded orthographic render of (vertices, triangles) -> PNG, via a
    CuPy z-buffer kernel + stdlib zlib (no PIL / matplotlib)."""
    import math, struct, zlib
    vertices = vertices.astype(cp.float32)
    cos_yaw, sin_yaw, cos_pitch, sin_pitch = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    rot_yaw = np.array([[cos_yaw, 0, sin_yaw], [0, 1, 0], [-sin_yaw, 0, cos_yaw]], dtype=np.float32)
    rot_pitch = np.array([[1, 0, 0], [0, cos_pitch, -sin_pitch], [0, sin_pitch, cos_pitch]], dtype=np.float32)
    rotation = cp.asarray(rot_pitch @ rot_yaw)
    projected = (vertices - vertices.mean(0)) @ rotation.T          # centred, rotated
    lo, hi = projected.min(0), projected.max(0)
    span = float((hi - lo)[:2].max()) * 1.08
    screen_x = (projected[:, 0] - lo[0]) / span * width + (width - (hi[0] - lo[0]) / span * width) * 0.5
    screen_y = height - ((projected[:, 1] - lo[1]) / span * height
                         + (height - (hi[1] - lo[1]) / span * height) * 0.5)
    depth = (projected[:, 2] - lo[2]) / (float((hi - lo)[2]) + 1e-9)  # 0 near .. 1 far
    depth = 1.0 - depth                                              # smaller = nearer for atomicMin

    num_tris = int(triangles.shape[0])
    triangle_flat = cp.ascontiguousarray(triangles.astype(cp.int32)).ravel()
    zbuffer = cp.full(width * height, (1 << 64) - 1, dtype=cp.uint64)
    kernel = cp.RawKernel(_RASTER_SRC, "raster")
    kernel(((num_tris + 255) // 256,), (256,),
           (cp.ascontiguousarray(screen_x), cp.ascontiguousarray(screen_y), cp.ascontiguousarray(depth),
            triangle_flat, zbuffer, num_tris, width, height))

    face_id = (zbuffer & 0xFFFFFFFF).astype(cp.int64)
    hit = zbuffer != (1 << 64) - 1
    face_id = cp.where(hit, face_id, 0)
    # face normals in view space -> two-sided Lambertian headlight + ambient.
    v0, v1, v2 = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    face_normal = cp.cross(projected[v1] - projected[v0], projected[v2] - projected[v0])
    face_normal = face_normal / (cp.linalg.norm(face_normal, axis=1, keepdims=True) + 1e-12)
    shade = 0.25 + 0.75 * cp.abs(face_normal[:, 2])                  # light along the view z axis
    image = cp.where(hit, shade[face_id], 0.0).reshape(height, width)
    rgb = cp.stack([image * 0.55, image * 0.78, image], axis=2)     # cool tint; black background
    rgb = (cp.clip(rgb, 0, 1) * 255).astype(cp.uint8)
    rows = cp.asnumpy(rgb)

    raw = b"".join(b"\x00" + rows[y].tobytes() for y in range(height))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    open(path, "wb").write(png)


# ---- mesh output + validation --------------------------------------------
# NB: writing/validation, not the meshing, dominates wall time on big meshes --
# ASCII OBJ via np.savetxt and np.unique(edges, axis=0) are single-CPU-core and
# take *minutes* at tens of millions of triangles while the GPU sits idle. Hence
# the fast binary-PLY writer and the GPU-side topology check below.
def write_obj(path, vertices, triangles):
    """ASCII Wavefront OBJ -- slow (np.savetxt, one CPU core) and large (~2.5 GB
    at 65M tris). Fine for small meshes; prefer .ply for anything large."""
    verts = np.asarray(vertices)
    faces = np.asarray(triangles) + 1                # OBJ vertex indices are 1-based
    with open(path, "w") as file:
        file.write(f"# DC+QEF NanoVDB mesh: {verts.shape[0]} verts, {faces.shape[0]} tris\n")
        np.savetxt(file, verts, fmt="v %.6g %.6g %.6g")
        np.savetxt(file, faces, fmt="f %d %d %d")


def write_ply(path, vertices, triangles):
    """Binary little-endian PLY -- a near-instant tobytes() dump (no per-row text
    formatting) and ~3x smaller than ASCII OBJ. The fast path for big meshes."""
    verts = np.ascontiguousarray(np.asarray(vertices), dtype="<f4")
    faces = np.asarray(triangles)
    header = (f"ply\nformat binary_little_endian 1.0\n"
              f"element vertex {verts.shape[0]}\n"
              f"property float x\nproperty float y\nproperty float z\n"
              f"element face {faces.shape[0]}\n"
              f"property list uchar int vertex_indices\nend_header\n").encode()
    face_records = np.empty(faces.shape[0], dtype=[("count", "u1"), ("indices", "<i4", 3)])
    face_records["count"] = 3
    face_records["indices"] = faces
    with open(path, "wb") as file:
        file.write(header)
        file.write(verts.tobytes())
        file.write(face_records.tobytes())


def write_mesh(path, vertices, triangles):
    """Dispatch on extension: .ply -> fast binary PLY; anything else -> ASCII OBJ."""
    (write_ply if path.lower().endswith(".ply") else write_obj)(path, vertices, triangles)


def mesh_stats(cp, vertices, triangles):
    """Topology check ON THE GPU: encode each sorted edge as one int64 key and
    cp.unique it (a GPU sort, ~seconds) instead of np.unique(edges, axis=0) on a
    single CPU core (minutes at tens of millions of tris). Returns
    (num_verts, num_edges, num_tris, euler_characteristic, boundary_edges,
    nonmanifold_edges)."""
    num_verts, num_tris = int(vertices.shape[0]), int(triangles.shape[0])
    v0, v1, v2 = (triangles[:, i].astype(cp.int64) for i in range(3))

    def edge_key(i, j):                              # one int64 per undirected edge
        return cp.minimum(i, j) * cp.int64(num_verts) + cp.maximum(i, j)   # num_verts < 2^31 -> exact
    _unique_edges, incidence = cp.unique(
        cp.concatenate([edge_key(v0, v1), edge_key(v1, v2), edge_key(v2, v0)]), return_counts=True)
    num_edges = int(incidence.shape[0])
    euler = num_verts - num_edges + num_tris
    return (num_verts, num_edges, num_tris, euler,
            int((incidence == 1).sum()), int((incidence > 2).sum()))


def main(argv):
    parser = argparse.ArgumentParser(description="GPU DC+QEF SDF mesher (prototype)")
    parser.add_argument("paths", nargs="+",
                        help="in.nvdb out.{obj,ply}  (or just out.{obj,ply} with --shape); "
                             ".ply = fast binary, recommended for large meshes")
    parser.add_argument("--shape", choices=["sphere", "csg"], default=None,
                        help="generate a synthetic SDF instead of reading an input grid")
    parser.add_argument("--iso", type=float, default=0.0)
    parser.add_argument("--method", choices=["qef", "nets"], default="qef")
    parser.add_argument("--png", default=None, help="also write a shaded PNG render")
    parser.add_argument("--reduce", type=int, default=1,
                        help="uniform F x F x F cluster-collapse decimation (1 = full detail; "
                             "~F^2 fewer vertices)")
    parser.add_argument("--adaptivity", type=float, default=0.0,
                        help="curvature-adaptive decimation in [0, 1.5]: 0 = off (uniform); >0 "
                             "collapses flat blocks while keeping full detail at features; 1.5 = "
                             "collapse everything (== uniform --reduce). Higher = more aggressive "
                             "(lower flatness bar), matching OpenVDB's adaptivity")
    args = parser.parse_args(argv[1:])
    if not 0.0 <= args.adaptivity <= 1.5:            # beyond 1.5 the flatness bar saturates
        clamped = min(max(args.adaptivity, 0.0), 1.5)
        print(f"note: --adaptivity {args.adaptivity:g} clamped to {clamped:g} "
              f"(valid range 0..1.5)", file=sys.stderr)
        args.adaptivity = clamped
    if args.shape:
        if len(args.paths) != 1:
            raise SystemExit("with --shape, pass exactly one path: the output mesh")
        input_path, output_path = None, args.paths[0]
    else:
        if len(args.paths) != 2:
            raise SystemExit("pass two paths: input.nvdb output.{obj,ply}")
        input_path, output_path = args.paths

    if not (nanovdb.isCudaAvailable() and nanovdb.isGpuAvailable()):
        raise SystemExit("needs a CUDA build of nanovdb and a GPU.")
    import cupy as cp

    read_start = time.time()
    if args.shape == "sphere":
        device_handle, grid, sdf, voxel_size, world_translation = sphere_to_device(cp)
        label = "sphere r=20"
    elif args.shape == "csg":
        device_handle, grid, sdf, voxel_size, world_translation = csg_to_device(cp)
        label = "box n sphere"
    else:
        device_handle, grid, sdf, voxel_size, world_translation = read_sdf_to_device(cp, input_path)
        label = input_path
    read_seconds = time.time() - read_start

    mesh_start = time.time()
    vertices, triangles = dual_contour(cp, grid, sdf, voxel_size, world_translation,
                                       iso=args.iso, method=args.method,
                                       reduce=args.reduce, adaptivity=args.adaptivity)
    cp.cuda.runtime.deviceSynchronize()
    mesh_seconds = time.time() - mesh_start

    (num_verts, num_edges, num_tris, euler,
     boundary_edges, nonmanifold_edges) = mesh_stats(cp, vertices, triangles)   # on GPU, before D2H
    write_mesh(output_path, cp.asnumpy(vertices), cp.asnumpy(triangles))
    decimation_note = "" if (args.reduce == 1 and args.adaptivity == 0) \
        else f" [decimated: reduce={args.reduce} adaptivity={args.adaptivity}]"
    print(f"[{args.method}] {label}: read {read_seconds:.2f}s, mesh {mesh_seconds:.3f}s{decimation_note}")
    print(f"  verts={num_verts}  edges={num_edges}  tris={num_tris}  Euler chi=V-E+F={euler} "
          f"(closed genus-0 => 2); boundary edges={boundary_edges}, non-manifold={nonmanifold_edges}")
    print(f"  wrote {output_path}")
    if args.png:
        render_png(cp, vertices, triangles, args.png)
        print(f"  wrote {args.png}")


if __name__ == "__main__":
    main(sys.argv)
