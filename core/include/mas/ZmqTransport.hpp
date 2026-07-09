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
    // (no connected peer draining it). send_timeout_ms >= 0: set ZMQ_SNDTIMEO
    // to that value; a send() that hits the timeout (EAGAIN: no connected
    // peer, or a peer that isn't draining) throws std::runtime_error instead
    // of blocking forever.
    //
    // linger_ms == -2 (default, sentinel): historical coupling — ZMQ_LINGER
    // follows send_timeout_ms when that is >= 0, else stays at the zmq
    // default (infinite). Existing behavior, unchanged for callers that do
    // not pass linger_ms.
    // linger_ms >= 0: set ZMQ_LINGER to exactly this value, independent of
    // send_timeout_ms. Use 0 for fire-and-forget sinks whose queued messages
    // must not delay process teardown: a CONNECT-mode PUSH creates its pipe
    // immediately and queues sends below HWM even if the peer never appears
    // (SNDTIMEO cannot fire), and zmq_ctx_term then waits out ZMQ_LINGER on
    // the undeliverable backlog (chaos E2E: the orphan worker outlived its
    // ~65 s exit budget, 61 s idle exit + 60 s linger = 121 s).
    // linger_ms == -1: explicit infinite linger, regardless of
    // send_timeout_ms.
    // All options are applied before bind/connect (mirroring how
    // ZmqPullSource sets rcvtimeo below).
    ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind,
                int send_timeout_ms = -1, int linger_ms = -2);
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
