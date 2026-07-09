#include "mas/ZmqTransport.hpp"
#include <stdexcept>

namespace mas {

ZmqPushSink::ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind,
                         int send_timeout_ms, int linger_ms)
    : sock_(ctx, zmq::socket_type::push) {
    if (send_timeout_ms >= 0) sock_.set(zmq::sockopt::sndtimeo, send_timeout_ms);
    if (linger_ms >= 0) {
        sock_.set(zmq::sockopt::linger, linger_ms);
    } else if (linger_ms == -2 && send_timeout_ms >= 0) {
        // Sentinel: historical coupling — linger follows the send timeout.
        sock_.set(zmq::sockopt::linger, send_timeout_ms);
    }
    // linger_ms == -1 (or sentinel with send_timeout_ms < 0): zmq default,
    // infinite linger.
    if (bind) sock_.bind(endpoint); else sock_.connect(endpoint);
}

void ZmqPushSink::send(const Message& m) {
    const auto sent = sock_.send(zmq::buffer(m.payload), zmq::send_flags::none);
    if (!sent.has_value()) {
        // EAGAIN: only reachable when send_timeout_ms >= 0 was set and the
        // socket is mute (no connected peer) or the peer isn't draining.
        throw std::runtime_error(
            "ZmqPushSink: send timed out (no connected peer or peer not draining)");
    }
}

ZmqPullSource::ZmqPullSource(zmq::context_t& ctx, const std::string& endpoint,
                             bool bind, int timeout_ms)
    : sock_(ctx, zmq::socket_type::pull) {
    sock_.set(zmq::sockopt::rcvtimeo, timeout_ms);
    if (bind) sock_.bind(endpoint); else sock_.connect(endpoint);
}

std::optional<Message> ZmqPullSource::recv() {
    zmq::message_t msg;
    const auto n = sock_.recv(msg, zmq::recv_flags::none);
    if (!n.has_value()) return std::nullopt;   // rcvtimeo expired
    return Message{msg.to_string()};
}

} // namespace mas
