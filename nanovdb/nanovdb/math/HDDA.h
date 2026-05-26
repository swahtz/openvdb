// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: Apache-2.0

/// @file HDDA.h
///
/// @author Ken Museth
///
/// @brief Hierarchical Digital Differential Analyzers specialized for VDB.

#ifndef NANOVDB_HDDA_H_HAS_BEEN_INCLUDED
#define NANOVDB_HDDA_H_HAS_BEEN_INCLUDED

// Comment out to disable this explicit round-off check
#define ENFORCE_FORWARD_STEPPING

#include <nanovdb/NanoVDB.h> // only dependency

namespace nanovdb::math {

/// @brief A Digital Differential Analyzer specialized for OpenVDB grids
/// @note Conceptually similar to Bresenham's line algorithm applied
/// to a 3D Ray intersecting OpenVDB nodes or voxels. Log2Dim = 0
/// corresponds to a voxel and Log2Dim a tree node of size 2^Log2Dim.
///
/// @note The Ray template class is expected to have the following
/// methods: test(time), t0(), t1(), dir(), invDir(), and operator()(time).
/// See the example Ray class above for their definition.
///
/// @warning This DDA derives @c mStep, @c mDelta and @c mT1 from the @c Ray
/// passed to @c step/next/update on every call. The caller therefore must
/// keep the same @c Ray instance live (and unchanged) for the lifetime of
/// the HDDA, otherwise the walk will become inconsistent. The maximum time
/// of the walk is always @c ray.t1(); callers that previously relied on a
/// separate @c maxTime stored inside the HDDA should pre-shrink the ray's
/// time span via @c ray.setTimes() before calling @c init().
template<typename RayT, typename CoordT = Coord>
class HDDA
{
public:
    using RealType = typename RayT::RealType;
    using RealT = RealType;
    using Vec3Type = typename RayT::Vec3Type;
    using Vec3T = Vec3Type;
    using CoordType = CoordT;

    /// @brief Default ctor
    HDDA() = default;

    /// @brief ctor from ray and dimension at which the DDA marches
    __hostdev__ HDDA(const RayT& ray, int dim) { this->init(ray, dim); }

    /// @brief Re-initializes the HDDA
    /// @note @a maxTime is retained for API symmetry with the previous
    /// signature, but the lean HDDA no longer stores it. The effective
    /// maximum time of the walk is @c ray.t1(); callers must pre-shrink
    /// the ray if they require a tighter bound.
    __hostdev__ void init(const RayT& ray, RealT startTime, RealT maxTime, int dim)
    {
        assert(startTime <= maxTime);
        (void)maxTime;
        mDim = dim;
        mT0 = startTime;
        const Vec3T &pos = ray(mT0), &dir = ray.dir(), &inv = ray.invDir();
        mVoxel = RoundDown<CoordT>(pos) & (~(dim - 1));
        for (int axis = 0; axis < 3; ++axis) {
            if (dir[axis] == RealT(0)) { //handles dir = +/- 0
                mNext[axis] = Maximum<RealT>::value(); //i.e. disabled!
            } else if (inv[axis] > 0) {
                mNext[axis] = mT0 + (mVoxel[axis] + dim - pos[axis]) * inv[axis];
            } else {
                mNext[axis] = mT0 + (mVoxel[axis] - pos[axis]) * inv[axis];
            }
        }
    }

    /// @brief Simular to init above except it uses the bounds of the input ray
    __hostdev__ void init(const RayT& ray, int dim) { this->init(ray, ray.t0(), ray.t1(), dim); }

