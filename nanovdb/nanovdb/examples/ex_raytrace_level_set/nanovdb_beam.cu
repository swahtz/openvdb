// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

// Beam-tracing renderer (milestone 2): two-kernel pipeline.
//
//   coarsePass:  one CUDA block per 16x16 screen tile (256 threads).  The
//                block walks the tree hierarchically -- per-upper-internal
//                first (block-wide skip if no thread hits), then per-lower
//                child of the surviving uppers.  Each thread accumulates
//                its own [tMin, tMax] over all lowers its ray hits; a
//                block-wide reduction at the end produces the tile's
//                conservative union t-range.
//
//   finePass:    same launch geometry.  Each thread reads its tile's
//                (tMin, tMax), clips its ray to that range, and runs the
//                stock ZeroCrossing.  Empty tiles (tMax < tMin) short-
//                circuit to the background.
//
// The hierarchical coarse pass avoids the O(numLower) flat iteration of
// the milestone-2 v1 implementation.  Storage is 8 bytes per tile (just
// the t-range); the per-lower beam list is no longer materialised since
// the v2 fine pass doesn't consume it (a future leaf-only walker, M2b,
// will need it back).

#define _USE_MATH_DEFINES
#include <cmath>
#include <chrono>
#include <iostream>

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

using GridT  = nanovdb::FloatGrid;
using CoordT = nanovdb::Coord;
using RealT  = float;
using Vec3T  = nanovdb::math::Vec3<RealT>;
using RayT   = nanovdb::math::Ray<RealT>;
using NMgrT  = nanovdb::NodeManager<float>;

constexpr int TILE_SIZE        = 16;
constexpr int THREADS_PER_TILE = TILE_SIZE * TILE_SIZE;
constexpr int NUM_WARPS        = THREADS_PER_TILE / 32;

struct CamParams {
    Vec3T eye;
    float wBBoxDimZ;
    Vec3T wBBoxCenter;
    int   width, height;
};

__device__ inline void pixelToWorldRay(int px, int py, const CamParams& c, Vec3T& outO, Vec3T& outD)
{
    const float fov    = 45.f;
    const float u      = (float(px) + 0.5f) / c.width;
    const float v      = (float(py) + 0.5f) / c.height;
    const float aspect = c.width / float(c.height);
    const float Px     = (2.f * u - 1.f) * tanf(fov * 0.5f * float(M_PI) / 180.f) * aspect;
    const float Py     = (2.f * v - 1.f) * tanf(fov * 0.5f * float(M_PI) / 180.f);
    Vec3T       dir(Px, Py, -1.f);
    dir.normalize();
    outO = c.eye;
    outD = dir;
}

__device__ inline nanovdb::math::BBox<nanovdb::Vec3f> coordBBoxToFloat(const nanovdb::CoordBBox& bb)
{
    return nanovdb::math::BBox<nanovdb::Vec3f>(
        nanovdb::Vec3f((float)bb.min()[0],     (float)bb.min()[1],     (float)bb.min()[2]),
        nanovdb::Vec3f((float)bb.max()[0]+1.f, (float)bb.max()[1]+1.f, (float)bb.max()[2]+1.f));
}

// Block-wide "any thread voted true?" reduction using shared memory.  Has
// two block syncs.  Cheap when called O(uppers) = 8 times per tile.
__device__ inline bool blockAnyHit(bool myHit, unsigned int* warpAny)
{
    const int tid = threadIdx.x;
    const int warpId = tid >> 5;
    const int laneId = tid & 31;
    unsigned int warpMask = __ballot_sync(0xFFFFFFFF, myHit);
    if (laneId == 0) warpAny[warpId] = warpMask;
    __syncthreads();
    if (tid == 0) {
        unsigned int any = 0;
        #pragma unroll
        for (int w = 0; w < NUM_WARPS; ++w) any |= warpAny[w];
        warpAny[0] = any;
    }
    __syncthreads();
    return warpAny[0] != 0;
}

