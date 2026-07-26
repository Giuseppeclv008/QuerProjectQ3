#pragma once
#include <array>
#include <string>

namespace mas {

inline constexpr int NUM_HEADS = 36;

// AROL Equatorque closure status, per the brief's slide-6 table: a bitmask, not
// an enum. Bit 0 is the reject signal; bits 1..6 are the error conditions
// (No Load, No Closure, No InTorque, No CapTurns, Following Error, Bad Closure).
// The table's 14 rows are those 6 conditions plus "Closure OK", each with and
// without the reject bit.
//
// Measured over 2026-02-01 (765,711 closures), and confirmed across the full
// three-month store:
//   status 0,  torque > 0   -> real capping operation, with load
//   status 2,  torque == 0  -> "No Load" cycle: counter advances, no cap applied
//   status 65, torque > 0   -> Bad Closure, rejected
//   status 9,  torque > 0   -> No InTorque, rejected
//   status 4,  torque >= 0  -> No Closure, not rejected
inline bool is_reject(double status) {
    return (static_cast<long long>(status) % 2) == 1;
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
