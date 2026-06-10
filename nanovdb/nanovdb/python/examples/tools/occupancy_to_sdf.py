# Copyright Contributors to the OpenVDB Project
# SPDX-License-Identifier: Apache-2.0
"""Occupancy IndexGrid -> narrow-band SDF, on the GPU (preprocessing for dc_qef_mesh.py).

Input: a NanoVDB **OnIndex** grid whose active voxels mark the *occupancy* of a
solid object (active = inside / part-of-object; the object's surface is the
boundary between active and inactive voxels). There is no distance payload --
just topology.

Output: an OnIndex grid carrying a **narrow-band signed distance field** in blind
channel 0 (semantic LevelSet): phi < 0 inside the object, phi > 0 outside, ~0 at
the surface, |phi| <= band * voxel_size. That is exactly the form dc_qef_mesh.py's
OnIndex+SDF path consumes, so the two compose:

    python occupancy_to_sdf.py  occ.nvdb  sdf.nvdb  [--band 3] [--smooth 8] [--coarsen K]
    python dc_qef_mesh.py        sdf.nvdb  mesh.obj  --method qef --png mesh.png

Pipeline (all on device, via the tools.cuda topology + gather ops):
  1. (optional) coarsenGrid x K   -- shrink very large inputs (each pass halves
                                     the resolution).
  2. dilateGrid x band            -- grow an exterior shell so the band has voxels
                                     OUTSIDE the occupancy (inside is already
                                     filled for a solid object).
  3. inject an occupancy mark     -- 1 on the original (inside) voxels, 0 on the
                                     freshly-added exterior -> the sign field.
  4. pin a signed step at the occupancy boundary (+/-0.5 vx at the surface,
     +/-band*vx away) and redistance it to |grad phi| = 1 with a **TVD Runge-Kutta
     Godunov** scheme (--order 1/2/3; 3 = default). Higher order is far less
     diffusive than first-order Euler, so the post-smoothing redistance does not
     re-terrace the surface -- that is what removes the staircase banding. Scalar,
     so memory-light (one (N,6) face gather per RK stage).
  5. (optional) smoothing + redistance -- de-staircase the blocky binary surface so
     it is not visibly voxelized. Two methods (--smooth-method): mean-curvature flow
     (default) erodes the staircase AND any voxel-scale comb baked into the occupancy
     boundary, but shrinks thin features; Taubin lambda|mu is volume-preserving but
     PRESERVES coherent combs/teeth, so it only takes the edge off true noise.
  6. (default) prune to |phi| <= band*vx -- drop the deep interior (useless for
     meshing; this keeps the output small and downstream-meshable).

Built only from gatherBoxStencilColumns + dilateGrid + inject + activeVoxelCoords +
voxelsToOnIndexGrid + addBlindData -- no hand-written CUDA.

Env on this branch:
    PYTHONPATH=/workspace/build_pygpu/py_import \
    /home/vscode/miniforge3/envs/nanovdb/bin/python occupancy_to_sdf.py ...
"""
import argparse
import sys
import time

import numpy as np

import nanovdb


def _codec(name):
    """Map a --codec string to a nanovdb.io.Codec (on-disk compression for the .nvdb)."""
    return {"none": nanovdb.io.Codec.NONE, "zip": nanovdb.io.Codec.ZIP,
            "blosc": nanovdb.io.Codec.BLOSC}[name]


# The 6 face-neighbour box-stencil spokes, in -x,+x,-y,+y,-z,+z order. These are
# the ONLY neighbours this script reads (the redistance, smoothing, and boundary
# steps are all 6-point stencils), so we gather just these columns -- the gathered
# table is (N, 6), not the full (N, 27). After the gather, column j corresponds to
# FACE_SPOKES[j]: 0=-x, 1=+x, 2=-y, 3=+y, 4=-z, 5=+z.
FACE_SPOKES = [4, 22, 10, 16, 12, 14]                # spoke = (di+1)*9+(dj+1)*3+(dk+1)
FACE_SPOKES_ARRAY = np.asarray(FACE_SPOKES, dtype=np.int32)


def _active_voxel_count(grid):
    """Number of active voxels in a device OnIndex grid (via a transient VBM)."""
    return int(nanovdb.tools.cuda.buildVoxelBlockManager(grid, log2_block_width=9).lastOffset())


