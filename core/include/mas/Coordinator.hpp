#pragma once
#include "mas/Message.hpp"
#include "mas/Transport.hpp"
#include <vector>

namespace mas {

struct DispatchSummary {
    long long total_events = 0;
    int files_ok = 0;      // results with events >= 0
    int files_failed = 0;  // events < 0, malformed results, or never reported
};

// Ventilator + sink in one call (spec §8): PUSH every item, PULL one result
// per item, then PUSH one STOP per worker so the pool shuts down. If the
// result source reports no more messages (timeout/closed) the remaining
// unreported items count as failed — the caller decides what to do; work
// re-dispatch on missed heartbeats is a later plan (spec §10).
DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                int num_workers);

} // namespace mas
