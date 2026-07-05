#pragma once
#include <array>
#include <string>

namespace mas {

inline constexpr int NUM_HEADS = 36;

// AROL Equatorque status codes: 0 = idle/held, 2 = OK cap, 65 = fault (rare).
// Verify the fault set against real data (spec §14, Open Question 1).
inline bool is_fault_status(double status) {
    return status == 65.0;
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