def _gather_faces(cp, grid, values, value_count):
    """Dense (value_count, 6) gather of just the 6 FACE neighbours of every voxel,
    in -x,+x,-y,+y,-z,+z order: row k, column j holds `values[neighbour]` for that
    voxel's face neighbour at spoke FACE_SPOKES[j] (inactive neighbours read
    values[0], the background slot). Uses gatherBoxStencilColumns, so the table is
    (N, 6) rather than the full (N, 27) -- ~4.5x less GPU memory for the dominant
    array, and these 6 columns are all the redistance / smoothing / boundary
    stencils ever touch."""
    faces = cp.empty((value_count, 6), dtype=cp.float32)
    nanovdb.tools.cuda.gatherBoxStencilColumns(grid, values, faces, FACE_SPOKES_ARRAY)
    return faces


def _redistance(cp, grid, phi, value_count, voxel_size, band_world, iters, order=3):
    """TVD Runge-Kutta Godunov redistancing of `phi` toward |grad phi| = 1.

    order = 1 (Euler), 2 (Heun) or 3 (Shu-Osher TVD-RK3). First-order Euler is
    diffusive and stair-steps the level set, which *re-terraces* a freshly-smoothed
    surface -> visible banding in the mesh; a higher-order TVD-RK step is far less
    diffusive, so it restores |grad phi| = 1 without re-introducing those steps.
    The sign is the Peng et al. smoothed sign  s = phi0 / sqrt(phi0^2 +
    |grad phi0|^2 * vx^2),  frozen from the initial field so the zero level stays
    put. Inactive neighbours read phi[0] = +band_world (the outside BC); phi is
    clamped to the band each step."""
    faces = _gather_faces(cp, grid, phi, value_count)[1:value_count]  # frozen smoothed sign (Peng)
    phi0 = phi[1:value_count]                        # face columns: 0=-x,1=+x,2=-y,3=+y,4=-z,5=+z
    grad_x = (faces[:, 1] - faces[:, 0]) / (2 * voxel_size)
    grad_y = (faces[:, 3] - faces[:, 2]) / (2 * voxel_size)
    grad_z = (faces[:, 5] - faces[:, 4]) / (2 * voxel_size)
    sign = phi0 / cp.sqrt(phi0 * phi0 + (grad_x * grad_x + grad_y * grad_y + grad_z * grad_z) * voxel_size * voxel_size + 1e-12)
    del faces, grad_x, grad_y, grad_z
    time_step = 0.4 * voxel_size

    def godunov_rhs(field):                          # d phi / dt = sign * (1 - |grad phi|), on [1:N]
        faces = _gather_faces(cp, grid, field, value_count)[1:value_count]
        centre = field[1:value_count]
        x_minus, x_plus, y_minus, y_plus, z_minus, z_plus = (faces[:, j] for j in range(6))
        del faces

        def upwind(back, forward):                   # Godunov one-sided gradient-squared
            return cp.where(sign > 0,
                            cp.maximum(cp.maximum(back, 0.0) ** 2, cp.minimum(forward, 0.0) ** 2),
                            cp.maximum(cp.minimum(back, 0.0) ** 2, cp.maximum(forward, 0.0) ** 2))
        grad_magnitude = cp.sqrt(upwind((centre - x_minus) / voxel_size, (x_plus - centre) / voxel_size)
                                 + upwind((centre - y_minus) / voxel_size, (y_plus - centre) / voxel_size)
                                 + upwind((centre - z_minus) / voxel_size, (z_plus - centre) / voxel_size))
        return sign * (1.0 - grad_magnitude)

    stage = phi.copy()                               # scratch full array (stage[0] stays +band_world)
    for _ in range(iters):
        phi_n = phi[1:value_count].copy()
        if order <= 1:                               # forward Euler
            phi[1:value_count] = cp.clip(phi_n + time_step * godunov_rhs(phi), -band_world, band_world)
        elif order == 2:                             # Heun (TVD-RK2)
            rhs_0 = godunov_rhs(phi)
            stage[1:value_count] = cp.clip(phi_n + time_step * rhs_0, -band_world, band_world)
            phi[1:value_count] = cp.clip(phi_n + 0.5 * time_step * (rhs_0 + godunov_rhs(stage)), -band_world, band_world)
        else:                                        # Shu-Osher TVD-RK3
            stage[1:value_count] = cp.clip(phi_n + time_step * godunov_rhs(phi), -band_world, band_world)
            stage[1:value_count] = cp.clip(0.75 * phi_n + 0.25 * (stage[1:value_count] + time_step * godunov_rhs(stage)),
                                           -band_world, band_world)
            phi[1:value_count] = cp.clip((phi_n + 2.0 * (stage[1:value_count] + time_step * godunov_rhs(stage))) / 3.0,
                                         -band_world, band_world)
    return phi


