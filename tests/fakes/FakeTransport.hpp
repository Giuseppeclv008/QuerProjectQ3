#pragma once
#include "mas/transport/Transport.hpp"
#include <cstddef>
#include <deque>
#include <stdexcept>
#include <utility>
#include <vector>

namespace mas::test {

// Scripted source: hands out queued messages in order, then reports a
// closed source (nullopt) forever.
struct FakeSource : IMessageSource {
    std::deque<Message> queue;
    std::optional<Message> recv() override {
        if (queue.empty()) return std::nullopt;
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
    std::optional<Message> recv() override {
        if (script.empty()) return std::nullopt;
        std::optional<Message> m = std::move(script.front());
        script.pop_front();
        return m;
    }
};

} // namespace mas::test
