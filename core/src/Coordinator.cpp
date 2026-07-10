#include "mas/Coordinator.hpp"
#include <iostream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace mas {
namespace {

struct ItemState {
    int redispatches = 0;   // sends beyond the first
    bool done = false;      // ok, failed, or permanently failed
};

struct WorkerState {
    std::chrono::steady_clock::time_point last_seen{};
    bool alive = true;
    // (in_path, events) of this worker's ok results: reopened if it dies,
    // because its store file is written off (resilience spec §3/§6).
    std::vector<std::pair<std::string, long long>> completed;
};

} // namespace

DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                IMessageSource& heartbeats,
                                const CoordinatorConfig& cfg, ClockFn now) {
    DispatchSummary s;
    std::unordered_map<std::string, ItemState> state;   // in_path -> state
    for (const auto& item : items) state.emplace(item.in_path, ItemState{});
    std::size_t open = state.size();   // duplicate paths collapse by contract
    for (const auto& item : items) work.send(encode(item));

    std::unordered_map<std::string, WorkerState> registry;
    const auto start = now();

    const auto touch = [&](const std::string& worker_id) -> WorkerState* {
        auto [it, inserted] = registry.try_emplace(worker_id);
        if (inserted) {
            it->second.last_seen = now();
            std::cerr << "coordinator: worker " << worker_id << " joined\n";
        } else if (!it->second.alive) {
            return nullptr;   // tombstoned: dead is dead (spec §8)
        } else {
            it->second.last_seen = now();
        }
        return &it->second;
    };

    // Loop-pass order is load-bearing for deterministic tests and pinned
    // here: (1) one results tick — its recv timeout paces the loop and, under
    // test fakes, advances virtual time; (2) heartbeat drain — refreshes are
    // stamped with now() AFTER the tick, so a beat delivered this pass
    // survives the deadline this pass; (3) deadline sweep; (4) aborts.
    while (open > 0) {
        // 1) One result tick (production recv timeout 200 ms paces the loop).
        if (const auto msg = results.recv()) {
            const auto r = decode_result(*msg);
            if (!r) {
                std::cerr << "coordinator: dropped malformed result\n";
            } else if (WorkerState* w = touch(r->worker_id); !w) {
                // Late result from a tombstoned worker: its store is written
                // off, so counting this would credit rows we may never merge.
                std::cerr << "coordinator: dropped result for " << r->in_path
                          << " from dead worker " << r->worker_id << "\n";
            } else {
                const auto st = state.find(r->in_path);
                if (st == state.end() || st->second.done) {
                    std::cerr << "coordinator: dropped duplicate/unknown result for "
                              << r->in_path << "\n";
                } else {
                    st->second.done = true;
                    --open;
                    if (r->events >= 0) {
                        ++s.files_ok;
                        s.total_events += r->events;
                        w->completed.emplace_back(r->in_path, r->events);
                    } else {
                        ++s.files_failed;   // unreadable input: deterministic,
                                            // re-dispatch would not help
                    }
                }
            }
        }

        // 2) Heartbeats: drain without blocking (production recv timeout 0).
        while (auto hb_msg = heartbeats.recv()) {
            const auto hb = decode_heartbeat(*hb_msg);
            if (!hb) {
                std::cerr << "coordinator: dropped malformed heartbeat\n";
                continue;
            }
            touch(hb->worker_id);
        }

        // 3) Deadline sweep: tombstone silent workers, write their stores
        //    off, reopen their completions, re-dispatch every open item.
        const auto t = now();
        bool any_death = false;
        for (auto& [id, w] : registry) {
            if (!w.alive || t - w.last_seen <= cfg.death_threshold) continue;
            w.alive = false;
            any_death = true;
            ++s.workers_died;
            std::cerr << "coordinator: worker " << id << " dead (silent > "
                      << cfg.death_threshold.count() << " ms)\n";
            for (const auto& [path, events] : w.completed) {
                auto st = state.find(path);
                if (st == state.end() || !st->second.done) continue;
                st->second.done = false;   // rows lived only in the dead store
                ++open;
                --s.files_ok;
                s.total_events -= events;
            }
            w.completed.clear();
        }

        // Live-worker count for this pass: computed once, right after the
        // tombstone pass, and reused below. A dead pool means an anonymous
        // PUSH re-dispatch send has no pipes to round-robin into (bind-mode
        // PUSH goes mute), which would otherwise block for the send timeout
        // instead of letting the abort check (section 4) fire.
        int live = 0;
        for (const auto& [id, w] : registry) {
            if (w.alive) ++live;
            (void)id;
        }

        if (any_death) {
            for (auto& [path, st] : state) {
                if (st.done) continue;
                if (st.redispatches >= cfg.redispatch_cap) {
                    st.done = true;
                    --open;
                    ++s.files_failed;
                    std::cerr << "coordinator: item " << path
                              << " failed permanently (re-dispatch cap)\n";
                    continue;
                }
                ++st.redispatches;
                std::cerr << "coordinator: re-dispatch " << path << " (attempt "
                          << (st.redispatches + 1) << ")\n";
                if (live > 0) {
                    work.send(encode(WorkItem{path}));
                }
            }
        }

        // 4) Abort: nobody to do the remaining work.
        const bool nobody_ever = registry.empty() &&
                                 (t - start > cfg.death_threshold);
        if (open > 0 && ((live == 0 && !registry.empty()) || nobody_ever)) {
            std::cerr << "coordinator: abort: no live workers, " << open
                      << " items unsettled\n";
            s.files_failed += static_cast<int>(open);
            open = 0;   // leave the loop; STOP goes only to live workers (none)
        }
    }

    for (const auto& [id, w] : registry) {
        if (w.alive) work.send(make_stop());
        (void)id;
    }
    return s;
}

} // namespace mas