def _smooth_pass(cp, grid, phi, value_count, weight):
    """One umbrella-Laplacian pass: phi += weight * (mean of 6 face neighbours - phi).
    weight > 0 shrinks (diffuses), weight < 0 inflates (anti-diffuses)."""
    faces = _gather_faces(cp, grid, phi, value_count)[1:value_count]
    centre = phi[1:value_count]
    phi[1:value_count] = centre + weight * (faces.mean(axis=1) - centre)
    del faces
    return phi


def _mean_curvature_smooth(cp, grid, phi, value_count, iters):
    """Mean-curvature flow on the band (weight-1 Laplacian, phi <- mean of 6 face
    neighbours each step). Diffusive: it ERODES high-curvature features, so it melts
    the voxel staircase AND any voxel-scale 'comb' baked into the occupancy boundary
    -- at the cost of shrinking genuinely thin features (run more iters for a smoother
    surface, fewer to preserve detail). This is the de-staircase / de-comb workhorse."""
    for _ in range(iters):
        _smooth_pass(cp, grid, phi, value_count, 1.0)
    return phi


def _taubin_smooth(cp, grid, phi, value_count, iters, lam=0.5, mu=-0.53):
    """Taubin lambda|mu smoothing on the band: a shrinking pass (weight `lam` > 0)
    followed by an inflating pass (weight `mu` < 0, |mu| slightly > lam). It is
    approximately VOLUME-PRESERVING, so it de-noises without the shrinkage of
    mean-curvature flow -- but for the same reason it PRESERVES coherent geometry,
    including a voxel-scale comb/teeth already present in the occupancy (verified:
    the comb survives even with a wide band and many iterations). Use it to take the
    edge off true high-frequency noise while keeping features; use mean-curvature
    flow to actually remove a staircase/comb. `iters` = number of lambda/mu pairs."""
    for _ in range(iters):
        _smooth_pass(cp, grid, phi, value_count, lam)
        _smooth_pass(cp, grid, phi, value_count, mu)
    return phi


_SMOOTHERS = {"mean-curvature": _mean_curvature_smooth, "taubin": _taubin_smooth}


