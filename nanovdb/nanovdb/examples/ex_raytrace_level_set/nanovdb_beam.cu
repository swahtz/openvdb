// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

// Beam-tracing renderer (milestone 2): two-kernel pipeline.
//
//   coarsePass:  one CUDA block per 16x16 screen tile (256 threads).  Each
//                thread casts its pixel's ray and the block cooperatively
//                builds the union of lower-internal nodes whose AABB any
//                ray inside the tile hits.  The union is written to a
//                per-tile beam list in global memory.
//
//   finePass:    same launch geometry.  Each thread reads its tile's beam
//                list and walks only those lower-internal regions, running
//                the per-leaf zero-crossing logic confined to the [t_enter,
//                t_exit] of each listed lower.
//
// The intent is to amortise the root/upper tree descent (coherent across
// the tile) and to short-circuit pure empty-space tiles (~50-80% of tiles
// on the test SDFs hit zero lower-internals).

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

// Tile and beam dimensions. 16x16 tile = 256 threads/block; max 64 entries per
// tile beam list (milestone 1 measured max=39 across our SDFs).
constexpr int TILE_SIZE     = 16;
constexpr int THREADS_PER_TILE = TILE_SIZE * TILE_SIZE;
constexpr int MAX_BEAM_LEN  = 64;

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

// One block per tile. Each thread = one pixel. The block cooperatively
// iterates all lower-internals (mgr->lower(i)); for each, all threads vote
// (slab test) and if any thread hits, the lower index is appended to the
// per-tile beam list in shared memory.  At the end the list is written to
// global memory.
__global__ void coarsePass(const GridT* __restrict__ grid,
                           const NMgrT* __restrict__ mgr,
                           CamParams cam,
                           int* __restrict__ beamLists,    // [numTiles * MAX_BEAM_LEN]
                           int* __restrict__ beamCounts,   // [numTiles]
                           int tilesX, int tilesY)
{
    __shared__ int sharedBeam[MAX_BEAM_LEN];
    __shared__ int sharedCount;
    __shared__ unsigned int warpAny[THREADS_PER_TILE / 32];

    const int tid     = threadIdx.x;
    const int tileX   = blockIdx.x;
    const int tileY   = blockIdx.y;
    const int tileIdx = tileY * tilesX + tileX;

    const int px = tileX * TILE_SIZE + (tid % TILE_SIZE);
    const int py = tileY * TILE_SIZE + (tid / TILE_SIZE);
    const bool isValidPixel = (px < cam.width) && (py < cam.height);

    if (tid == 0) sharedCount = 0;
    __syncthreads();

    // Build this thread's index-space ray.
    Vec3T rayO, rayD;
    pixelToWorldRay(px, py, cam, rayO, rayD);
    RayT wRay(rayO, rayD);
    RayT iRay = wRay.worldToIndexF(*grid);

    const int warpId = tid >> 5;
    const int laneId = tid & 31;

    const uint64_t numLower = mgr->lowerCount();
    for (uint64_t i = 0; i < numLower; ++i) {
        auto bb = mgr->lower((uint32_t)i).bbox();
        nanovdb::math::BBox<nanovdb::Vec3f> box(
            nanovdb::Vec3f((float)bb.min()[0],     (float)bb.min()[1],     (float)bb.min()[2]),
            nanovdb::Vec3f((float)bb.max()[0]+1.f, (float)bb.max()[1]+1.f, (float)bb.max()[2]+1.f));

        float t0, t1;
        bool myHit = isValidPixel && iRay.intersects(box, t0, t1);

        unsigned int warpMask = __ballot_sync(0xFFFFFFFF, myHit);
        if (laneId == 0) warpAny[warpId] = warpMask;
        __syncthreads();

        if (tid == 0) {
            unsigned int anyMask = 0;
            #pragma unroll
            for (int w = 0; w < THREADS_PER_TILE / 32; ++w) anyMask |= warpAny[w];
            if (anyMask && sharedCount < MAX_BEAM_LEN) {
                sharedBeam[sharedCount++] = (int)i;
            }
        }
        __syncthreads();
    }

    // Stream the per-tile beam list to global memory.
    const int count = sharedCount;
    if (tid == 0) beamCounts[tileIdx] = count;
    for (int k = tid; k < count; k += blockDim.x) {
        beamLists[tileIdx * MAX_BEAM_LEN + k] = sharedBeam[k];
    }
}

