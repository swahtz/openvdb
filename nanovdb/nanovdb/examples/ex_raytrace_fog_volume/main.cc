// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

#include <algorithm>
#include <iostream>
#include <string>
#include <nanovdb/io/IO.h>
#include <nanovdb/tools/CreatePrimitives.h>

#if defined(NANOVDB_USE_CUDA)
#include <nanovdb/cuda/DeviceBuffer.h>
using BufferT = nanovdb::cuda::DeviceBuffer;
#else
using BufferT = nanovdb::HostBuffer;
#endif

#include "common.h"

extern void runNanoVDB(nanovdb::GridHandle<BufferT>& handle, const BenchmarkOptions& opts, BufferT& imageBuffer);
extern void runNanoVDBHdda(nanovdb::GridHandle<BufferT>& handle, const BenchmarkOptions& opts, BufferT& imageBuffer);
#if defined(NANOVDB_USE_OPENVDB)
extern void runOpenVDB(nanovdb::GridHandle<BufferT>& handle, const BenchmarkOptions& opts, BufferT& imageBuffer);
#endif

int main(int ac, char** av)
{
    try {
        // Optional first positional arg is a .nvdb file path; the rest are flags.
        const bool hasInput = (ac > 1 && av[1][0] != '-');
        const int  firstFlag = hasInput ? 2 : 1;
        BenchmarkOptions opts = parseBenchmarkOptions(ac, av, firstFlag);

        nanovdb::GridHandle<BufferT> handle;
        if (hasInput) {
            handle = nanovdb::io::readGrid<BufferT>(av[1]);
            std::cout << "Loaded NanoVDB grid[" << handle.gridMetaData()->shortGridName() << "]...\n";
        } else {
            handle = nanovdb::tools::createFogVolumeSphere<float, BufferT>(100.0f, nanovdb::Vec3d(-20, 0, 0), 1.0, 3.0, nanovdb::Vec3d(0), "sphere");
        }

        if (handle.gridMetaData()->isFogVolume() == false) {
            throw std::runtime_error("Grid must be a fog volume");
        }

        BufferT imageBuffer(opts.width * opts.height * sizeof(float));

        runNanoVDB(handle, opts, imageBuffer);
        runNanoVDBHdda(handle, opts, imageBuffer);
#if defined(NANOVDB_USE_OPENVDB)
        runOpenVDB(handle, opts, imageBuffer);
#endif
    }
    catch (const std::exception& e) {
        std::cerr << "An exception occurred: \"" << e.what() << "\"" << std::endl;
    }
    return 0;
}
