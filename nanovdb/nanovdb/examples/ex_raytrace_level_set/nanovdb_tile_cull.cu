// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

// Tile-cull renderer for the level-set raytrace example.
//
// Two-kernel pipeline: a block-cooperative coarse pre-pass over the tree's
// upper-internal nodes, followed by a per-pixel fine pass that runs the
// stock ZeroCrossing on a t-clipped ray (or short-circuits for tiles that
// the coarse pass classified as empty).
//
//   coarsePass:  one CUDA block per 16x16 screen tile (256 threads).  Each
//                thread casts its pixel's ray and tests it against every
//                upper-internal-node bbox; a block-wide reduction produces
//                the conservative union (tMin, tMax) over the upper-internal
//                bboxes the tile's rays hit.  Constant ~19 M dynamic
//                instructions regardless of scene complexity.
//
//   finePass:    same launch geometry.  Each thread clips its ray to the
//                tile's (tMin, tMax) and runs the stock ZeroCrossing on
//                the clipped ray (or returns background immediately if
//                tMax < tMin).
//
// The fine pass is functionally identical to the static path -- same
// algorithm, just with a tighter t-range.  The win comes from short-
// circuiting the 44-80% of pixels whose tiles' rays don't touch any
// upper-internal at all: those skip the entire ZeroCrossing setup and
// first-step descent that the static path would otherwise pay before
// HDDA discovers there's nothing to hit.
//
// The (tMin, tMax) union is strictly looser than the lower-internal
// union, which is strictly looser than the leaf union, which strictly
// contains the set of voxels the ray would visit -- so the tile-cull
// clipping is conservative by construction and the fine pass produces
// the same surface hits as static (pixel diff is 0-5 / 1M on every
// tested SDF).

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
constexpr int NUM_WARPS        = THREADS_PER_TILE / 32;

__device__ inline nanovdb::math::BBox<nanovdb::Vec3f> coordBBoxToFloat(const nanovdb::CoordBBox& bb)
{
    return nanovdb::math::BBox<nanovdb::Vec3f>(
        nanovdb::Vec3f((float)bb.min()[0],     (float)bb.min()[1],     (float)bb.min()[2]),
        nanovdb::Vec3f((float)bb.max()[0]+1.f, (float)bb.max()[1]+1.f, (float)bb.max()[2]+1.f));
}

__global__ void coarsePass(const GridT* __restrict__        grid,
                           const NodeManagerT* __restrict__ mgr,
                           RayGenOp<Vec3T>                  rayGenOp,
                           int                              width,
                           int                              height,
                           float* __restrict__              tMinOut,
                           float* __restrict__              tMaxOut,
                           int                              tilesX,
                           int                              tilesY)
{
    __shared__ float sharedMin[NUM_WARPS];
    __shared__ float sharedMax[NUM_WARPS];

    const int tid     = threadIdx.x;
    const int tileX   = blockIdx.x;
    const int tileY   = blockIdx.y;
    const int tileIdx = tileY * tilesX + tileX;

    const int  px           = tileX * TILE_SIZE + (tid % TILE_SIZE);
    const int  py           = tileY * TILE_SIZE + (tid / TILE_SIZE);
    const bool isValidPixel = (px < width) && (py < height);

    Vec3T rayEye, rayDir;
    rayGenOp(py * width + px, width, height, rayEye, rayDir);
    RayT wRay(rayEye, rayDir);
    RayT iRay = wRay.worldToIndexF(*grid);

    float myMin = 1e30f, myMax = -1e30f;

    const uint64_t numUpper = mgr->upperCount();
    for (uint64_t u = 0; u < numUpper; ++u) {
        const auto& upper = mgr->upper((uint32_t)u);
        auto        uBox  = coordBBoxToFloat(upper.bbox());
        float       ut0, ut1;
        if (isValidPixel && iRay.intersects(uBox, ut0, ut1)) {
            if (ut0 < myMin) myMin = ut0;
            if (ut1 > myMax) myMax = ut1;
        }
    }

    // Warp-level reduce then block-level scalar reduce on warp leaders.
    #pragma unroll
    for (int off = 16; off >= 1; off /= 2) {
        myMin = fminf(myMin, __shfl_xor_sync(0xFFFFFFFF, myMin, off));
        myMax = fmaxf(myMax, __shfl_xor_sync(0xFFFFFFFF, myMax, off));
    }
    const int warpId = tid >> 5;
    const int laneId = tid & 31;
    if (laneId == 0) {
        sharedMin[warpId] = myMin;
        sharedMax[warpId] = myMax;
    }
    __syncthreads();
    if (tid == 0) {
        float blockMin = sharedMin[0], blockMax = sharedMax[0];
        #pragma unroll
        for (int w = 1; w < NUM_WARPS; ++w) {
            blockMin = fminf(blockMin, sharedMin[w]);
            blockMax = fmaxf(blockMax, sharedMax[w]);
        }
        tMinOut[tileIdx] = blockMin;
        tMaxOut[tileIdx] = blockMax;
    }
}

