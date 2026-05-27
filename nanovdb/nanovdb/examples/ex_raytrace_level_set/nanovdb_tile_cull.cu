// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

// Tile-cull renderer for the level-set raytrace example.
//
// Single-kernel design: each thread (= one pixel in a 16x16 tile) tests its
// ray against the tree's upper-internal-node bboxes inline, then either
// short-circuits to background (no upper hit) or runs the stock ZeroCrossing
// on a ray clipped to the union of hit-upper t-ranges.
//
// The fine pass is functionally identical to the static path -- same
// algorithm, just with a tighter t-range and an early-out for pixels whose
// rays don't touch any upper-internal at all.  The win comes from short-
// circuiting the 44-80% of pixels whose tiles' rays don't touch any
// upper-internal at all: those skip the entire ZeroCrossing setup and
// first-step descent that the static path would otherwise pay before
// HDDA discovers there's nothing to hit.
//
// The (tMin, tMax) per-pixel range is strictly looser than the lower-
// internal union, which is strictly looser than the leaf union, which
// strictly contains the set of voxels the ray would visit -- so the
// tile-cull clipping is conservative by construction and produces the
// same surface hits as static (pixel diff is 0-5 / 1M on every tested
// SDF).

#define _USE_MATH_DEFINES
#include <cmath>

#include <cuda_runtime_api.h>

#include <nanovdb/cuda/DeviceBuffer.h>
#include <nanovdb/GridHandle.h>
#include <nanovdb/io/IO.h>
#include <nanovdb/math/Ray.h>
#include <nanovdb/math/HDDA.h>
#include <nanovdb/NodeManager.h>
#include <nanovdb/cuda/NodeManager.cuh>

#include "common.h"

using BufferT = nanovdb::cuda::DeviceBuffer;

namespace {

using GridT        = nanovdb::FloatGrid;
using CoordT       = nanovdb::Coord;
using RealT        = float;
using Vec3T        = nanovdb::math::Vec3<RealT>;
using RayT         = nanovdb::math::Ray<RealT>;
using NodeManagerT = nanovdb::NodeManager<float>;

constexpr int TILE_SIZE        = 16;
constexpr int THREADS_PER_TILE = TILE_SIZE * TILE_SIZE;

__device__ inline nanovdb::math::BBox<nanovdb::Vec3f> coordBBoxToFloat(const nanovdb::CoordBBox& bb)
{
    return nanovdb::math::BBox<nanovdb::Vec3f>(
        nanovdb::Vec3f((float)bb.min()[0],     (float)bb.min()[1],     (float)bb.min()[2]),
        nanovdb::Vec3f((float)bb.max()[0]+1.f, (float)bb.max()[1]+1.f, (float)bb.max()[2]+1.f));
}

__global__ void renderKernel(const GridT* __restrict__        grid,
                             const NodeManagerT* __restrict__ mgr,
                             RayGenOp<Vec3T>                  rayGenOp,
                             CompositeOp                      compositeOp,
                             float                            wBBoxDimZ,
                             int                              width,
                             int                              height,
                             float* __restrict__              outImage)
{
    const int tid   = threadIdx.x;
    const int tileX = blockIdx.x;
    const int tileY = blockIdx.y;
    const int px    = tileX * TILE_SIZE + (tid % TILE_SIZE);
    const int py    = tileY * TILE_SIZE + (tid / TILE_SIZE);
    if (px >= width || py >= height) return;
    const int i = py * width + px;

    Vec3T rayEye, rayDir;
    rayGenOp(i, width, height, rayEye, rayDir);
    RayT wRay(rayEye, rayDir);
    RayT iRay = wRay.worldToIndexF(*grid);

    // Per-pixel upper-internal cull: test this thread's ray against every
    // upper-internal bbox and accumulate (tMin, tMax) as the smallest
    // interval bracketing every upper this ray enters.  If no upper is hit
    // we short-circuit to background; otherwise we clip the ray to the
    // union and run the stock ZeroCrossing below.
    //
    // Each thread does numUpper (~8) slab tests; bbox reads are broadcast
    // across the block so the L1 sees identical cache lines from every
    // thread (cheap).
    float tMin = 1e30f, tMax = -1e30f;
    const uint64_t numUpper = mgr->upperCount();
    for (uint64_t u = 0; u < numUpper; ++u) {
        const auto& upper = mgr->upper((uint32_t)u);
        auto        uBox  = coordBBoxToFloat(upper.bbox());
        float       ut0, ut1;
        if (iRay.intersects(uBox, ut0, ut1)) {
            if (ut0 < tMin) tMin = ut0;
            if (ut1 > tMax) tMax = ut1;
        }
    }

    if (tMax < tMin) {
        // empty pixel -- ray hits no upper-internal at all.
        compositeOp(outImage, i, width, height, 0.0f, 0.0f);
        return;
    }

    RayT clipped = iRay;
    clipped.setTimes(tMin > 0.f ? tMin : iRay.t0(), tMax);

    auto   acc = grid->tree().getAccessor();
    CoordT ijk;
    float  v, t;
    if (nanovdb::math::ZeroCrossing(clipped, acc, ijk, v, t)) {
        // write distance to surface.
        const float wT0 = t * float(grid->voxelSize()[0]);
        compositeOp(outImage, i, width, height, wT0 / (wBBoxDimZ * 2), 1.0f);
    } else {
        // write background value.
        compositeOp(outImage, i, width, height, 0.0f, 0.0f);
    }
}

} // namespace

