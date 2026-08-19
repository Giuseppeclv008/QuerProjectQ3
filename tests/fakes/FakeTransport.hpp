#pragma once
#include "mas/transport/Transport.hpp"
#include <cstddef>
#include <deque>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace mas::test {


// A drained scripted source cannot end a run on its own, and that is a hazard
// rather than a detail. Every one of these fakes reports nullopt forever once
// its script runs out, and most coordinator tests drive a frozen clock, so a
// regression that leaves an item unsettled makes `while (open > 0)` spin on an
// empty source with no deadline that can ever elapse. gtest has no per-test
// timeout, so the suite hangs and CI reports nothing at all -- which is worse
// than a red test, because it looks like infrastructure trouble.
//
// The watchdog is deliberately clock-free: counting drained reads works the
// same for a frozen clock, a scripted one and a real one, and unlike advancing
// virtual time it cannot move a boundary test's deadline underneath it. The
// limit is far above any real test (the longest scripted run here settles in
// well under a hundred passes) and far below a wall-clock hang.
struct DrainedSourceWatchdog {
    std::size_t drained_reads = 0;
    static constexpr std::size_t kLimit = 10000;
    void tick(const char* which) {
        if (++drained_reads > kLimit)
            throw std::runtime_error(
                std::string(which) + " was read " +
                std::to_string(drained_reads) +
                " times after its script drained: the run is not settling. "
                "A re-dispatch or settle path is missing, and without this the "
                "test would hang instead of failing.");
    }
};

// Scripted source: hands out queued messages in order, then reports a
// closed source (nullopt) forever -- bounded by the watchdog above.
struct FakeSource : IMessageSource {
    std::deque<Message> queue;
    DrainedSourceWatchdog watchdog;
    std::optional<Message> recv() override {
        if (queue.empty()) {
            watchdog.tick("FakeSource");
            return std::nullopt;
        }
        Message m = std::move(queue.front());
        queue.pop_front();
        return m;
    }
};

struct FakeSink : IMessageSink {
    std::vector<Message> sent;
    void send(const Message& m) override { sent.push_back(m); }
};

// A sink that fails the way the real one does. FakeSink cannot: its send is a
// push_back, so every coordinator and worker test ran on a transport with no
// failure mode at all. ZmqPushSink::send throws std::runtime_error when a mute
// socket hits its send timeout -- pinned in test_zmq_transport.cpp -- and the
// coordinator sends on six paths, so that branch was unreachable from any test
// until this existed. `fail_after` sends land in `sent`; the next one throws.
struct ThrowingSink : IMessageSink {
    std::vector<Message> sent;
    std::size_t fail_after = 0;
    void send(const Message& m) override {
        if (sent.size() >= fail_after)
            throw std::runtime_error(
                "send timed out after 60000 ms: no peer drained the socket");
        sent.push_back(m);
    }
};

// Scripted source that can interleave empty ticks: a nullopt entry models a
// recv timeout (production ZmqPullSource with a finite timeout). After the
// script drains it reports nullopt forever, like FakeSource.
struct FakeTickSource : IMessageSource {
    std::deque<std::optional<Message>> script;
    DrainedSourceWatchdog watchdog;
    std::optional<Message> recv() override {
        if (script.empty()) {
            watchdog.tick("FakeTickSource");
            return std::nullopt;
        }
        std::optional<Message> m = std::move(script.front());
        script.pop_front();
        return m;
    }
};

} // namespace mas::test
