// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

#if defined(NANOVDB_USE_OPENVDB)

#define _USE_MATH_DEFINES
#include <cmath>
#include <chrono>

#include <openvdb/openvdb.h>
#include <openvdb/math/Ray.h>
#include <openvdb/tools/LevelSetSphere.h>

#include <nanovdb/tools/NanoToOpenVDB.h>

#include "common.h"

#if defined(NANOVDB_USE_CUDA)
#include <nanovdb/cuda/DeviceBuffer.h>
using BufferT = nanovdb::cuda::DeviceBuffer;
#else
using BufferT = nanovdb::HostBuffer;
#endif

void runOpenVDB(nanovdb::GridHandle<BufferT>& handle, const BenchmarkOptions& opts, BufferT& imageBuffer)
{
    using GridT = openvdb::FloatGrid;
    using CoordT = openvdb::Coord;
    using RealT = float;
    using Vec3T = openvdb::math::Vec3<RealT>;
    using RayT = openvdb::math::Ray<RealT>;

    const int width = opts.width;
    const int height = opts.height;

    openvdb::GridBase::Ptr srcGrid = nanovdb::tools::nanoToOpenVDB(handle);
    std::cout << "Exporting to OpenVDB grid[" << srcGrid->getName() << "]...\n";
    GridT::Ptr h_grid = openvdb::GridBase::grid<GridT>(srcGrid);

    float* h_outImage = reinterpret_cast<float*>(imageBuffer.data());

    auto  indexBBox = h_grid->evalActiveVoxelBoundingBox();
    auto  gridXform = h_grid->transformPtr();
    auto  worldBBox = gridXform->indexToWorld(indexBBox);
    float wBBoxDimZ = (float)worldBBox.extents()[2] * 2;
    Vec3T wBBoxCenter = Vec3T(worldBBox.min() + worldBBox.extents() * 0.5f);

    RayGenOp<Vec3T> rayGenOp(wBBoxDimZ, wBBoxCenter);
    CompositeOp     compositeOp;

    openvdb::CoordBBox treeIndexBbox;
    treeIndexBbox = h_grid->evalActiveVoxelBoundingBox();
    std::cout << "Bounds: " << treeIndexBbox << std::endl;

    auto renderOp = [width, height, rayGenOp, compositeOp, treeIndexBbox] __hostdev__(int start, int end, float* image, const GridT* grid) {
        // get an accessor.
        auto acc = grid->getAccessor();

        for (int i = start; i < end; ++i) {
            Vec3T rayEye;
            Vec3T rayDir;
            rayGenOp(i, width, height, rayEye, rayDir);
            // generate ray.
            RayT wRay(rayEye, rayDir);
            // transform the ray to the grid's index-space.
            RayT iRay = wRay.worldToIndex(*grid);
            // clip to bounds.
            if (iRay.clip(treeIndexBbox) == false) {
                compositeOp(image, i, width, height, 0.0f, 0.0f);
                continue;
            }
            // integrate...
            const float dt = 0.5f;
            float       transmittance = 1.0f;
            for (float t = iRay.t0(); t < iRay.t1(); t += dt) {
                float sigma = acc.getValue(CoordT::floor(iRay(t))) * 0.1f;
                transmittance *= 1.0f - sigma * dt;
            }
            // write transmittance.
            compositeOp(image, i, width, height, 0.0f, 1.0f - transmittance);
        }
    };

    const std::string outPrefix = opts.outPrefix.empty() ? std::string("raytrace_fog_volume") : opts.outPrefix;

    {
        for (int i = 0; i < opts.numWarmup; ++i)
            (void)renderImage(false, renderOp, width, height, h_outImage, h_grid.get());
        std::vector<float> timings;
        timings.reserve(opts.numIterations);
        for (int i = 0; i < opts.numIterations; ++i)
            timings.push_back(renderImage(false, renderOp, width, height, h_outImage, h_grid.get()));
        printStats("OpenVDB-Host", computeStats(timings));

        saveImage(outPrefix + "-openvdb-host.pfm", width, height, (float*)imageBuffer.data());
    }
}

#endif