    /// @brief Updates the HDDA to march with the specified dimension
    __hostdev__ bool update(const RayT& ray, int dim)
    {
        if (mDim == dim)
            return false;

        // compute valid voxel range
        Coord voxelMax = (mVoxel + Coord(mDim - 1)) & (~(dim - 1));
        Coord voxelMin = mVoxel & (~(dim - 1));

        mDim = dim;
        const Vec3T &pos = ray(mT0), &inv = ray.invDir();
        mVoxel = RoundDown<CoordT>(pos) & (~(dim - 1));

        // clamp mVoxel to valid range
        mVoxel[0] = nanovdb::math::Min(mVoxel[0], voxelMax[0]);
        mVoxel[1] = nanovdb::math::Min(mVoxel[1], voxelMax[1]);
        mVoxel[2] = nanovdb::math::Min(mVoxel[2], voxelMax[2]);
        mVoxel[0] = nanovdb::math::Max(mVoxel[0], voxelMin[0]);
        mVoxel[1] = nanovdb::math::Max(mVoxel[1], voxelMin[1]);
        mVoxel[2] = nanovdb::math::Max(mVoxel[2], voxelMin[2]);

        for (int axis = 0; axis < 3; ++axis) {
            if (ray.dir()[axis] == RealT(0))
                continue;
            mNext[axis] = mT0 + (mVoxel[axis] - pos[axis]) * inv[axis];
            if (inv[axis] > 0)
                mNext[axis] += dim * inv[axis];
        }

        return true;
    }

    __hostdev__ int dim() const { return mDim; }

    /// @brief Increment the voxel index to next intersected voxel or node
    /// and returns true if the step in time does not exceed @c ray.t1().
    __hostdev__ bool step(const RayT& ray)
    {
        const int axis = MinIndex(mNext);
        switch (axis) {
        case 0:
            return step<0>(ray);
        case 1:
            return step<1>(ray);
        default:
            return step<2>(ray);
        }
    }

    /// @brief Return the index coordinates of the next node or voxel
    /// intersected by the ray. If Log2Dim = 0 the return value is the
    /// actual signed coordinate of the voxel, else it is the origin
    /// of the corresponding VDB tree node or tile.
    /// @note Incurs no computational overhead.
    __hostdev__ const CoordT& voxel() const { return mVoxel; }

    /// @brief Return the time (parameterized along the Ray) of the
    /// first hit of a tree node of size 2^Log2Dim.
    /// @details This value is initialized to startTime or ray.t0()
    /// depending on the constructor used.
    /// @note Incurs no computational overhead.
    __hostdev__ RealType time() const { return mT0; }

    /// @brief Return the maximum time (parameterized along the Ray).
    __hostdev__ RealType maxTime(const RayT& ray) const { return ray.t1(); }

    /// @brief Return the time (parameterized along the Ray) of the
    /// second (i.e. next) hit of a tree node of size 2^Log2Dim.
    /// @note Incurs a (small) computational overhead.
    __hostdev__ RealType next(const RayT& ray) const
    {
#if 1 //def __CUDA_ARCH__
        return fminf(ray.t1(), fminf(mNext[0], fminf(mNext[1], mNext[2])));
#else
        return std::min(ray.t1(), std::min(mNext[0], std::min(mNext[1], mNext[2])));
#endif
    }

private:
    // helper to implement the general form
    template<int axis>
    __hostdev__ bool step(const RayT& ray)
    {
#ifdef ENFORCE_FORWARD_STEPPING
        if (mNext[axis] <= mT0) {
            mNext[axis] += mT0 - 0.999999f * mNext[axis] + 1.0e-6f;
        }
#endif
        mT0 = mNext[axis];
        mNext[ axis] += mDim * fabsf(ray.invDir()[axis]);
        mVoxel[axis] += mDim * (ray.dir()[axis] > RealT(0) ? 1 : -1);
        return mT0 <= ray.t1();
    }

