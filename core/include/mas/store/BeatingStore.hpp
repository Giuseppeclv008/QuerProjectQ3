#pragma once
#include "mas/store/EventStore.hpp"
#include <chrono>
#include <functional>
#include <span>
#include <utility>

namespace mas {

// Beats while a file is being cleaned. Decorating the store rather than
// spawning a thread is deliberate: ZeroMQ sockets are not thread-safe, so
// beating from a second thread on the same PUSH socket would be a data race.
// clean_file() writes every 8,192 events, which on a real day-file is roughly
// every 30 ms.
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

} // namespace mas