def occupancy_to_sdf(cp, device_handle, voxel_size, band=3, smooth=8, coarsen=0,
                     prune=True, order=3, redistance_iters=None,
                     smooth_method="mean-curvature"):
    """Occupancy OnIndex device handle + voxel size -> (DeviceGridHandle with the
    narrow-band SDF in blind channel 0, voxel_size, stats dict, kept-alive handle
    list). See the module docstring for the pipeline."""
    handles = [device_handle]                        # hold handles so device memory survives
    grid = device_handle.deviceGrid(0)
    voxel_size = float(voxel_size)

    for _ in range(coarsen):                         # shrink huge inputs (each pass halves resolution)
        device_handle = nanovdb.tools.cuda.coarsenGrid(grid)
        handles.append(device_handle)
        grid = device_handle.deviceGrid(0)
        voxel_size *= 2.0
    solid_voxel_count = _active_voxel_count(grid)

    # Occupancy on the original (solid) grid: 1 everywhere active, 0 = background.
    occupancy_solid = cp.ones(solid_voxel_count + 1, dtype=cp.float32)
    occupancy_solid[0] = 0.0

    # Dilate the topology outward by `band` layers (BOX connectivity op=26 covers
    # diagonals, so every surface cell's 8 corners end up active -> no mesh holes).
    dilated_handle, dilated_grid = device_handle, grid
    for _ in range(band):
        dilated_handle = nanovdb.tools.cuda.dilateGrid(dilated_grid, op=26)
        handles.append(dilated_handle)
        dilated_grid = dilated_handle.deviceGrid(0)
    band_voxel_count = _active_voxel_count(dilated_grid)
    value_count = band_voxel_count + 1

    # Carry the occupancy mark onto the dilated grid: the original (inside) voxels
    # get 1; the freshly-added exterior shell stays 0.
    occupancy = cp.zeros(value_count, dtype=cp.float32)
    nanovdb.tools.cuda.inject(grid, dilated_grid, occupancy_solid, occupancy)
    del occupancy_solid

    band_world = band * voxel_size                   # band half-width in world units
    sign = cp.where(occupancy[1:value_count] > 0.5, cp.float32(-1.0), cp.float32(1.0))
    # A voxel is on the surface if a face neighbour has the opposite occupancy.
    faces = _gather_faces(cp, dilated_grid, occupancy, value_count)[1:value_count]
    is_boundary = (faces != occupancy[1:value_count][:, None]).any(axis=1)
    del faces
    # Pin a signed step: +/-0.5 vx right at the surface, +/-band away from it.
    phi = cp.empty(value_count, dtype=cp.float32)
    phi[1:value_count] = sign * cp.where(is_boundary, cp.float32(0.5 * voxel_size), cp.float32(band_world))
    phi[0] = band_world                              # background reads as "outside" in the gather BC
    del sign, is_boundary, occupancy

    iters = redistance_iters if redistance_iters is not None else max(6, int(round(2.5 * band)) + 2)
    phi = _redistance(cp, dilated_grid, phi, value_count, voxel_size, band_world, iters, order=order)
    if smooth:
        _SMOOTHERS[smooth_method](cp, dilated_grid, phi, value_count, smooth)
        phi = _redistance(cp, dilated_grid, phi, value_count, voxel_size, band_world, max(4, smooth), order=order)

    stats = {"voxels_in": solid_voxel_count, "voxels_band": band_voxel_count, "vx": voxel_size}
    if prune:
        # Drop the deep interior (the clamped |phi| == band voxels, ~all of a solid)
        # by REBUILDING a grid from just the narrow-band ramp voxels -- a per-voxel
        # rebuild, NOT pruneGrid (which prunes at leaf granularity and would leave
        # gated cells that tear holes). Surface-cell corners (|phi| <~ 1.7) are kept.
        voxel_coord = cp.empty((value_count, 3), dtype=cp.int32)
        nanovdb.tools.cuda.activeVoxelCoords(dilated_grid, voxel_coord)
        in_band = cp.abs(phi) < band_world * 0.999
        in_band[0] = False
        band_coords = cp.ascontiguousarray(voxel_coord[in_band])
        band_handle = nanovdb.tools.cuda.voxelsToOnIndexGrid(band_coords, voxel_size)
        handles.append(band_handle)
        band_grid = band_handle.deviceGrid(0)
        band_count = _active_voxel_count(band_grid)
        band_phi = cp.full(band_count + 1, band_world, dtype=cp.float32)
        nanovdb.tools.cuda.inject(dilated_grid, band_grid, phi, band_phi)
        band_phi[0] = band_world
        dilated_grid, phi = band_grid, band_phi
        stats["voxels_out"] = band_count
    else:
        stats["voxels_out"] = band_voxel_count

    output_handle = nanovdb.tools.cuda.addBlindData(
        dilated_grid, phi, nanovdb.GridBlindDataClass.ChannelArray,
        nanovdb.GridBlindDataSemantic.LevelSet, "sdf")
    handles.append(output_handle)
    return output_handle, voxel_size, stats, handles


def solid_ball_handle(cp, radius=20.0, voxel_size=1.0):
    """A filled-ball occupancy OnIndex grid (synthetic self-test input)."""
    radius_int = int(radius)
    grid_coords = np.mgrid[-radius_int - 1:radius_int + 2,
                           -radius_int - 1:radius_int + 2,
                           -radius_int - 1:radius_int + 2].reshape(3, -1).T
    inside = (grid_coords ** 2).sum(axis=1) <= radius * radius
    coords = cp.asarray(np.ascontiguousarray(grid_coords[inside], dtype=np.int32))
    return nanovdb.tools.cuda.voxelsToOnIndexGrid(coords, voxel_size)


