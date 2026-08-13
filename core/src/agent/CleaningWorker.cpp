#include "mas/agent/CleaningWorker.hpp"
#include "mas/agent/Message.hpp"
#include "mas/store/BeatingStore.hpp"
#include <chrono>
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
    beat();   // hello: register with the coordinator promptly
    while (idle_ticks < kIdleExitTicks) {
        const auto msg = work_.recv();
        if (!msg) {   // empty tick: recv timed out (or the source closed)
            ++idle_ticks;
            beat();
            continue;
        }
        idle_ticks = 0;   // any frame proves the coordinator is alive
        if (is_stop(*msg)) break;
        const auto item = decode_work(*msg);
        if (!item) continue;   // malformed frame: drop it, keep serving
        const auto t0 = std::chrono::steady_clock::now();
        BeatingStore beating(store_, [this] { beat(); }, kBeatEvery);
        const long long events = clean_fn_(item->in_path, beating,
                                           [this] { beat(); });
        const std::chrono::duration<double> dt =
            std::chrono::steady_clock::now() - t0;
        results_.send(
            encode(WorkResult{item->in_path, events, dt.count(), worker_id_}));
        beat();
        ++handled;
    }
    return handled;
}

} // namespace mas
