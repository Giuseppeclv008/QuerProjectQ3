#include "mas/agent/CleaningWorker.hpp"
#include "mas/agent/Message.hpp"
#include <chrono>
#include <utility>

namespace mas {

namespace {

// Beats while a file is being cleaned.
//
// The coordinator declares a worker dead after 30 s of silence, and the worker
// used to send nothing between picking up an item and finishing it. On this pool
// a day-file takes ~3 s, so the margin was 10x -- but it is a function of the
// input, not a constant: a bigger file, a slow disk, or a loaded VM tombstones a
// worker that is alive and working. Its results are then dropped, its item is
// re-dispatched for nothing, and since the final STOP goes only to live workers
// it lingers for the whole idle-exit budget.
//
// Decorating the store rather than spawning a heartbeat thread is deliberate:
// ZeroMQ sockets are not thread-safe, so beating from a second thread on the
// same PUSH socket would be a data race. clean_file() writes every 8,192 events,
// which on a real day-file is roughly every 30 ms.
//
// Residual window: a file that reads for a long time while emitting almost no
// events -- a machine idle for the whole period -- still writes once, at the end.
// Bounding that needs a progress hook on the reader, which would change CleanFn's
// signature and every call site. Out of scope here, and the 30 s threshold covers
// it by a wide margin at any plausible file size.
class BeatingStore : public IEventStore {
public:
    BeatingStore(IEventStore& inner, std::function<void()> beat,
                 std::chrono::milliseconds every)
        : inner_(inner), beat_(std::move(beat)), every_(every),
          last_(std::chrono::steady_clock::now()) {}

    void write(std::span<const CapEvent> events) override {
        inner_.write(events);
        const auto now = std::chrono::steady_clock::now();
        if (now - last_ >= every_) {
            last_ = now;
            beat_();
        }
    }

private:
    IEventStore& inner_;
    std::function<void()> beat_;
    std::chrono::milliseconds every_;
    std::chrono::steady_clock::time_point last_;
};

} // namespace

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
        const long long events = clean_fn_(item->in_path, beating);
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
