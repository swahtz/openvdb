// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

#pragma once

#define _USE_MATH_DEFINES
#include <algorithm>
#include <cmath>
#include <chrono>
#include <cstring>
#include <fstream>
#include <iostream>
#include <numeric>
#include <string>
#include <vector>
#include <nanovdb/NanoVDB.h>
#include "ComputePrimitives.h"

#if defined(NANOVDB_USE_CUDA)
#include <cuda_runtime_api.h>
#endif

#if defined(NANOVDB_USE_NVTX)
#include <nvtx3/nvToolsExt.h>
#endif

inline __hostdev__ uint32_t CompactBy1(uint32_t x)
{
    x &= 0x55555555;
    x = (x ^ (x >> 1)) & 0x33333333;
    x = (x ^ (x >> 2)) & 0x0f0f0f0f;
    x = (x ^ (x >> 4)) & 0x00ff00ff;
    x = (x ^ (x >> 8)) & 0x0000ffff;
    return x;
}

inline __hostdev__ uint32_t SeparateBy1(uint32_t x)
{
    x &= 0x0000ffff;
    x = (x ^ (x << 8)) & 0x00ff00ff;
    x = (x ^ (x << 4)) & 0x0f0f0f0f;
    x = (x ^ (x << 2)) & 0x33333333;
    x = (x ^ (x << 1)) & 0x55555555;
    return x;
}

inline __hostdev__ void mortonDecode(uint32_t code, uint32_t& x, uint32_t& y)
{
    x = CompactBy1(code);
    y = CompactBy1(code >> 1);
}

inline __hostdev__ void mortonEncode(uint32_t& code, uint32_t x, uint32_t y)
{
    code = SeparateBy1(x) | (SeparateBy1(y) << 1);
}

// Times the kernel launch + sync only. On the CUDA path we use cudaEvents so
// the reported figure is kernel-only and excludes host-side dispatch/launch
// overhead. On the host path we fall back to std::chrono. An NVTX range is
// pushed around the launch when NANOVDB_USE_NVTX is defined so external
// profilers (Nsight Systems, ncu --nvtx) can isolate exactly this region.
template<typename RenderFn, typename GridT>
inline float renderImage(bool useCuda, const RenderFn renderOp, int width, int height, float* image, const GridT* grid)
{
#if defined(NANOVDB_USE_NVTX)
    nvtxRangePush("raytrace_kernel");
#endif

#if defined(NANOVDB_USE_CUDA) && defined(__CUDACC__)
    if (useCuda) {
        cudaEvent_t startEvent, stopEvent;
        cudaEventCreate(&startEvent);
        cudaEventCreate(&stopEvent);

        cudaEventRecord(startEvent, 0);
        computeForEach(
            useCuda, width * height, 256, __FILE__, __LINE__, [renderOp, image, grid] __hostdev__(int start, int end) {
                renderOp(start, end, image, grid);
            });
        cudaEventRecord(stopEvent, 0);
        cudaEventSynchronize(stopEvent);

        float durationMs = 0.f;
        cudaEventElapsedTime(&durationMs, startEvent, stopEvent);

        cudaEventDestroy(startEvent);
        cudaEventDestroy(stopEvent);

#if defined(NANOVDB_USE_NVTX)
        nvtxRangePop();
#endif
        return durationMs;
    }
#endif

    using ClockT = std::chrono::high_resolution_clock;
    auto t0 = ClockT::now();
    computeForEach(
        useCuda, width * height, 256, __FILE__, __LINE__, [renderOp, image, grid] __hostdev__(int start, int end) {
            renderOp(start, end, image, grid);
        });
    computeSync(useCuda, __FILE__, __LINE__);
    auto t1 = ClockT::now();

#if defined(NANOVDB_USE_NVTX)
    nvtxRangePop();
#endif
    return std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count() / 1000.f;
}

inline void saveImage(const std::string& filename, int width, int height, const float* image)
{
    const auto isLittleEndian = []() -> bool {
        static int  x = 1;
        static bool result = reinterpret_cast<uint8_t*>(&x)[0] == 1;
        return result;
    };

    float scale = 1.0f;
    if (isLittleEndian())
        scale = -scale;

    std::fstream fs(filename, std::ios::out | std::ios::binary);
    if (!fs.is_open()) {
        throw std::runtime_error("Unable to open file: " + filename);
    }

    fs << "Pf\n"
       << width << "\n"
       << height << "\n"
       << scale << "\n";

    for (int i = 0; i < width * height; ++i) {
        float r = image[i];
        fs.write((char*)&r, sizeof(float));
    }
}

