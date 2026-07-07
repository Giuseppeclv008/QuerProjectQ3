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

} // namespace mas::test