    int32_t mDim;
    RealT   mT0; // start time of the current span
    CoordT  mVoxel; // current voxel location
    Vec3T   mNext; // time at which the ray exits the current voxel along each axis
}; // class HDDA

/////////////////////////////////////////// ZeroCrossing ////////////////////////////////////////////

/// @brief returns true if the ray intersects a zero-crossing at the voxel level of the grid in the accessor
///        The empty-space ray-marching is performed at all levels of the tree using an
///        HDDA. If an intersection is detected, then ijk is updated with the index coordinate of the closest
///        voxel after the intersection point, v contains the grid values at ijk, and t is set to the time of
///        the intersection along the ray.
template<typename RayT, typename AccT>
inline __hostdev__ bool ZeroCrossing(RayT& ray, AccT& acc, Coord& ijk, typename AccT::ValueType& v, float& t)
{
    if (!ray.clip(acc.root().bbox()) || ray.t1() > 1e20)
        return false; // clip ray to bbox
    static const float Delta = 1.0001f;
    ijk = RoundDown<Coord>(ray.start()); // first hit of bbox
    HDDA<RayT, Coord> hdda(ray, acc.getDim(ijk, ray));
    const auto        v0 = acc.getValue(ijk);
    while (hdda.step(ray)) {
        ijk = RoundDown<Coord>(ray(hdda.time() + Delta));
        hdda.update(ray, acc.getDim(ijk, ray));
        if (hdda.dim() > 1 || !acc.isActive(ijk))
            continue; // either a tile value or an inactive voxel
        while (hdda.step(ray) && acc.isActive(hdda.voxel())) { // in the narrow band
            v = acc.getValue(hdda.voxel());
            if (v * v0 < 0) { // zero crossing
                ijk = hdda.voxel();
                t = hdda.time();
                return true;
            }
        }
    }
    return false;
}

/////////////////////////////////////////// DDA ////////////////////////////////////////////

/// @brief A Digital Differential Analyzer. Unlike HDDA (defined above) this DDA
///        uses a fixed step-size defined by the template parameter Dim!
///
/// @note The Ray template class is expected to have the following
/// methods: test(time), t0(), t1(), dir(), invDir(), and operator()(time).
/// See the example Ray class above for their definition.
///
/// @warning See @c HDDA above: the @c Ray must outlive the @c DDA and the
/// effective maximum time of the walk is always @c ray.t1().
template<typename RayT, typename CoordT = Coord, int Dim = 1>
class DDA
{
    static_assert(Dim >= 1, "Dim must be >= 1");

public:
    using RealType = typename RayT::RealType;
    using RealT = RealType;
    using Vec3Type = typename RayT::Vec3Type;
    using Vec3T = Vec3Type;
    using CoordType = CoordT;

    /// @brief Default ctor
    DDA() = default;

    /// @brief ctor from ray and dimension at which the DDA marches
    __hostdev__ DDA(const RayT& ray) { this->init(ray); }

    /// @brief Re-initializes the DDA
    /// @note See @c HDDA::init above: @a maxTime is unused by the lean DDA.
    __hostdev__ void init(const RayT& ray, RealT startTime, RealT maxTime)
    {
        assert(startTime <= maxTime);
        (void)maxTime;
        mT0 = startTime;
        const Vec3T &pos = ray(mT0), &dir = ray.dir(), &inv = ray.invDir();
        mVoxel = RoundDown<CoordT>(pos) & (~(Dim - 1));
        for (int axis = 0; axis < 3; ++axis) {
            if (dir[axis] == RealT(0)) { //handles dir = +/- 0
                mNext[axis] = Maximum<RealT>::value(); //i.e. disabled!
            } else if (inv[axis] > 0) {
                mNext[axis] = mT0 + (mVoxel[axis] + Dim - pos[axis]) * inv[axis];
            } else {
                mNext[axis] = mT0 + (mVoxel[axis] - pos[axis]) * inv[axis];
            }
        }
    }

    /// @brief Simular to init above except it uses the bounds of the input ray
    __hostdev__ void init(const RayT& ray) { this->init(ray, ray.t0(), ray.t1()); }

    /// @brief Increment the voxel index to next intersected voxel or node
    /// and returns true if the step in time does not exceed @c ray.t1().
    __hostdev__ bool step(const RayT& ray)
    {
        const int axis = MinIndex(mNext);
        switch (axis) {
        case 0:
            return step<0>(ray);
        case 1:
            return step<1>(ray);
        default:
            return step<2>(ray);
        }
    }

    /// @brief Return the index coordinates of the next node or voxel
    /// intersected by the ray. If Log2Dim = 0 the return value is the
    /// actual signed coordinate of the voxel, else it is the origin
    /// of the corresponding VDB tree node or tile.
    /// @note Incurs no computational overhead.
    __hostdev__ const CoordT& voxel() const { return mVoxel; }

    /// @brief Return the time (parameterized along the Ray) of the
    /// first hit of a tree node of size 2^Log2Dim.
    /// @details This value is initialized to startTime or ray.t0()
    /// depending on the constructor used.
    /// @note Incurs no computational overhead.
    __hostdev__ RealType time() const { return mT0; }

    /// @brief Return the maximum time (parameterized along the Ray).
    __hostdev__ RealType maxTime(const RayT& ray) const { return ray.t1(); }

