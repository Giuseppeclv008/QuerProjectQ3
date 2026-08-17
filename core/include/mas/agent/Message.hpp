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
// (mirrors clean_file's contract), deterministic, not worth re-dispatching.
// events == -2 => the store failed part-way with rows already written: the
// item is retryable, and re-running it is safe because the upsert is
// idempotent on (machine_id, head_id, ts). Reporting both as -1 made the run
// summary say "0 events" for files whose rows were sitting in the store.
// worker_id attributes the result to the
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

// Worker -> coordinator, on the results socket, the moment a WorkItem is
// dequeued: "I hold this item". The work socket is anonymous PUSH round-robin,
// so this is the only way the coordinator learns who holds what -- and without
// it, any worker death forced a re-dispatch of *every* open item, with the
// per-item re-dispatch cap consumed by deaths that had nothing to do with the
// item. Sharing the results socket makes claim-before-result ordering a
// per-pipe FIFO guarantee rather than a race.
struct WorkClaim {
    std::string in_path;
    std::string worker_id;
};

// Worker -> coordinator, on the results socket: an announced, voluntary exit
// (idle budget exhausted). Distinct from death on purpose: a departed worker's
// store is intact, so its completed results stay counted, nothing is reopened,
// and nothing is re-dispatched. Without this frame an ordinary idle-exit is
// indistinguishable from a crash, and three ordinary exits failed whole runs.
struct Goodbye {
    std::string worker_id;
};

Message encode(const WorkItem& w);
Message encode(const WorkResult& r);
Message encode(const Heartbeat& h);
Message encode(const WorkClaim& c);
Message encode(const Goodbye& g);
std::optional<WorkItem> decode_work(const Message& m);
std::optional<WorkResult> decode_result(const Message& m);
std::optional<Heartbeat> decode_heartbeat(const Message& m);
std::optional<WorkClaim> decode_claim(const Message& m);
std::optional<Goodbye> decode_goodbye(const Message& m);
Message make_stop();
bool is_stop(const Message& m);

} // namespace mas