def main(argv):
    parser = argparse.ArgumentParser(description="occupancy IndexGrid -> narrow-band SDF (GPU)")
    parser.add_argument("paths", nargs="+",
                        help="occ.nvdb sdf.nvdb  (or just sdf.nvdb with --solid-ball)")
    parser.add_argument("--band", type=int, default=3, help="narrow-band half-width in voxels")
    parser.add_argument("--smooth", type=int, default=8,
                        help="de-staircase smoothing iterations (default 8; raise for "
                             "combed/voxelized inputs, lower to preserve fine detail)")
    parser.add_argument("--smooth-method", choices=("mean-curvature", "taubin"),
                        default="mean-curvature",
                        help="mean-curvature flow erodes the staircase/comb away (default; "
                             "shrinks thin features); taubin is volume-preserving but PRESERVES "
                             "a comb baked into the occupancy, so it won't remove these artefacts")
    parser.add_argument("--coarsen", type=int, default=0,
                        help="coarsenGrid passes (each pass halves the resolution)")
    parser.add_argument("--order", type=int, default=3, choices=(1, 2, 3),
                        help="redistance TVD-RK order (1 = Euler / banded, 3 = smoothest)")
    parser.add_argument("--no-prune", dest="prune", action="store_false",
                        help="keep the deep interior instead of pruning to the band")
    parser.add_argument("--solid-ball", type=float, default=None,
                        help="use a synthetic filled ball of this radius instead of reading a grid")
    parser.add_argument("--codec", choices=("none", "zip", "blosc"), default="zip",
                        help="on-disk .nvdb compression (default zip)")
    args = parser.parse_args(argv[1:])
    if args.solid_ball is not None:
        if len(args.paths) != 1:
            raise SystemExit("with --solid-ball, pass exactly one path: the output sdf.nvdb")
        input_path, output_path = None, args.paths[0]
    else:
        if len(args.paths) != 2:
            raise SystemExit("pass two paths: occupancy.nvdb output_sdf.nvdb")
        input_path, output_path = args.paths

    if not (nanovdb.isCudaAvailable() and nanovdb.isGpuAvailable()):
        raise SystemExit("needs a CUDA build of nanovdb and a GPU.")
    import cupy as cp

    read_start = time.time()
    if input_path is None:
        voxel_size = 1.0
        device_handle = solid_ball_handle(cp, radius=args.solid_ball, voxel_size=voxel_size)
        label = f"solid ball r={args.solid_ball:g}"
    else:
        host_handle = nanovdb.io.readGrid(input_path)
        if host_handle.gridType(0) != nanovdb.GridType.OnIndex:
            raise SystemExit(f"{input_path}: expected an OnIndex occupancy grid, got {host_handle.gridType(0)}.")
        voxel_size = float(host_handle.grid(0).voxelSize()[0])
        device_handle = nanovdb.io.deviceReadGrid(input_path)
        device_handle.deviceUpload(0, True)
        label = input_path
    read_seconds = time.time() - read_start

    process_start = time.time()
    output_handle, voxel_size, stats, _handles = occupancy_to_sdf(
        cp, device_handle, voxel_size, band=args.band, smooth=args.smooth,
        coarsen=args.coarsen, prune=args.prune, order=args.order,
        smooth_method=args.smooth_method)
    cp.cuda.runtime.deviceSynchronize()
    process_seconds = time.time() - process_start

    output_handle.deviceDownload(0, True)
    # deviceWriteGrid (not the raw, uncompressed handle.write) so --codec applies.
    nanovdb.io.deviceWriteGrid(output_path, output_handle, _codec(args.codec))
    print(f"{label}: read {read_seconds:.2f}s, sdf {process_seconds:.2f}s  "
          f"(voxelSize {voxel_size:g}, TVD-RK{args.order})")
    print(f"  occupancy in: {stats['voxels_in']:,}  -> band+shell: {stats['voxels_band']:,} "
          f"-> output: {stats['voxels_out']:,} active voxels")
    print(f"  wrote {output_path}  (OnIndex + LevelSet blind channel; feed to dc_qef_mesh.py)")


if __name__ == "__main__":
    main(sys.argv)
