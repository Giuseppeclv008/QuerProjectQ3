#pragma once
#include "mas/Transport.hpp"
#include <string>
#include <zmq.hpp>

namespace mas {

// ZeroMQ adapters. DIP boundary (spec §7): only this header, its .cpp and
// the agent mains may include zmq.hpp; domain code sees just the interfaces.

class ZmqPushSink : public IMessageSink {
public:
    // send_timeout_ms < 0 (default): block forever on a full/mute socket
    // (no connected peer draining it) and leave ZMQ_LINGER at its default
    // (infinite) - existing behavior, unchanged for all current callers.
    // send_timeout_ms >= 0: set both ZMQ_SNDTIMEO and ZMQ_LINGER to that
    // value, applied before bind/connect (mirroring how ZmqPullSource sets
    // rcvtimeo below). A send() that hits the timeout (EAGAIN: no connected
    // peer, or a peer that isn't draining) throws std::runtime_error instead
    // of blocking forever.
    ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind,
                int send_timeout_ms = -1);
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
