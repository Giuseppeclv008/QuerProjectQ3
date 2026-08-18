#include "mas/agent/Coordinator.hpp"
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
    // Announced idle-exit (BYE frame). Also !alive, but the opposite of a
    // tombstone everywhere it matters: the store is intact, so completions
    // stay counted and nothing of this worker's is reopened or re-dispatched.
    bool departed = false;
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

    std::unordered_map<std::string, WorkerState> registry;
    // Who holds which open item (CLAIM frames). The work socket is anonymous
    // PUSH round-robin, so this map is the only attribution there is: an item
    // absent from it may be queued in anyone's pipe, including a dead one.
    std::unordered_map<std::string, std::string> holder;   // in_path -> worker
    const auto start = now();

    const auto touch = [&](const std::string& worker_id) -> WorkerState* {
        auto [it, inserted] = registry.try_emplace(worker_id);
        if (inserted) {
            it->second.last_seen = now();
            std::cerr << "coordinator: worker " << worker_id << " joined\n";
        } else if (!it->second.alive) {
            return nullptr;   // tombstoned or departed: gone is gone (spec §8)
        } else {
            it->second.last_seen = now();
        }
        return &it->second;
    };

    const auto count_live = [&] {
        int live = 0;
        for (const auto& [id, w] : registry) {
            if (w.alive) ++live;
            (void)id;
        }
        return live;
    };

    // One policy for every re-dispatch, because expressing it at a single call
    // site is how the other four came to be missed. ZmqPushSink::send throws
    // when a mute socket hits its send timeout, and coordinator_main gives that
    // socket a 60 s one. Unguarded, the throw escapes run_coordinator and the
    // process reports "error:" and exit 1 for a run in which every other item
    // was settled and its events persisted.
    //
    // Re-dispatch is best-effort. It is settled failed rather than left open,
    // and that is what guarantees the loop terminates: a work socket that has
    // stopped accepting anything would otherwise leave an open item no live
    // worker can ever be given.
    //
    // That is exact at the two charged sites, where the item really has failed
    // or lost its holder. At the uncharged ones -- an unclaimed item at a
    // death, a claim from a tombstoned worker -- it is the least-bad option
    // rather than a free one: the item may still be sitting in a live but busy
    // worker's pipe, which is exactly when a PUSH send hits its high-water mark
    // and times out, so a later merge can fold in rows for a file this summary
    // counted failed. The summary can therefore undercount ok files against the
    // merged store; it never overcounts them, and it never hangs.
    //
    // The initial dispatch is deliberately NOT routed through here and still
    // throws: failing to place work that has never run is a failed run, not a
    // settled item. A test pins that half, so widening this guard to cover it
    // fails rather than passes silently.
    // Returns nothing on purpose: every caller's work is finished either way,
    // because the failure branch settles the item itself. An earlier version
    // returned bool and all five sites discarded it, which only invited a
    // reader to hunt for the site that branched on it.
    const auto try_redispatch = [&](const std::string& path, ItemState& st) {
        try {
            work.send(encode(WorkItem{path}));
        } catch (const std::exception& e) {
            std::cerr << "coordinator: could not re-dispatch " << path << " ("
                      << e.what() << "); settling it as failed\n";
            st.done = true;
            --open;
            ++s.files_failed;
        }
    };

    // A goodbye (announced idle-exit) is the opposite of a death: the store is
    // intact, so completions stay counted, nothing reopens, no cap is charged,
    // and workers_died is untouched. Without the distinction, three workers
    // finishing their queue and idling out looked identical to three crashes
    // -- and failed the whole remaining batch.
    const auto mark_departed = [&](const std::string& worker_id) {
        auto it = registry.find(worker_id);
        if (it == registry.end() || !it->second.alive) return;
        it->second.alive = false;
        it->second.departed = true;
        std::cerr << "coordinator: worker " << worker_id
                  << " departed (announced idle exit)\n";
        // A goodbye with a claim outstanding is not a path the worker takes
        // (it claims, results, then idles), but stranding the item on the
        // assumption would turn a protocol slip into a hung run.
        for (auto h = holder.begin(); h != holder.end();) {
            if (h->second != worker_id) {
                ++h;
                continue;
            }
            const auto st = state.find(h->first);
            if (st != state.end() && !st->second.done && count_live() > 0) {
                std::cerr << "coordinator: attempting re-dispatch of " << h->first
                          << " (holder departed)\n";
                try_redispatch(h->first, st->second);
            }
            h = holder.erase(h);
        }
    };

    // Registration gate (Plan 5): with expected_workers > 0, hold the initial
    // dispatch until that many workers have said hello, so PUSH round-robins
    // over all their pipes instead of queueing everything into the first one.
    if (cfg.expected_workers > 0) {
        while (static_cast<int>(registry.size()) < cfg.expected_workers) {
            // Same tick order as the main loop: one results tick paces the
            // wait (and, under test fakes, advances virtual time)...
            if (const auto msg = results.recv()) {
                if (const auto r = decode_result(*msg)) {
                    touch(r->worker_id);   // liveness only; no items are open yet
                    std::cerr << "coordinator: dropped pre-dispatch result from "
                              << r->worker_id << "\n";
                } else if (const auto c = decode_claim(*msg)) {
                    touch(c->worker_id);   // liveness only; nothing dispatched yet
                } else if (const auto g = decode_goodbye(*msg)) {
                    mark_departed(g->worker_id);
                } else {
                    std::cerr << "coordinator: dropped malformed result\n";
                }
            }
            // ...then a heartbeat drain.
            while (auto hb_msg = heartbeats.recv()) {
                const auto hb = decode_heartbeat(*hb_msg);
                if (!hb) {
                    std::cerr << "coordinator: dropped malformed heartbeat\n";
                    continue;
                }
                touch(hb->worker_id);
            }
            if (now() - start > cfg.registration_timeout) {
                if (registry.empty()) {
                    std::cerr << "coordinator: abort: no workers registered within "
                              << cfg.registration_timeout.count() << " ms\n";
                    s.files_failed = static_cast<int>(open);
                    return s;
                }
                std::cerr << "coordinator: proceeding with " << registry.size()
                          << " of " << cfg.expected_workers << " workers\n";
                break;
            }
        }
    }
    for (const auto& item : items) work.send(encode(item));

    // Loop-pass order is load-bearing for deterministic tests and pinned
    // here: (1) one lifecycle tick (result, claim, or goodbye) — its recv
    // timeout paces the loop and, under test fakes, advances virtual time;
    // (2) heartbeat drain — refreshes are stamped with now() AFTER the tick,
    // so a beat delivered this pass survives the deadline this pass;
    // (3) deadline sweep; (4) aborts.
    while (open > 0) {
        // 1) One lifecycle tick: a result, a claim, or a goodbye (they share
        //    the socket, so claim-before-result is a FIFO guarantee). The
        //    production recv timeout of 200 ms paces the loop.
        if (const auto msg = results.recv()) {
            if (const auto r = decode_result(*msg)) {
                if (WorkerState* w = touch(r->worker_id); !w) {
                    // Late result from a tombstoned worker: its store is
                    // written off, so counting this would credit rows we may
                    // never merge.
                    std::cerr << "coordinator: dropped result for " << r->in_path
                              << " from dead worker " << r->worker_id << "\n";
                } else {
                    const auto st = state.find(r->in_path);
                    if (st == state.end() || st->second.done) {
                        std::cerr << "coordinator: dropped duplicate/unknown result for "
                                  << r->in_path << "\n";
                    } else {
                        // -2 is the one reported failure worth retrying: the
                        // store died with rows already written, which the same
                        // file on a healthy store does not do, and the upsert
                        // is idempotent on (machine_id, head_id, ts) so the
                        // retry completes the item rather than duplicating it.
                        // Charged against the same cap that bounds
                        // death-driven re-dispatch, so a store that fails every
                        // time still terminates. The budget is shared, and that
                        // has a cost worth naming: an item that survives two
                        // transient store faults has spent the resilience it
                        // would otherwise have had for its holder dying. One
                        // cap is still the right call -- two would let a file
                        // alternating between the two failures run forever --
                        // but a run that re-dispatches for a store fault is
                        // less resilient to a death afterwards, not equally so.
                        // -1 keeps its shortcut: nothing was written, the
                        // failure is deterministic.
                        const bool retryable =
                            r->events == -2 &&
                            st->second.redispatches < cfg.redispatch_cap &&
                            count_live() > 0;
                        holder.erase(r->in_path);
                        if (retryable) {
                            // Deliberately not a `continue`: the rest of this
                            // pass -- heartbeat drain, death sweep, abort check
                            // -- must still run, or a retry would silently cost
                            // the loop a tick of liveness.
                            ++st->second.redispatches;
                            std::cerr << "coordinator: attempting re-dispatch of "
                                      << r->in_path << " (attempt "
                                      << (st->second.redispatches + 1)
                                      << ", partial store failure)\n";
                            // try_redispatch settles the item itself if the send
                            // does not land, so there is nothing to do on either
                            // outcome here -- and settling it again below would
                            // double-count files_failed and decrement open twice.
                            try_redispatch(r->in_path, st->second);
                        } else {
                            st->second.done = true;
                            --open;
                            if (r->events >= 0) {
                                ++s.files_ok;
                                s.total_events += r->events;
                                w->completed.emplace_back(r->in_path, r->events);
                            } else {
                                ++s.files_failed;   // unreadable input, or a
                                                    // partial failure past the cap
                            }
                        }
                    }
                }
            } else if (const auto c = decode_claim(*msg)) {
                if (touch(c->worker_id)) {
                    const auto st = state.find(c->in_path);
                    if (st != state.end() && !st->second.done)
                        holder[c->in_path] = c->worker_id;
                } else {
                    // A claim from a tombstoned worker is proof of two things:
                    // the worker is alive (tombstoned for silence, not dead),
                    // and the item landed in a written-off pipe -- the zombie's
                    // RESULT will be dropped at this same gate, so without
                    // action the item never settles: survivors run dry, idle
                    // out, and the run aborts on a file that was cleaned
                    // correctly into a store nobody will merge. Re-dispatch
                    // uncharged (the zombie vouched for nothing) unless a live
                    // worker already holds it. Consuming one claim emits at
                    // most one frame, so the queue cannot grow -- and the
                    // re-send can round-robin into the zombie again, which
                    // just repeats this exchange until a live pipe wins.
                    const auto st = state.find(c->in_path);
                    const auto h = holder.find(c->in_path);
                    const bool held_by_live =
                        h != holder.end() &&
                        [&] {
                            const auto w = registry.find(h->second);
                            return w != registry.end() && w->second.alive;
                        }();
                    if (st != state.end() && !st->second.done && !held_by_live &&
                        count_live() > 0) {
                        std::cerr << "coordinator: attempting re-dispatch of " << c->in_path
                                  << " (claimed by tombstoned " << c->worker_id
                                  << "; uncharged)\n";
                        try_redispatch(c->in_path, st->second);
                    }
                }
            } else if (const auto g = decode_goodbye(*msg)) {
                mark_departed(g->worker_id);
            } else {
                std::cerr << "coordinator: dropped malformed result\n";
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

        // 3) Deadline sweep: tombstone silent workers (announced departures
        //    were already marked !alive and are skipped here), write their
        //    stores off, reopen their completions.
        const auto t = now();
        std::vector<std::string> dead_now;
        for (auto& [id, w] : registry) {
            if (!w.alive || t - w.last_seen <= cfg.death_threshold) continue;
            w.alive = false;
            dead_now.push_back(id);
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
        const int live = count_live();

        if (!dead_now.empty()) {
            // Re-dispatch by attribution, not blanket. Three cases per open
            // item:
            //   holder died  -> re-send and charge its cap: repeated deaths of
            //                   *its own* holders is the poison-item signature
            //                   the cap exists for.
            //   no holder    -> re-send free of charge: the item may be queued
            //                   in a dead pipe, and no worker ever vouched for
            //                   it, so its death count is evidence of nothing.
            //                   Bounded: this only runs on a death, and there
            //                   are at most as many deaths as workers.
            //   holder alive -> leave it alone. It is mid-file on a live
            //                   worker; the blanket re-send used to burn its
            //                   cap on unrelated deaths until the run reported
            //                   a completed item as a total loss.
            const auto died_now = [&](const std::string& id) {
                for (const auto& d : dead_now)
                    if (d == id) return true;
                return false;
            };
            for (auto& [path, st] : state) {
                if (st.done) continue;
                const auto h = holder.find(path);
                if (h != holder.end() && !died_now(h->second)) continue;
                const bool charged = h != holder.end();
                if (charged) holder.erase(h);
                if (!charged) {
                    std::cerr << "coordinator: attempting re-dispatch of " << path
                              << " (unclaimed at a death; uncharged)\n";
                    if (live > 0) try_redispatch(path, st);
                    continue;
                }
                if (st.redispatches >= cfg.redispatch_cap) {
                    st.done = true;
                    --open;
                    ++s.files_failed;
                    std::cerr << "coordinator: item " << path
                              << " failed permanently (re-dispatch cap)\n";
                    continue;
                }
                ++st.redispatches;
                std::cerr << "coordinator: attempting re-dispatch of " << path
                          << " (attempt " << (st.redispatches + 1)
                          << ", holder died)\n";
                if (live > 0) {
                    try_redispatch(path, st);
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

    // Shutdown. One STOP per live worker is not enough: the work socket is
    // anonymous PUSH, which round-robins across *connected pipes*, and a worker
    // tombstoned for silence may well still be alive with its pipe attached —
    // that is precisely why its items get re-dispatched. It then absorbs a STOP
    // meant for someone else, and the live worker that missed out sits out its
    // whole idle-exit budget (60 ticks, ~60 s) before returning.
    //
    // So send one per live worker plus one per tombstoned worker. A surplus STOP
    // is free: a genuinely dead peer has no pipe and its frames are dropped at
    // the socket's linger. The `live > 0` guard is load-bearing — sending into a
    // PUSH socket with no peers blocks for the send timeout, which is why the
    // abort path leaves the loop without stopping anyone.
    int live = 0, tombstoned = 0;
    for (const auto& [id, w] : registry) {
        (w.alive ? live : tombstoned)++;
        (void)id;
    }
    // Best-effort by construction, so a transport failure here must not lose
    // the run. Every item is settled and every event is already in a worker's
    // store by this point; an undelivered STOP costs a worker its 60-tick
    // idle-exit budget, not a row. Unguarded, a throw skipped the return and
    // coordinator_main reported "error:" and exit 1 for a run that had in fact
    // finished -- and the peers a mute socket implies are exactly the peers
    // that are shutting down anyway. Dispatch above is deliberately not
    // wrapped: a work socket that cannot accept the work is a failed run.
    if (live > 0) {
        try {
            for (int i = 0; i < live + tombstoned; ++i) work.send(make_stop());
        } catch (const std::exception& e) {
            std::cerr << "coordinator: could not deliver STOP (" << e.what()
                      << "); workers will idle-exit on their own budget\n";
        }
    }
    return s;
}

} // namespace mas
