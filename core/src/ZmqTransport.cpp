#include "mas/ZmqTransport.hpp"

namespace mas {

ZmqPushSink::ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind)
    : sock_(ctx, zmq::socket_type::push) {
    if (bind) sock_.bind(endpoint); else sock_.connect(endpoint);
}

void ZmqPushSink::send(const Message& m) {
    sock_.send(zmq::buffer(m.payload), zmq::send_flags::none);
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
