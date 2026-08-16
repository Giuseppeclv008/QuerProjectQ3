#include "mas/agent/CleaningWorker.hpp"
#include "mas/agent/Message.hpp"
#include "mas/store/BeatingStore.hpp"
#include <chrono>
#include <exception>
#include <iostream>
#include <utility>

namespace mas {

CleaningWorker::CleaningWorker(IMessageSource& work, IMessageSink& results,
                               IMessageSink& heartbeats, IEventStore& store,
                               std::string worker_id, CleanFn clean_fn)
    : work_(work), results_(results), heartbeats_(heartbeats), store_(store),
      worker_id_(std::move(worker_id)), clean_fn_(std::move(clean_fn)) {}

void CleaningWorker::beat() {
    heartbeats_.send(encode(Heartbeat{worker_id_, hb_seq_++}));
}

int CleaningWorker::run() {
    int handled = 0;
    int idle_ticks = 0;
    bool stopped = false;
    beat();   // hello: register with the coordinator promptly
    while (idle_ticks < kIdleExitTicks) {
        const auto msg = work_.recv();
        if (!msg) {   // empty tick: recv timed out (or the source closed)
            ++idle_ticks;
            beat();
            continue;
        }
        idle_ticks = 0;   // any frame proves the coordinator is alive
        if (is_stop(*msg)) {
            stopped = true;
            break;
        }
        const auto item = decode_work(*msg);
        if (!item) continue;   // malformed frame: drop it, keep serving
        // Claim before cleaning, on the results socket: the same pipe carries
        // the eventual result, so the coordinator is guaranteed to learn who
        // holds the item before it learns the outcome. This is what lets a
        // death re-dispatch only the dead worker's items.
        results_.send(encode(WorkClaim{item->in_path, worker_id_}));
        const auto t0 = std::chrono::steady_clock::now();
        BeatingStore beating(store_, [this] { beat(); }, kBeatEvery);
        // A throw is one bad item, not a dead worker. Unwinding past main here
        // sent NO result: the coordinator burned the full death threshold,
        // tombstoned the worker, wrote its store off, and re-dispatched
        // everything it held. events == -1 is the failure channel built for
        // exactly this; route into it and keep serving.
        long long events = -1;
        try {
            events = clean_fn_(item->in_path, beating, [this] { beat(); });
        } catch (const std::exception& e) {
            std::cerr << "worker " << worker_id_ << ": " << item->in_path
                      << " failed: " << e.what() << "\n";
        }
        const std::chrono::duration<double> dt =
            std::chrono::steady_clock::now() - t0;
        results_.send(
            encode(WorkResult{item->in_path, events, dt.count(), worker_id_}));
        beat();
        ++handled;
    }
    // An idle exit is voluntary; say so. Without the goodbye the coordinator
    // cannot tell this from a crash, and treated it as one: completions
    // reopened, items re-dispatched, budgets spent. After a STOP the
    // coordinator is already shutting down and reads nothing further.
    if (!stopped) results_.send(encode(Goodbye{worker_id_}));
    return handled;
}

} // namespace mas