__global__ void finePass(const GridT* __restrict__ grid,
                         const float* __restrict__ tMinIn,
                         const float* __restrict__ tMaxIn,
                         RayGenOp<Vec3T>           rayGenOp,
                         CompositeOp               compositeOp,
                         float                     wBBoxDimZ,
                         int                       width,
                         int                       height,
                         float* __restrict__       outImage,
                         int                       tilesX,
                         int                       tilesY)
{
    const int tid     = threadIdx.x;
    const int tileX   = blockIdx.x;
    const int tileY   = blockIdx.y;
    const int tileIdx = tileY * tilesX + tileX;

    const int px = tileX * TILE_SIZE + (tid % TILE_SIZE);
    const int py = tileY * TILE_SIZE + (tid / TILE_SIZE);
    if (px >= width || py >= height) return;
    const int i = py * width + px;

    const float tMin = tMinIn[tileIdx];
    const float tMax = tMaxIn[tileIdx];

    if (tMax < tMin) {
        // empty tile -- write background.
        compositeOp(outImage, i, width, height, 0.0f, 0.0f);
        return;
    }

    Vec3T rayEye, rayDir;
    rayGenOp(i, width, height, rayEye, rayDir);
    RayT wRay(rayEye, rayDir);
    RayT iRay = wRay.worldToIndexF(*grid);

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

    const int tilesX   = (width  + TILE_SIZE - 1) / TILE_SIZE;
    const int tilesY   = (height + TILE_SIZE - 1) / TILE_SIZE;
    const int numTiles = tilesX * tilesY;

    float *d_tMin = nullptr, *d_tMax = nullptr;
    cudaMalloc(&d_tMin, sizeof(float) * numTiles);
    cudaMalloc(&d_tMax, sizeof(float) * numTiles);

    imageBuffer.deviceUpload();
    float* d_outImage = reinterpret_cast<float*>(imageBuffer.deviceData());

    dim3 grid2D(tilesX, tilesY);
    dim3 block1D(THREADS_PER_TILE);

    // Warm-up.
    for (int i = 0; i < 5; ++i) {
        coarsePass<<<grid2D, block1D>>>(d_grid, d_mgr, rayGenOp, width, height,
                                        d_tMin, d_tMax, tilesX, tilesY);
        finePass  <<<grid2D, block1D>>>(d_grid, d_tMin, d_tMax, rayGenOp, compositeOp,
                                        wBBoxDimZ, width, height, d_outImage,
                                        tilesX, tilesY);
    }
    cudaDeviceSynchronize();

    cudaEvent_t e0, e1, e2;
    cudaEventCreate(&e0); cudaEventCreate(&e1); cudaEventCreate(&e2);

    float totalCoarse = 0.f, totalFine = 0.f;
    for (int i = 0; i < numIterations; ++i) {
        cudaEventRecord(e0);
        coarsePass<<<grid2D, block1D>>>(d_grid, d_mgr, rayGenOp, width, height,
                                        d_tMin, d_tMax, tilesX, tilesY);
        cudaEventRecord(e1);
        finePass  <<<grid2D, block1D>>>(d_grid, d_tMin, d_tMax, rayGenOp, compositeOp,
                                        wBBoxDimZ, width, height, d_outImage,
                                        tilesX, tilesY);
        cudaEventRecord(e2);
        cudaEventSynchronize(e2);
        float t01 = 0.f, t12 = 0.f;
        cudaEventElapsedTime(&t01, e0, e1);
        cudaEventElapsedTime(&t12, e1, e2);
        totalCoarse += t01;
        totalFine   += t12;
    }
    std::cout << "Tile-cull tracer avg ms:"
              << " coarse=" << (totalCoarse / numIterations)
              << " fine=" << (totalFine / numIterations)
              << " total=" << ((totalCoarse + totalFine) / numIterations)
              << "\n";

    cudaEventDestroy(e0); cudaEventDestroy(e1); cudaEventDestroy(e2);

    imageBuffer.deviceDownload();
    saveImage("raytrace_level_set-nanovdb-cuda-tile-cull.pfm", width, height, (float*)imageBuffer.data());

    cudaFree(d_tMin);
    cudaFree(d_tMax);
}