__global__ void coarsePass(const GridT* __restrict__ grid,
                           const NMgrT* __restrict__ mgr,
                           CamParams cam,
                           float* __restrict__ tMinOut,
                           float* __restrict__ tMaxOut,
                           int tilesX, int tilesY)
{
    __shared__ unsigned int warpAny[NUM_WARPS];
    __shared__ float        sharedMin[NUM_WARPS];
    __shared__ float        sharedMax[NUM_WARPS];

    const int tid     = threadIdx.x;
    const int tileX   = blockIdx.x;
    const int tileY   = blockIdx.y;
    const int tileIdx = tileY * tilesX + tileX;

    const int px = tileX * TILE_SIZE + (tid % TILE_SIZE);
    const int py = tileY * TILE_SIZE + (tid / TILE_SIZE);
    const bool isValidPixel = (px < cam.width) && (py < cam.height);

    Vec3T rayO, rayD;
    pixelToWorldRay(px, py, cam, rayO, rayD);
    RayT  wRay(rayO, rayD);
    RayT  iRay = wRay.worldToIndexF(*grid);

    float myMin = 1e30f, myMax = -1e30f;

    const uint64_t numUpper = mgr->upperCount();
    for (uint64_t u = 0; u < numUpper; ++u) {
        const auto& upper = mgr->upper((uint32_t)u);
        auto uBox = coordBBoxToFloat(upper.bbox());

        float ut0, ut1;
        bool myUHit = isValidPixel && iRay.intersects(uBox, ut0, ut1);
        if (!blockAnyHit(myUHit, warpAny)) continue;

        // Descend into upper's lower children. All threads iterate in
        // lockstep; only those whose ray hits a given lower update myMin/myMax.
        for (auto it = upper.cbeginChild(); it; ++it) {
            const auto& lower = *it;
            auto lBox = coordBBoxToFloat(lower.bbox());

            float lt0, lt1;
            if (isValidPixel && iRay.intersects(lBox, lt0, lt1)) {
                if (lt0 < myMin) myMin = lt0;
                if (lt1 > myMax) myMax = lt1;
            }
        }
    }

    // Block-wide reduction: per-thread (myMin, myMax) -> tile-wide (tMin, tMax).
    // Warp-level shuffle followed by per-warp lane-0 spill and a final scalar pass.
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
                         CamParams cam,
                         float* __restrict__ image,
                         int tilesX, int tilesY)
{
    const int tid     = threadIdx.x;
    const int tileX   = blockIdx.x;
    const int tileY   = blockIdx.y;
    const int tileIdx = tileY * tilesX + tileX;

    const int px = tileX * TILE_SIZE + (tid % TILE_SIZE);
    const int py = tileY * TILE_SIZE + (tid / TILE_SIZE);
    if (px >= cam.width || py >= cam.height) return;
    const int pixel = py * cam.width + px;

    const float tMin = tMinIn[tileIdx];
    const float tMax = tMaxIn[tileIdx];

    const int   mask = 1 << 7;
    const float bg   = ((px & mask) ^ (py & mask)) ? 1.f : 0.5f;

    if (tMax < tMin) { image[pixel] = bg; return; } // empty tile

    Vec3T rayO, rayD;
    pixelToWorldRay(px, py, cam, rayO, rayD);
    RayT wRay(rayO, rayD);
    RayT iRay = wRay.worldToIndexF(*grid);

    RayT clipped = iRay;
    clipped.setTimes(tMin > 0.f ? tMin : iRay.t0(), tMax);

    auto acc = grid->tree().getAccessor();
    CoordT ijk;
    float v, t;
    if (nanovdb::math::ZeroCrossing(clipped, acc, ijk, v, t)) {
        const float wT0 = t * float(grid->voxelSize()[0]);
        image[pixel] = wT0 / (cam.wBBoxDimZ * 2);
    } else {
        image[pixel] = bg;
    }
}

} // namespace

