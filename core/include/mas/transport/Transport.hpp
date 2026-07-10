#pragma once
#include "mas/agent/Message.hpp"
#include <optional>

namespace mas {

// Transport seam (spec §7; ISP: recv and send split by socket role).
// recv() returning nullopt means "no message": closed/drained source or
// a timed-out poll — either way the caller stops waiting.
struct IMessageSource {
    virtual std::optional<Message> recv() = 0;
    virtual ~IMessageSource() = default;
};

struct IMessageSink {
    virtual void send(const Message& m) = 0;
    virtual ~IMessageSink() = default;
};

} // namespace mas