    /// @brief Return the time (parameterized along the Ray) of the
    /// second (i.e. next) hit of a tree node of size 2^Log2Dim.
    /// @note Incurs a (small) computational overhead.
    __hostdev__ RealType next(const RayT& ray) const
    {
        return Min(ray.t1(), Min(mNext[0], Min(mNext[1], mNext[2])));
    }

    __hostdev__ int nextAxis() const
    {
        return nanovdb::math::MinIndex(mNext);
    }

private:
    // helper to implement the general form
    template<int axis>
    __hostdev__ bool step(const RayT& ray)
    {
#ifdef ENFORCE_FORWARD_STEPPING
        if (mNext[axis] <= mT0) {
            mNext[axis] += mT0 - 0.999999f * mNext[axis] + 1.0e-6f;
        }
#endif
        mT0 = mNext[axis];
        mNext[axis] += fabsf(ray.invDir()[axis]);
        mVoxel[axis] += Dim * (ray.dir()[axis] > RealT(0) ? 1 : -1);
        return mT0 <= ray.t1();
    }

    RealT  mT0; // start time of the current span
    CoordT mVoxel; // current voxel location
    Vec3T  mNext; // time at which the ray exits the current voxel along each axis
}; // class DDA

/////////////////////////////////////////// ZeroCrossingNode ////////////////////////////////////////////

template<typename RayT, typename NodeT>
inline __hostdev__ bool ZeroCrossingNode(RayT& ray, const NodeT& node, float v0, nanovdb::math::Coord& ijk, float& v, float& t)
{
    math::BBox<Coord> bbox(node.origin(), node.origin() + Coord(node.dim() - 1));

    if (!ray.clip(node.bbox())) {
        return false;
    }

    const float t0 = ray.t0();

    static const float Delta = 1.0001f;
    ijk = Coord::Floor(ray(ray.t0() + Delta));

    t = t0;
    v = 0;

    DDA<RayT, Coord, 1 << NodeT::LOG2DIM> dda(ray);
    while (dda.step(ray)) {
        ijk = dda.voxel();

        if (bbox.isInside(ijk) == false)
            return false;

        v = node.getValue(ijk);
        if (v * v0 < 0) {
            t = dda.time();
            return true;
        }
    }
    return false;
}

/////////////////////////////////////////// TreeMarcher ////////////////////////////////////////////

/// @brief returns true if the ray intersects an active value at any level of the grid in the accessor.
///        The empty-space ray-marching is performed at all levels of the tree using an
///        HDDA. If an intersection is detected, then ijk is updated with the index coordinate of the first
///        active voxel or tile, and t is set to the time of its intersection along the ray.
template<typename RayT, typename AccT>
inline __hostdev__ bool firstActive(RayT& ray, AccT& acc, Coord &ijk, float& t)
{
    if (!ray.clip(acc.root().bbox()) || ray.t1() > 1e20) {// clip ray to bbox
        return false;// missed or undefined bbox
    }
    static const float Delta = 1.0001f;// forward step-size along the ray to avoid getting stuck
    t = ray.t0();// initiate time
    ijk = RoundDown<Coord>(ray.start()); // first voxel inside bbox
    for (HDDA<RayT, Coord> hdda(ray, acc.getDim(ijk, ray)); !acc.isActive(ijk); hdda.update(ray, acc.getDim(ijk, ray))) {
        if (!hdda.step(ray)) return false;// leap-frog HDDA and exit if ray bound is exceeded
        t = hdda.time() + Delta;// update time
        ijk = RoundDown<Coord>( ray(t) );// update ijk
    }
    return true;
}

/////////////////////////////////////////// TreeMarcher ////////////////////////////////////////////

/// @brief A Tree Marcher for Generic Grids

template<typename NodeT, typename RayT, typename AccT, typename CoordT = Coord>
class TreeMarcher
{
public:
    using ChildT = typename NodeT::ChildNodeType;
    using RealType = typename RayT::RealType;
    using RealT = RealType;
    using CoordType = CoordT;

    inline __hostdev__ TreeMarcher(AccT& acc)
        : mAcc(acc)
    {
    }

