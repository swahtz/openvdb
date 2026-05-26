// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

#define _USE_MATH_DEFINES
#include <cmath>
#include <chrono>

#if defined(NANOVDB_USE_CUDA)
#include <nanovdb/cuda/DeviceBuffer.h>
using BufferT = nanovdb::cuda::DeviceBuffer;
#else
using BufferT = nanovdb::HostBuffer;
#endif
#include <nanovdb/GridHandle.h>
#include <nanovdb/io/IO.h>
#include <nanovdb/math/Ray.h>
#include <nanovdb/math/HDDA.h>

#include "common.h"

namespace {

// Run `n` iterations of `op()` on the host side, collecting per-iteration
// kernel time in `out`. Caller is responsible for any warmup before `n`.
template<typename Op>
inline void timedLoop(int n, std::vector<float>& out, const Op& op)
{
    out.reserve(out.size() + n);
    for (int i = 0; i < n; ++i)
        out.push_back(op());
}

} // namespace

void runNanoVDB(nanovdb::GridHandle<BufferT>& handle, const BenchmarkOptions& opts, BufferT& imageBuffer)
{
    using GridT = nanovdb::FloatGrid;
    using CoordT = nanovdb::Coord;
    using RealT = float;
    using Vec3T = nanovdb::math::Vec3<RealT>;
    using RayT = nanovdb::math::Ray<RealT>;

    const int width = opts.width;
    const int height = opts.height;

    auto *h_grid = handle.grid<float>();
    if (!h_grid)
        throw std::runtime_error("GridHandle does not contain a valid host grid");

    float* h_outImage = reinterpret_cast<float*>(imageBuffer.data());

    float              wBBoxDimZ = (float)h_grid->worldBBox().dim()[2] * 2;
    Vec3T              wBBoxCenter = Vec3T(h_grid->worldBBox().min() + h_grid->worldBBox().dim() * 0.5f);
    nanovdb::CoordBBox treeIndexBbox = h_grid->tree().bbox();
    std::cout << "Bounds: "
              << "[" << treeIndexBbox.min()[0] << "," << treeIndexBbox.min()[1] << "," << treeIndexBbox.min()[2] << "] -> ["
              << treeIndexBbox.max()[0] << "," << treeIndexBbox.max()[1] << "," << treeIndexBbox.max()[2] << "]" << std::endl;

    RayGenOp<Vec3T> rayGenOp(wBBoxDimZ, wBBoxCenter);
    CompositeOp     compositeOp;

    auto renderOp = [width, height, rayGenOp, compositeOp, treeIndexBbox] __hostdev__(int start, int end, float* image, const GridT* grid) {
        // get an accessor.
        auto acc = grid->tree().getAccessor();

        for (int i = start; i < end; ++i) {
            Vec3T rayEye;
            Vec3T rayDir;
            rayGenOp(i, width, height, rayEye, rayDir);
            // generate ray.
            RayT wRay(rayEye, rayDir);
            // transform the ray to the grid's index-space.
            RayT iRay = wRay.worldToIndexF(*grid);
            // clip to bounds.
            if (iRay.clip(treeIndexBbox) == false) {
                compositeOp(image, i, width, height, 0.0f, 0.0f);
                continue;
            }
            // integrate...
            const float dt = 0.5f;
            float       transmittance = 1.0f;
            for (float t = iRay.t0(); t < iRay.t1(); t += dt) {
                float sigma = acc.getValue(CoordT::Floor(iRay(t))) * 0.1f;
                transmittance *= 1.0f - sigma * dt;
            }
            // write transmittance.
            compositeOp(image, i, width, height, 0.0f, 1.0f - transmittance);
        }
    };

    const std::string outPrefix = opts.outPrefix.empty() ? std::string("raytrace_fog_volume") : opts.outPrefix;

    {
        for (int i = 0; i < opts.numWarmup; ++i)
            (void)renderImage(false, renderOp, width, height, h_outImage, h_grid);
        std::vector<float> timings;
        timedLoop(opts.numIterations, timings, [&] { return renderImage(false, renderOp, width, height, h_outImage, h_grid); });
        printStats("NanoVDB-Host-Fixed", computeStats(timings));
        saveImage(outPrefix + "-nanovdb-host-fixed.pfm", width, height, (float*)imageBuffer.data());
    }

#if defined(NANOVDB_USE_CUDA)
    handle.deviceUpload();

    auto* d_grid = handle.deviceGrid<float>();
    if (!d_grid)
        throw std::runtime_error("GridHandle does not contain a valid device grid");

    imageBuffer.deviceUpload();
    float* d_outImage = reinterpret_cast<float*>(imageBuffer.deviceData());

    {
        for (int i = 0; i < opts.numWarmup; ++i)
            (void)renderImage(true, renderOp, width, height, d_outImage, d_grid);
        std::vector<float> timings;
        timedLoop(opts.numIterations, timings, [&] { return renderImage(true, renderOp, width, height, d_outImage, d_grid); });
        printStats("NanoVDB-Cuda-Fixed", computeStats(timings));

        imageBuffer.deviceDownload();
        saveImage(outPrefix + "-nanovdb-cuda-fixed.pfm", width, height, (float*)imageBuffer.data());
    }
#endif
}

