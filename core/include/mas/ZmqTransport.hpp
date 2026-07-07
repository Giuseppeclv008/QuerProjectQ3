#pragma once
#include "mas/Transport.hpp"
#include <string>
#include <zmq.hpp>

namespace mas {

// ZeroMQ adapters. DIP boundary (spec §7): only this header, its .cpp and
// the agent mains may include zmq.hpp; domain code sees just the interfaces.

class ZmqPushSink : public IMessageSink {
public:
    ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind);
    void send(const Message& m) override;

private:
    zmq::socket_t sock_;
};

class ZmqPullSource : public IMessageSource {
public:
    // timeout_ms < 0 blocks forever; otherwise recv() yields nullopt after
    // timeout_ms of silence.
    ZmqPullSource(zmq::context_t& ctx, const std::string& endpoint, bool bind,
                  int timeout_ms = -1);
    std::optional<Message> recv() override;

private:
    zmq::socket_t sock_;
};

} // namespace mas