// One block per tile, one thread per pixel.  Each thread reads its tile's
// beam list (lower-internal indices in front-to-back order is _not_
// guaranteed here -- the coarse pass appends in index order, not in t
// order), intersects its ray against each listed lower's bbox to derive a
// local [t_enter, t_exit], and runs the existing ZeroCrossing inside that
// clipped range.  The first hit (smallest t along the ray) wins.
__global__ void finePass(const GridT* __restrict__ grid,
                         const NMgrT* __restrict__ mgr,
                         const int*   __restrict__ beamLists,
                         const int*   __restrict__ beamCounts,
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

    const int beamLen = beamCounts[tileIdx];

    // Checkerboard background; mirrors CompositeOp.
    const int   mask = 1 << 7;
    const float bg   = ((px & mask) ^ (py & mask)) ? 1.f : 0.5f;

    if (beamLen == 0) {
        image[pixel] = bg; // empty-space tile, no surface
        return;
    }

    Vec3T rayO, rayD;
    pixelToWorldRay(px, py, cam, rayO, rayD);
    RayT wRay(rayO, rayD);
    RayT iRay = wRay.worldToIndexF(*grid);

    auto acc = grid->tree().getAccessor();

    // Union the t-ranges of all listed lowers, then run a single ZeroCrossing
    // on the clipped ray.  This isolates the "skip empty tiles" win from the
    // per-lower walk cost.  TODO(beam_M2b): replace with a leaf-only walker
    // that descends directly from the listed lower-internal pointers and
    // avoids the per-call root-descent inside ZeroCrossing.
    float tMin = 1e30f, tMax = -1e30f;
    for (int k = 0; k < beamLen; ++k) {
        const int lowerIdx = beamLists[tileIdx * MAX_BEAM_LEN + k];
        auto bb = mgr->lower((uint32_t)lowerIdx).bbox();
        nanovdb::math::BBox<nanovdb::Vec3f> box(
            nanovdb::Vec3f((float)bb.min()[0],     (float)bb.min()[1],     (float)bb.min()[2]),
            nanovdb::Vec3f((float)bb.max()[0]+1.f, (float)bb.max()[1]+1.f, (float)bb.max()[2]+1.f));
        float tEnter, tExit;
        if (iRay.intersects(box, tEnter, tExit)) {
            if (tEnter < tMin) tMin = tEnter;
            if (tExit  > tMax) tMax = tExit;
        }
    }
    if (tMax < 0.f) { image[pixel] = bg; return; }

    RayT clipped = iRay;
    clipped.setTimes(tMin > 0.f ? tMin : iRay.t0(), tMax);

    CoordT ijk;
    float v, t;
    if (nanovdb::math::ZeroCrossing(clipped, acc, ijk, v, t)) {
        const float wT0 = t * float(grid->voxelSize()[0]);
        const float val = wT0 / (cam.wBBoxDimZ * 2);
        image[pixel] = val; // alpha=1
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

    // Ensure device upload.
    handle.deviceUpload();
    auto* d_grid = handle.deviceGrid<float>();
    if (!d_grid) throw std::runtime_error("GridHandle does not contain a valid device grid");

    // Build and upload NodeManager (gives random access to lower-internals on
    // device, which we need to enumerate during the coarse pass).  The cuda
    // factory takes the device grid pointer and constructs the manager in
    // device memory.
    auto mgrHandle  = nanovdb::cuda::createNodeManager<float>(d_grid);
    auto* d_mgr     = mgrHandle.deviceMgr<float>();
    if (!d_mgr) throw std::runtime_error("NodeManager allocation failed");

    // For diagnostics, also build a tiny host-side NodeManager so we can
    // report node counts without a device round-trip.
    auto hostMgrHandle = nanovdb::createNodeManager<float, nanovdb::HostBuffer>(*h_grid);
    auto* h_mgr = hostMgrHandle.mgr<float>();
    std::cout << "Beam tracer: upperCount=" << h_mgr->upperCount()
              << "  lowerCount=" << h_mgr->lowerCount() << "\n";

    // Camera params (mirrors RayGenOp).
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

    // Per-tile beam list and counts.
    int *d_beamLists = nullptr, *d_beamCounts = nullptr;
    cudaMalloc(&d_beamLists,  sizeof(int) * numTiles * MAX_BEAM_LEN);
    cudaMalloc(&d_beamCounts, sizeof(int) * numTiles);

    imageBuffer.deviceUpload();
    float* d_image = reinterpret_cast<float*>(imageBuffer.deviceData());

    dim3 grid2D(tilesX, tilesY);
    dim3 block1D(THREADS_PER_TILE);

    // Warm-up
    for (int i = 0; i < 5; ++i) {
        coarsePass<<<grid2D, block1D>>>(d_grid, d_mgr, cam, d_beamLists, d_beamCounts, tilesX, tilesY);
        finePass  <<<grid2D, block1D>>>(d_grid, d_mgr, d_beamLists, d_beamCounts, cam, d_image, tilesX, tilesY);
    }
    cudaDeviceSynchronize();

    // Timed iterations
    cudaEvent_t e0, e1, e2;
    cudaEventCreate(&e0); cudaEventCreate(&e1); cudaEventCreate(&e2);
    float totalCoarse = 0.f, totalFine = 0.f;
    for (int i = 0; i < numIterations; ++i) {
        cudaEventRecord(e0);
        coarsePass<<<grid2D, block1D>>>(d_grid, d_mgr, cam, d_beamLists, d_beamCounts, tilesX, tilesY);
        cudaEventRecord(e1);
        finePass  <<<grid2D, block1D>>>(d_grid, d_mgr, d_beamLists, d_beamCounts, cam, d_image, tilesX, tilesY);
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

    cudaFree(d_beamLists);
    cudaFree(d_beamCounts);
}
