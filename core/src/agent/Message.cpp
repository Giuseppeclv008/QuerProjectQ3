#include "mas/agent/Message.hpp"
#include <exception>
#include <vector>

namespace mas {
namespace {

constexpr const char* kWorkTag = "WORK";
constexpr const char* kResultTag = "RESULT";
constexpr const char* kStopTag = "STOP";
constexpr const char* kHeartbeatTag = "HB";

std::vector<std::string> split_lines(const std::string& s) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (true) {
        const auto nl = s.find('\n', start);
        if (nl == std::string::npos) {
            out.push_back(s.substr(start));
            return out;
        }
        out.push_back(s.substr(start, nl - start));
        start = nl + 1;
    }
}

} // namespace

Message encode(const WorkItem& w) {
    return {std::string(kWorkTag) + "\n" + w.in_path};
}

Message encode(const WorkResult& r) {
    return {std::string(kResultTag) + "\n" + r.in_path + "\n" +
            std::to_string(r.events) + "\n" + std::to_string(r.seconds) +
            "\n" + r.worker_id};
}

std::optional<WorkItem> decode_work(const Message& m) {
    const auto f = split_lines(m.payload);
    if (f.size() != 2 || f[0] != kWorkTag || f[1].empty()) return std::nullopt;
    return WorkItem{f[1]};
}

std::optional<WorkResult> decode_result(const Message& m) {
    const auto f = split_lines(m.payload);
    if (f.size() != 5 || f[0] != kResultTag || f[4].empty()) return std::nullopt;
    try {
        std::size_t events_end = 0;
        std::size_t seconds_end = 0;
        const long long events = std::stoll(f[2], &events_end);
        const double seconds = std::stod(f[3], &seconds_end);
        // Wire bytes are untrusted: a field with trailing garbage ("5x")
        // still stoll/stod-parses its prefix, so require full consumption.
        if (events_end != f[2].size() || seconds_end != f[3].size()) {
            return std::nullopt;
        }
        return WorkResult{f[1], events, seconds, f[4]};
    } catch (const std::exception&) {
        return std::nullopt;   // non-numeric events/seconds
    }
}

Message encode(const Heartbeat& h) {
    return {std::string(kHeartbeatTag) + "\n" + h.worker_id + "\n" +
            std::to_string(h.seq)};
}

std::optional<Heartbeat> decode_heartbeat(const Message& m) {
    const auto f = split_lines(m.payload);
    if (f.size() != 3 || f[0] != kHeartbeatTag || f[1].empty()) return std::nullopt;
    try {
        std::size_t seq_end = 0;
        const long long seq = std::stoll(f[2], &seq_end);
        if (seq_end != f[2].size() || seq < 0) return std::nullopt;
        return Heartbeat{f[1], seq};
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

Message make_stop() { return {kStopTag}; }

bool is_stop(const Message& m) { return m.payload == kStopTag; }

} // namespace mas
