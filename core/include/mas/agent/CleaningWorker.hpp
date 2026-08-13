#pragma once
#include "mas/store/EventStore.hpp"
#include "mas/transport/Transport.hpp"
#include <chrono>
#include <functional>
#include <string>

namespace mas {

// Cleaning agent loop (spec §5.3 + resilience spec §5/§7): PULL work items,
// clean each day-file into the injected store, PUSH one WorkResult per item,
// and PUSH heartbeats so the coordinator can tell death from silence.
// Liveness contract: one heartbeat at run() entry, one per empty recv tick
// (production wires a 1 s recv timeout), one after each result, and one at most
// every kBeatEvery while a file is being cleaned. The coordinator's death
// threshold is 30 s, so a worker must never fall silent that long while it is in
// fact working.
class CleaningWorker {
public:
    // The beat callback is passed so a clean_fn that supplies its own store --
    // the parquet path does -- can decorate it too. Without it that path is
    // silent for the whole file and the coordinator tombstones a live worker.
    using CleanFn = std::function<long long(const std::string&, IEventStore&,
                                            const std::function<void()>&)>;

    // Minimum spacing between in-progress heartbeats. Far under the
    // coordinator's 30 s death threshold, and coarse enough that a day-file's
    // ~93 store writes do not become 93 frames.
    static constexpr std::chrono::milliseconds kBeatEvery{1000};

    // Consecutive empty ticks before run() gives up on the coordinator and
    // returns (~60 s at the production 1 s tick). Tick counting instead of a
    // clock keeps this deterministic under test fakes.
    static constexpr int kIdleExitTicks = 60;

    CleaningWorker(IMessageSource& work, IMessageSink& results,
                   IMessageSink& heartbeats, IEventStore& store,
                   std::string worker_id, CleanFn clean_fn);

    // Blocks; returns the number of work items handled (failures included —
    // their WorkResult carries events == -1). Exits on STOP or after
    // kIdleExitTicks consecutive empty ticks.
    int run();

private:
    void beat();

    IMessageSource& work_;
    IMessageSink& results_;
    IMessageSink& heartbeats_;
    IEventStore& store_;
    std::string worker_id_;
    CleanFn clean_fn_;
    long long hb_seq_ = 0;
};

} // namespace mas
