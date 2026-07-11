#pragma once
#include "mas/agent/Message.hpp"
#include "mas/transport/Transport.hpp"
#include <chrono>
#include <functional>
#include <vector>

namespace mas {

struct DispatchSummary {
    long long total_events = 0;
    int files_ok = 0;      // results with events >= 0
    int files_failed = 0;  // events < 0, re-dispatch cap exceeded, or abort leftovers
    int workers_died = 0;  // workers tombstoned by the deadline sweep
};

struct CoordinatorConfig {
    // A worker silent longer than this is declared dead (resilience spec §6).
    std::chrono::milliseconds death_threshold{30000};
    // Re-sends allowed per item beyond its first dispatch; exceeding it marks
    // the item permanently failed (poison-item protection).
    int redispatch_cap = 2;
    // Initial dispatch waits until this many distinct workers have registered
    // (hello heartbeat or result). 0 = dispatch immediately (previous
    // behavior; ZMQ PUSH then queues everything into the first connected
    // pipe — fine for 1 worker or tests, wrong for measuring N-worker
    // scaling: the slow-joiner capture found by the Plan 5 bench sweep).
    int expected_workers = 0;
    // Give up waiting for registrations after this long: proceed degraded
    // if at least one worker showed up, abort the run if none did.
    std::chrono::milliseconds registration_timeout{10000};
};

// Injectable time source so unit tests drive deadlines without sleeping.
using ClockFn = std::function<std::chrono::steady_clock::time_point()>;

// Ventilator + sink + liveness monitor in one call (resilience spec §6): with
// cfg.expected_workers > 0, first gate the initial dispatch on that many
// workers registering (hello heartbeat or result) — avoids ZMQ PUSH's
// slow-joiner capture, where sending before any connect lands queues the
// whole batch into the first pipe (Plan 5 bench sweep) — giving up after
// registration_timeout (degraded start if some registered, abort if none).
// Then: PUSH every item, then tick: drain heartbeats, take one result per
// tick (the results source's recv timeout paces the loop), sweep deadlines
// and re-dispatch a dead worker's open items and completions, until every
// item is settled. Ends by PUSHing one STOP per live registry entry. The
// dead worker's store file is written off — its rows are recreated in
// survivor stores and the idempotent upsert absorbs any overlap.
DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                IMessageSource& heartbeats,
                                const CoordinatorConfig& cfg, ClockFn now);

} // namespace mas
