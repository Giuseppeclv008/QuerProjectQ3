#pragma once
#include "mas/Transport.hpp"
#include <deque>
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