// Fog-volume integrator that uses TreeMarcher to leap empty space.
// Mirrors the OpenVDB tools::VolumeRayIntersector::march pattern: walk the
// tree at internal-node granularity, only doing the dt-march inside spans
// where a leaf is intersected. For sparse grids this is dramatically less
// work than the fixed-step march that runNanoVDB performs.
void runNanoVDBHdda(nanovdb::GridHandle<BufferT>& handle, const BenchmarkOptions& opts, BufferT& imageBuffer)
{
    using GridT = nanovdb::FloatGrid;
    using CoordT = nanovdb::Coord;
    using RealT = float;
    using Vec3T = nanovdb::math::Vec3<RealT>;
    using RayT = nanovdb::math::Ray<RealT>;

    const int width = opts.width;
    const int height = opts.height;

    auto *h_grid = handle.grid<float>();
    if (!h_grid)
        throw std::runtime_error("GridHandle does not contain a valid host grid");

    float* h_outImage = reinterpret_cast<float*>(imageBuffer.data());

    float              wBBoxDimZ = (float)h_grid->worldBBox().dim()[2] * 2;
    Vec3T              wBBoxCenter = Vec3T(h_grid->worldBBox().min() + h_grid->worldBBox().dim() * 0.5f);
    nanovdb::CoordBBox treeIndexBbox = h_grid->tree().bbox();

    RayGenOp<Vec3T> rayGenOp(wBBoxDimZ, wBBoxCenter);
    CompositeOp     compositeOp;

    auto renderOp = [width, height, rayGenOp, compositeOp, treeIndexBbox] __hostdev__(int start, int end, float* image, const GridT* grid) {
        using LeafNodeT = typename GridT::TreeType::LeafNodeType;
        using AccT = decltype(grid->tree().getAccessor());

        auto acc = grid->tree().getAccessor();

        for (int i = start; i < end; ++i) {
            Vec3T rayEye;
            Vec3T rayDir;
            rayGenOp(i, width, height, rayEye, rayDir);
            RayT wRay(rayEye, rayDir);
            RayT iRay = wRay.worldToIndexF(*grid);
            if (iRay.clip(treeIndexBbox) == false) {
                compositeOp(image, i, width, height, 0.0f, 0.0f);
                continue;
            }

            const float dt = 0.5f;
            float       transmittance = 1.0f;

            nanovdb::math::TreeMarcher<LeafNodeT, RayT, AccT, CoordT> marcher(acc);
            if (marcher.init(iRay)) {
                const LeafNodeT* leaf = nullptr;
                float            t0 = 0.f, t1 = 0.f;
                while (marcher.step(&leaf, t0, t1)) {
                    // dt-march only inside spans where a leaf is intersected;
                    // empty internal-node spans contribute zero opacity.
                    for (float t = t0; t < t1; t += dt) {
                        float sigma = acc.getValue(CoordT::Floor(iRay(t))) * 0.1f;
                        transmittance *= 1.0f - sigma * dt;
                    }
                }
            }
            compositeOp(image, i, width, height, 0.0f, 1.0f - transmittance);
        }
    };

    const std::string outPrefix = opts.outPrefix.empty() ? std::string("raytrace_fog_volume") : opts.outPrefix;

    {
        for (int i = 0; i < opts.numWarmup; ++i)
            (void)renderImage(false, renderOp, width, height, h_outImage, h_grid);
        std::vector<float> timings;
        timedLoop(opts.numIterations, timings, [&] { return renderImage(false, renderOp, width, height, h_outImage, h_grid); });
        printStats("NanoVDB-Host-Hdda", computeStats(timings));
        saveImage(outPrefix + "-nanovdb-host-hdda.pfm", width, height, (float*)imageBuffer.data());
    }

#if defined(NANOVDB_USE_CUDA)
    handle.deviceUpload();

    auto* d_grid = handle.deviceGrid<float>();
    if (!d_grid)
        throw std::runtime_error("GridHandle does not contain a valid device grid");

    imageBuffer.deviceUpload();
    float* d_outImage = reinterpret_cast<float*>(imageBuffer.deviceData());

    {
        for (int i = 0; i < opts.numWarmup; ++i)
            (void)renderImage(true, renderOp, width, height, d_outImage, d_grid);
        std::vector<float> timings;
        timedLoop(opts.numIterations, timings, [&] { return renderImage(true, renderOp, width, height, d_outImage, d_grid); });
        printStats("NanoVDB-Cuda-Hdda", computeStats(timings));

        imageBuffer.deviceDownload();
        saveImage(outPrefix + "-nanovdb-cuda-hdda.pfm", width, height, (float*)imageBuffer.data());
    }
#endif
}