    /// @brief Initialize the TreeMarcher with an index-space ray.
    inline __hostdev__ bool init(const RayT& indexRay)
    {
        mRay = indexRay;
        if (!mRay.clip(mAcc.root().bbox()))
            return false; // clip ray to bbox

        // tweak the intersection span into the bbox.
        // CAVEAT: this will potentially clip some tiny corner intersections.
        static const float Eps = 0.000001f;
        const float        t0 = mRay.t0() + Eps;
        const float        t1 = mRay.t1() - Eps;
        if (t0 > t1)
            return false;

        // Bake the trimmed span into mRay so the lean HDDA can derive
        // mT1/mDelta/mStep from the ray on every step() / next() call.
        mRay.setTimes(t0, t1);

        const CoordT      ijk = RoundDown<Coord>(mRay(t0));
        const uint32_t    dim = mAcc.getDim(ijk, mRay);
        mHdda.init(mRay, mRay.t0(), mRay.t1(), nanovdb::math::Max(dim, NodeT::dim()));

        mT0 = (dim <= ChildT::dim()) ? mHdda.time() : -1; // potentially begin a span.
        return true;
    }

    /// @brief step the ray through the tree. If the ray hits a node then
    /// populate t0 & t1, and the node.
    /// @return true when a node of type NodeT is intersected, false otherwise.
    inline __hostdev__ bool step(const NodeT** node, float& t0, float& t1)
    {
        // CAVEAT: if Delta is too large then it will clip corners of nodes in a visible way.
        // but it has to be quite large when very far from the grid (due to fp32 rounding)
        static const float Delta = 0.01f;
        bool               hddaIsValid;

        do {
            t0 = mT0;

            auto currentNode = mAcc.template getNode<NodeT>();

            // get next node intersection...
            hddaIsValid = mHdda.step(mRay);
            const CoordT nextIjk = RoundDown<Coord>(mRay(mHdda.time() + Delta));
            const auto   nextDim = mAcc.getDim(nextIjk, mRay);
            mHdda.update(mRay, (int)Max(nextDim, NodeT::dim()));
            mT0 = (nextDim <= ChildT::dim()) ? mHdda.time() : -1; // potentially begin a span.

            if (t0 >= 0) { // we are in a span.
                t1 = Min(mRay.t1(), mHdda.time());

                // TODO: clean this up!
                if (t0 >= t1 || currentNode == nullptr)
                    continue;

                *node = currentNode;
                return true;
            }

        } while (hddaIsValid);

        return false;
    }

    inline __hostdev__ const RayT& ray() const { return mRay; }

    inline __hostdev__ RayT& ray() { return mRay; }

private:
    AccT&             mAcc;
    RayT              mRay;
    HDDA<RayT, Coord> mHdda;
    float             mT0;
};// TreeMarcher

/////////////////////////////////////////// PointTreeMarcher ////////////////////////////////////////////

/// @brief A Tree Marcher for Point Grids
///
/// @note This class will handle correctly offseting the ray by 0.5 to ensure that
/// the underlying HDDA will intersect with the grid-cells. See details below.

template<typename AccT, typename RayT, typename CoordT = Coord>
class PointTreeMarcher : public TreeMarcher<LeafNode<typename AccT::ValueType>, RayT, AccT, CoordT>
{
    using BaseT = TreeMarcher<LeafNode<typename AccT::ValueType>, RayT, AccT, CoordT>;
public:
    __hostdev__ PointTreeMarcher(AccT& acc) : BaseT(acc) {}

    /// @brief Initiates this instance with a ray in index space.
    ///
    /// @details An offset by 0.5 is applied to the ray to account for the fact that points in vdb
    ///          grids are bucketed into so-called grid cell, which are centered round grid voxels,
    ///          whereas the DDA is based on so-called grid nodes, which are coincident with grid
    ///          voxels. So, rather than offsettting the points by 0.5 to bring them into a grid
    ///          node representation this method offsets the eye of the ray by 0.5, which effectively
    ///          ensures that the DDA operates on grid cells as oppose to grid nodes. This subtle
    ///          but important offset by 0.5 is explined in more details in our online documentation.
    __hostdev__ bool init(RayT ray) { return BaseT::init(ray.offsetEye(0.5)); }
};// PointTreeMarcher

} // namespace nanovdb::math

#endif // NANOVDB_HDDA_HAS_BEEN_INCLUDED
