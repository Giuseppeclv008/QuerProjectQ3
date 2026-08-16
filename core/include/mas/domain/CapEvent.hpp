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
// `% 2 == 1` would be wrong for a negative status: C++ truncates toward zero,
// so -65 % 2 is -1 and a rejected closure would read as clean. The PLC has never
// emitted a negative status in this pool, but the guard is free and the failure
// mode is silent.
inline bool is_reject(double status) {
    // static_cast<long long> of a non-finite or out-of-range double is UB,
    // and both are reachable: std::stod happily returns 1e300, inf, or nan
    // for a Status cell. A status that cannot be the PLC's bitmask is not
    // evidence of a clean closure -- unknown reads as reject, never as OK.
    if (!(status >= -9007199254740992.0 && status <= 9007199254740992.0))
        return true;   // NaN fails both comparisons and lands here too
    return (static_cast<long long>(status) % 2) != 0;
}

// A Count cell is usable only where llround is defined and the value can act
// as a counter: finite and within ±2^53 (the integer-exact double range).
// stod accepts "1e18", "inf" and "nan", and llround on those is unspecified;
// worse, a cast-truncated count fabricated events -- counts 100 -> 5e9 once
// emitted delta=705032604, and a truncation landing at <= 1 also cleared
// `aggregated`, an event claiming no caps elapsed. Both loaders skip rows
// that fail this, the same treatment as any other malformed cell.
inline bool is_valid_count(double c) {
    return c >= -9007199254740992.0 && c <= 9007199254740992.0;
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