void runNanoVDBBeam(nanovdb::GridHandle<BufferT>& handle, int numIterations,
                    int width, int height, BufferT& imageBuffer)
{
    auto* h_grid = handle.grid<float>();
    if (!h_grid) throw std::runtime_error("GridHandle does not contain a valid host grid");

    handle.deviceUpload();
    auto* d_grid = handle.deviceGrid<float>();
    if (!d_grid) throw std::runtime_error("GridHandle does not contain a valid device grid");

    auto mgrHandle = nanovdb::cuda::createNodeManager<float>(d_grid);
    auto* d_mgr    = mgrHandle.deviceMgr<float>();
    if (!d_mgr) throw std::runtime_error("NodeManager allocation failed");

    auto hostMgrHandle = nanovdb::createNodeManager<float, nanovdb::HostBuffer>(*h_grid);
    auto* h_mgr = hostMgrHandle.mgr<float>();
    std::cout << "Beam tracer: upperCount=" << h_mgr->upperCount()
              << "  lowerCount=" << h_mgr->lowerCount() << "\n";

    const float wBBoxDimZ   = (float)h_grid->worldBBox().dim()[2] * 2.f;
    const Vec3T wBBoxCenter = Vec3T(h_grid->worldBBox().min() + h_grid->worldBBox().dim() * 0.5f);
    CamParams cam;
    cam.eye         = wBBoxCenter + Vec3T(0, 0, wBBoxDimZ);
    cam.wBBoxDimZ   = wBBoxDimZ;
    cam.wBBoxCenter = wBBoxCenter;
    cam.width       = width;
    cam.height      = height;

    const int tilesX = (width  + TILE_SIZE - 1) / TILE_SIZE;
    const int tilesY = (height + TILE_SIZE - 1) / TILE_SIZE;
    const int numTiles = tilesX * tilesY;

    float *d_tMin = nullptr, *d_tMax = nullptr;
    cudaMalloc(&d_tMin, sizeof(float) * numTiles);
    cudaMalloc(&d_tMax, sizeof(float) * numTiles);

    imageBuffer.deviceUpload();
    float* d_image = reinterpret_cast<float*>(imageBuffer.deviceData());

    dim3 grid2D(tilesX, tilesY);
    dim3 block1D(THREADS_PER_TILE);

    for (int i = 0; i < 5; ++i) {
        coarsePass<<<grid2D, block1D>>>(d_grid, d_mgr, cam, d_tMin, d_tMax, tilesX, tilesY);
        finePass  <<<grid2D, block1D>>>(d_grid, d_tMin, d_tMax, cam, d_image, tilesX, tilesY);
    }
    cudaDeviceSynchronize();

    cudaEvent_t e0, e1, e2;
    cudaEventCreate(&e0); cudaEventCreate(&e1); cudaEventCreate(&e2);
    float totalCoarse = 0.f, totalFine = 0.f;
    for (int i = 0; i < numIterations; ++i) {
        cudaEventRecord(e0);
        coarsePass<<<grid2D, block1D>>>(d_grid, d_mgr, cam, d_tMin, d_tMax, tilesX, tilesY);
        cudaEventRecord(e1);
        finePass  <<<grid2D, block1D>>>(d_grid, d_tMin, d_tMax, cam, d_image, tilesX, tilesY);
        cudaEventRecord(e2);
        cudaEventSynchronize(e2);
        float t01 = 0.f, t12 = 0.f;
        cudaEventElapsedTime(&t01, e0, e1);
        cudaEventElapsedTime(&t12, e1, e2);
        totalCoarse += t01;
        totalFine   += t12;
    }
    cudaEventDestroy(e0); cudaEventDestroy(e1); cudaEventDestroy(e2);

    const float avgCoarse = totalCoarse / numIterations;
    const float avgFine   = totalFine   / numIterations;
    std::cout << "Beam tracer (NanoVDB-Cuda) avg ms:"
              << " coarse=" << avgCoarse
              << " fine=" << avgFine
              << " total=" << (avgCoarse + avgFine)
              << "\n";

    imageBuffer.deviceDownload();
    saveImage("raytrace_level_set-nanovdb-cuda-beam.pfm", width, height, (float*)imageBuffer.data());

    cudaFree(d_tMin);
    cudaFree(d_tMax);
}
