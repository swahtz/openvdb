// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

/// @file BenchLoadAligned.cu
/// @brief Measures nanovdb::loadAligned on dense Vec4f / Vec4d grids (PR #2207 review).
///
/// Build twice from the repository root and compare the two binaries:
///
///   nvcc -std=c++17 -O3 -DNDEBUG --extended-lambda -arch=sm_XX -Inanovdb \
///        nanovdb/nanovdb/benchmark/BenchLoadAligned.cu -o bench_aligned
///   nvcc -std=c++17 -O3 -DNDEBUG --extended-lambda -arch=sm_XX -Inanovdb -DNANOVDB_DISABLE_LOAD_ALIGNED \
///        nanovdb/nanovdb/benchmark/BenchLoadAligned.cu -o bench_plain
///
/// Keep -DNDEBUG: in debug builds the alignment assert inside loadAligned dominates the timings.
/// Each kernel is timed as the median of 21 launches after one warm-up launch.

#include <nanovdb/NanoVDB.h>
#include <nanovdb/tools/CreateNanoGrid.h>
#include <nanovdb/tools/GridBuilder.h>
#include <algorithm>
#include <cstdio>
#include <vector>

using namespace nanovdb;

// One block per leaf, one thread per voxel: a fully coalesced sweep of every leaf value.
template<typename V>
__global__ void sweepLeaves(const NanoGrid<V>* grid, float* out)
{
    const auto& leaf = grid->tree().template getFirstNode<0>()[blockIdx.x];
    const V v = leaf.getValue(threadIdx.x);
    float s = 0;
    for (int k = 0; k < 4; ++k) s += float(v[k]);
    if (s == -1.f) out[0] = s; // keeps the load live without a store per voxel
}

// One warp per leaf, each thread reading 16 consecutive values: strided across lanes.
template<typename V>
__global__ void warpPerLeaf(const NanoGrid<V>* grid, uint32_t leafCount, float* out)
{
    const uint32_t w = (blockIdx.x * blockDim.x + threadIdx.x) / 32, lane = threadIdx.x % 32;
    if (w >= leafCount) return;
    const auto& leaf = grid->tree().template getFirstNode<0>()[w];
    float s = 0;
    for (int k = 0; k < 16; ++k) {
        const V v = leaf.getValue(lane * 16 + k);
        s += float(v[0]) + float(v[1]) + float(v[2]) + float(v[3]);
    }
    if (s == -1.f) out[0] = s;
}

// Random-access accessor reads, as in sampling.
template<typename V>
__global__ void accessorReads(const NanoGrid<V>* grid, const Coord* ijk, size_t n, float* out)
{
    const size_t i = blockIdx.x * size_t(blockDim.x) + threadIdx.x;
    if (i >= n) return;
    auto acc = grid->getAccessor();
    const V v = acc.getValue(ijk[i]);
    float s = 0;
    for (int k = 0; k < 4; ++k) s += float(v[k]);
    if (s == -1.f) out[0] = s;
}

template<typename LaunchT>
float medianMs(LaunchT launch)
{
    cudaEvent_t a, b;
    cudaEventCreate(&a);
    cudaEventCreate(&b);
    launch();
    cudaDeviceSynchronize();
    std::vector<float> t;
    for (int r = 0; r < 21; ++r) {
        cudaEventRecord(a);
        launch();
        cudaEventRecord(b);
        cudaEventSynchronize(b);
        float ms;
        cudaEventElapsedTime(&ms, a, b);
        t.push_back(ms);
    }
    std::sort(t.begin(), t.end());
    return t[t.size() / 2];
}

template<typename V>
void run(const char* name)
{
    const int R = 256; // 256^3 dense active voxels
    tools::build::Grid<V> src(V(0));
    auto a = src.getAccessor();
    for (int i = 0; i < R; ++i)
        for (int j = 0; j < R; ++j)
            for (int k = 0; k < R; ++k) a.setValue(Coord(i, j, k), V(i, j, k, 1));
    auto handle = tools::createNanoGrid(src);
    const uint32_t leafCount = handle.template grid<V>()->tree().nodeCount(0);

    void* d;
    cudaMalloc(&d, handle.bufferSize());
    cudaMemcpy(d, handle.data(), handle.bufferSize(), cudaMemcpyHostToDevice);
    const auto* grid = reinterpret_cast<const NanoGrid<V>*>(d);
    float* out;
    cudaMalloc(&out, sizeof(float));

    const size_t n = size_t(1) << 22;
    std::vector<Coord> coords(n);
    uint64_t s = 1;
    for (auto& c : coords) {
        s = s * 6364136223846793005ull + 1;
        c = Coord(int(s >> 33) % R, int(s >> 41) % R, int(s >> 49) % R);
    }
    Coord* dCoords;
    cudaMalloc(&dCoords, n * sizeof(Coord));
    cudaMemcpy(dCoords, coords.data(), n * sizeof(Coord), cudaMemcpyHostToDevice);

    const double bytes = double(leafCount) * 512 * sizeof(V);
    const float  tSweep = medianMs([&] { sweepLeaves<V><<<leafCount, 512>>>(grid, out); });
    const float  tWarp  = medianMs([&] { warpPerLeaf<V><<<(leafCount * 32 + 255) / 256, 256>>>(grid, leafCount, out); });
    const float  tAcc   = medianMs([&] { accessorReads<V><<<unsigned((n + 255) / 256), 256>>>(grid, dCoords, n, out); });
    std::printf("%-6s leaf sweep %7.3f ms (%5.0f GB/s) | warp per leaf %7.3f ms (%5.0f GB/s) | random accessor %7.3f ms\n",
                name, tSweep, bytes / tSweep / 1e6, tWarp, bytes / tWarp / 1e6, tAcc);

    cudaFree(d);
    cudaFree(out);
    cudaFree(dCoords);
}

int main()
{
    cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, 0);
#ifdef NANOVDB_DISABLE_LOAD_ALIGNED
    std::printf("%s (sm_%d%d), plain loads\n", prop.name, prop.major, prop.minor);
#else
    std::printf("%s (sm_%d%d), loadAligned\n", prop.name, prop.major, prop.minor);
#endif
    run<Vec4f>("Vec4f");
    run<Vec4d>("Vec4d");
    const cudaError_t err = cudaGetLastError();
    if (err != cudaSuccess) {
        std::printf("CUDA error: %s\n", cudaGetErrorString(err));
        return 1;
    }
    return 0;
}
