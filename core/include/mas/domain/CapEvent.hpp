#pragma once
#include <array>
#include <string>

namespace mas {

inline constexpr int NUM_HEADS = 36;

// AROL Equatorque status codes, measured at closure over 2026-02-01 (spec §3.1).
// The joint (status, torque) distribution of 765,711 closures separates cleanly:
//
//   status 0,  torque > 0   -> 427,643/day  real capping operation, with load
//   status 2,  torque == 0  -> 337,772/day  "No Load" cycle: the counter advances
//                                           but no cap is applied (the idle signal)
//   status 65, torque > 0   ->       4/day  fault
//
// An earlier version of this comment had 0 and 2 backwards. It cannot be right:
// status 0 carries ~2.0 Nm on 427k closures a day, so it is not "idle", and
// status 2 carries zero torque on 337k, so it is not an "OK cap".
inline bool is_fault_status(double status) {
    return status == 65.0;
}

// A capping operation is a closure WITH load. No-load cycles are excluded from
// every success denominator (spec §3.2).
inline bool is_successful_cap(double status, double torque) {
    return status == 0.0 && torque > 0.0;
}

inline bool is_no_load(double status, double torque) {
    return status == 2.0 && torque == 0.0;
}

// One raw 1 Hz poll: timestamp + per-head Count / AppTorque / Status.
struct RawRow {
    std::string ts;
    std::array<double, NUM_HEADS> count{};
    std::array<double, NUM_HEADS> torque{};
    std::array<double, NUM_HEADS> status{};
};

// One real cap event (or a reset marker) emitted by the extractor.
struct CapEvent {
    int head_id = 0;          // 1..36
    std::string ts;
    long long cap_seq = 0;    // head Count at this cap
    double app_torque = 0.0;
    double status = 0.0;
    int delta = 0;            // caps since last observation; >1 => aggregated
    bool is_fault = false;
    bool aggregated = false;
    bool reset = false;       // true => counter-reset marker
};

} // namespace mas