template<typename Vec3T>
struct RayGenOp
{
    float mWBBoxDimZ;
    Vec3T mWBBoxCenter;

    inline RayGenOp(float wBBoxDimZ, Vec3T wBBoxCenter)
        : mWBBoxDimZ(wBBoxDimZ)
        , mWBBoxCenter(wBBoxCenter)
    {
    }

    inline __hostdev__ void operator()(int i, int w, int h, Vec3T& outOrigin, Vec3T& outDir) const
    {
        // perspective camera along Z-axis...
        uint32_t x, y;
#if 0
        mortonDecode(i, x, y);
#else
        x = i % w;
        y = i / w;
#endif
        const float fov = 45.f;
        const float u = (float(x) + 0.5f) / w;
        const float v = (float(y) + 0.5f) / h;
        const float aspect = w / float(h);
        const float Px = (2.f * u - 1.f) * tanf(fov / 2 * 3.14159265358979323846f / 180.f) * aspect;
        const float Py = (2.f * v - 1.f) * tanf(fov / 2 * 3.14159265358979323846f / 180.f);
        const Vec3T origin = mWBBoxCenter + Vec3T(0, 0, mWBBoxDimZ);
        Vec3T       dir(Px, Py, -1.f);
        dir.normalize();
        outOrigin = origin;
        outDir = dir;
    }
};

struct CompositeOp
{
    inline __hostdev__ void operator()(float* outImage, int i, int w, int h, float value, float alpha) const
    {
        uint32_t x, y;
        int      offset;
#if 0
        mortonDecode(i, x, y);
        offset = x + y * w;
#else
        x = i % w;
        y = i / w;
        offset = i;
#endif

        // checkerboard background...
        const int   mask = 1 << 7;
        const float bg = ((x & mask) ^ (y & mask)) ? 1.0f : 0.5f;
        outImage[offset] = alpha * value + (1.0f - alpha) * bg;
    }
};

struct BenchmarkOptions
{
    int         numWarmup = 10;
    int         numIterations = 50;
    int         width = 1024;
    int         height = 1024;
    std::string outPrefix;
};

inline BenchmarkOptions parseBenchmarkOptions(int ac, char** av, int firstArg = 1)
{
    BenchmarkOptions opts;
    for (int i = firstArg; i < ac; ++i) {
        const char* a = av[i];
        auto        next = [&](const char* flag) -> const char* {
            if (i + 1 >= ac) {
                std::cerr << "Missing value for " << flag << std::endl;
                std::exit(1);
            }
            return av[++i];
        };
        if (std::strcmp(a, "--warmup") == 0)
            opts.numWarmup = std::atoi(next(a));
        else if (std::strcmp(a, "--iters") == 0)
            opts.numIterations = std::atoi(next(a));
        else if (std::strcmp(a, "--width") == 0)
            opts.width = std::atoi(next(a));
        else if (std::strcmp(a, "--height") == 0)
            opts.height = std::atoi(next(a));
        else if (std::strcmp(a, "--out-prefix") == 0)
            opts.outPrefix = next(a);
    }
    return opts;
}

struct BenchmarkStats
{
    float mean   = 0.f;
    float median = 0.f;
    float minV   = 0.f;
    float maxV   = 0.f;
    float stddev = 0.f;
};

inline BenchmarkStats computeStats(std::vector<float> timings)
{
    BenchmarkStats s;
    if (timings.empty())
        return s;
    s.minV = *std::min_element(timings.begin(), timings.end());
    s.maxV = *std::max_element(timings.begin(), timings.end());
    s.mean = std::accumulate(timings.begin(), timings.end(), 0.f) / float(timings.size());
    std::sort(timings.begin(), timings.end());
    const size_t n = timings.size();
    s.median = (n & 1) ? timings[n / 2] : 0.5f * (timings[n / 2 - 1] + timings[n / 2]);
    float ss = 0.f;
    for (float t : timings) {
        float d = t - s.mean;
        ss += d * d;
    }
    s.stddev = std::sqrt(ss / float(timings.size()));
    return s;
}

inline void printStats(const char* label, const BenchmarkStats& s)
{
    std::cout << label << " ms: median=" << s.median
              << " min=" << s.minV
              << " max=" << s.maxV
              << " mean=" << s.mean
              << " stddev=" << s.stddev
              << std::endl;
}
