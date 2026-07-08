#pragma once
#include <optional>
#include <string>

namespace mas {

// One transport frame. Payload is a tag line plus '\n'-separated fields;
// day-file paths therefore must not contain newlines.
struct Message {
    std::string payload;
};

// Ventilator -> worker: process one raw day-file (spec §8 unit of work).
// The worker's store owns machine identity, so the item is just the path.
struct WorkItem {
    std::string in_path;
};

// Worker -> sink: outcome of one WorkItem. events == -1 => input unreadable
// (mirrors clean_file's contract). worker_id attributes the result to the
// worker that produced it, so the coordinator can write a dead worker's
// store off and re-dispatch its completions (resilience spec §5/§6).
struct WorkResult {
    std::string in_path;
    long long events = 0;
    double seconds = 0.0;
    std::string worker_id;
};

// Worker -> coordinator liveness beacon (resilience spec §5). seq increases
// monotonically per worker; the coordinator only uses arrival time.
struct Heartbeat {
    std::string worker_id;
    long long seq = 0;
};

Message encode(const WorkItem& w);
Message encode(const WorkResult& r);
Message encode(const Heartbeat& h);
std::optional<WorkItem> decode_work(const Message& m);
std::optional<WorkResult> decode_result(const Message& m);
std::optional<Heartbeat> decode_heartbeat(const Message& m);
Message make_stop();
bool is_stop(const Message& m);

} // namespace mas