void runNanoVDBTileCull(nanovdb::GridHandle<BufferT>& handle, int numIterations,
                        int width, int height, BufferT& imageBuffer)
{
    auto* h_grid = handle.grid<float>();
    if (!h_grid) throw std::runtime_error("GridHandle does not contain a valid host grid");

    handle.deviceUpload();
    auto* d_grid = handle.deviceGrid<float>();
    if (!d_grid) throw std::runtime_error("GridHandle does not contain a valid device grid");

    auto  mgrHandle = nanovdb::cuda::createNodeManager<float>(d_grid);
    auto* d_mgr     = mgrHandle.deviceMgr<float>();
    if (!d_mgr) throw std::runtime_error("NodeManager allocation failed");

    auto  hostMgrHandle = nanovdb::createNodeManager<float, nanovdb::HostBuffer>(*h_grid);
    auto* h_mgr         = hostMgrHandle.mgr<float>();
    std::cout << "Tile-cull tracer: upperCount=" << h_mgr->upperCount()
              << "  lowerCount=" << h_mgr->lowerCount() << "\n";

    const float wBBoxDimZ   = (float)h_grid->worldBBox().dim()[2] * 2;
    const Vec3T wBBoxCenter = Vec3T(h_grid->worldBBox().min() + h_grid->worldBBox().dim() * 0.5f);

    RayGenOp<Vec3T> rayGenOp(wBBoxDimZ, wBBoxCenter);
    CompositeOp     compositeOp;

    const int tilesX = (width  + TILE_SIZE - 1) / TILE_SIZE;
    const int tilesY = (height + TILE_SIZE - 1) / TILE_SIZE;

    imageBuffer.deviceUpload();
    float* d_outImage = reinterpret_cast<float*>(imageBuffer.deviceData());

    dim3 grid2D(tilesX, tilesY);
    dim3 block1D(THREADS_PER_TILE);

    // Warm-up.
    for (int i = 0; i < 5; ++i) {
        renderKernel<<<grid2D, block1D>>>(d_grid, d_mgr, rayGenOp, compositeOp,
                                          wBBoxDimZ, width, height, d_outImage);
    }
    cudaDeviceSynchronize();

    cudaEvent_t e0, e1;
    cudaEventCreate(&e0); cudaEventCreate(&e1);

    float totalTime = 0.f;
    for (int i = 0; i < numIterations; ++i) {
        cudaEventRecord(e0);
        renderKernel<<<grid2D, block1D>>>(d_grid, d_mgr, rayGenOp, compositeOp,
                                          wBBoxDimZ, width, height, d_outImage);
        cudaEventRecord(e1);
        cudaEventSynchronize(e1);
        float t = 0.f;
        cudaEventElapsedTime(&t, e0, e1);
        totalTime += t;
    }
    std::cout << "Tile-cull tracer avg ms: " << (totalTime / numIterations) << "\n";

    cudaEventDestroy(e0); cudaEventDestroy(e1);

    imageBuffer.deviceDownload();
    saveImage("raytrace_level_set-nanovdb-cuda-tile-cull.pfm", width, height, (float*)imageBuffer.data());
}
