#include "mas/transport/ZmqTransport.hpp"
#include "mas/agent/CleaningWorker.hpp"
#include <chrono>
#include <gtest/gtest.h>
#include <stdexcept>

namespace {

TEST(ZmqTransport, PushPullRoundtripOverInproc) {
    zmq::context_t ctx(0);
    // inproc requires bind before connect: construct the bound PULL first.
    mas::ZmqPullSource source(ctx, "inproc://t1", /*bind=*/true, /*timeout_ms=*/1000);
    mas::ZmqPushSink sink(ctx, "inproc://t1", /*bind=*/false);

    sink.send(mas::Message{"hello"});
    const auto m = source.recv();
    ASSERT_TRUE(m.has_value());
    EXPECT_EQ(m->payload, "hello");
}

TEST(ZmqTransport, RecvTimesOutToNullopt) {
    zmq::context_t ctx(0);
    mas::ZmqPullSource source(ctx, "inproc://t2", /*bind=*/true, /*timeout_ms=*/50);
    EXPECT_FALSE(source.recv().has_value());
}

// Coordinator/worker arrangement: the sink binds and the source
// connects to it, with the source blocking indefinitely (timeout_ms = -1,
// the production default) instead of the reverse arrangement above. Send
// before recv so the blocking recv() has a message already queued and can
// never hang the test.
TEST(ZmqTransport, SinkBindsSourceConnectsBlockingRecvRoundtrips) {
    zmq::context_t ctx(0);
    // inproc requires bind before connect: construct the bound PUSH first.
    mas::ZmqPushSink sink(ctx, "inproc://t3", /*bind=*/true);
    mas::ZmqPullSource source(ctx, "inproc://t3", /*bind=*/false, /*timeout_ms=*/-1);

    sink.send(mas::Message{"hello"});
    const auto m = source.recv();
    ASSERT_TRUE(m.has_value());
    EXPECT_EQ(m->payload, "hello");
}

// Final-review fix: a PUSH socket with no connected peer is
// "mute" in ZeroMQ terms - binding gives it an endpoint but not a peer, so
// send() blocks forever without a timeout. This reproduces the coordinator's
// zero-workers hang scenario. With send_timeout_ms set, the mute-send EAGAIN
// must surface as an exception instead of blocking indefinitely.
TEST(ZmqTransport, SendTimesOutAndThrowsWhenNoPeerConnected) {
    zmq::context_t ctx(0);
    mas::ZmqPushSink sink(ctx, "inproc://t4", /*bind=*/true, /*send_timeout_ms=*/100);
    EXPECT_THROW(sink.send(mas::Message{"hello"}), std::runtime_error);
}

// Chaos E2E finding (Plan 4 Task 7, orphan-worker direction): a CONNECT-mode
// PUSH to a peer that never exists creates its pipe immediately and send()
// queues below HWM — the mute-send timeout never fires. Without linger_ms,
// ZmqPushSink couples ZMQ_LINGER to send_timeout_ms (60000 here), so this
// teardown would block inside zmq_ctx_term for the full 60 s trying to flush
// the queued, undeliverable message; the orphan mas_worker was measured alive
// for 121 s (61 s correct idle exit + 60 s linger) against a ~65 s spec §8
// budget. linger_ms=0 must make teardown drop the queue and return at once.
TEST(ZmqTransport, ZeroLingerTeardownDropsUndeliverableQueueImmediately) {
    std::chrono::steady_clock::time_point t0;
    {
        // 1 I/O thread: TCP needs one (inproc-only tests above use 0), and
        // the reproduction is TCP-specific — connect-mode pipe + reconnect.
        zmq::context_t ctx(1);
        // Nothing ever binds this endpoint (chaos runs use 5571-73/5581-83).
        mas::ZmqPushSink sink(ctx, "tcp://127.0.0.1:5599", /*bind=*/false,
                              /*send_timeout_ms=*/60000, /*linger_ms=*/0);
        sink.send(mas::Message{"undeliverable heartbeat"});
        t0 = std::chrono::steady_clock::now();
    }   // sink then ctx destroyed: zmq_ctx_term must not wait out linger
    const std::chrono::duration<double> dt =
        std::chrono::steady_clock::now() - t0;
    EXPECT_LT(dt.count(), 2.0);
}

// The other half of the same knob, and the half that had no test. Fixing the
// 121 s teardown above by zeroing linger cost the worker its Goodbye: the BYE
// is sent and the sink is destroyed immediately after, so at linger_ms=0 the
// frame is dropped on the way out and the coordinator reads an ordinary
// departure as a death -- completions reopened, items re-dispatched,
// workers_died inflated. worker_main.cpp now gives the result sink 300 ms for
// exactly this, and that constant lives in a main() where no unit test can see
// it. Without this test, reverting it to 0 leaves the whole suite green.
TEST(ZmqTransport, NonZeroLingerFlushesAQueuedFrameToAPeerThatIsStillThere) {
    zmq::context_t ctx(1);
    mas::ZmqPullSource peer(ctx, "tcp://127.0.0.1:5598", /*bind=*/true,
                            /*timeout_ms=*/2000);
    {
        // The worker's own constant, not a literal: this must fail if that
        // value goes back to 0, which is the regression it exists to catch.
        mas::ZmqPushSink sink(ctx, "tcp://127.0.0.1:5598", /*bind=*/false,
                              /*send_timeout_ms=*/60000,
                              /*linger_ms=*/mas::kResultSinkLingerMs);
        sink.send(mas::Message{"goodbye"});
    }   // sink destroyed straight after the send, as the worker destroys its own
    const auto m = peer.recv();
    ASSERT_TRUE(m.has_value())
        << "the frame was dropped at teardown; linger is back to 0";
    EXPECT_EQ(m->payload, "goodbye");
}

} // namespace
