#include "mas/ZmqTransport.hpp"
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

// Coordinator/worker arrangement (Task 7): the sink binds and the source
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

// Final-review fix (Task 8 wave): a PUSH socket with no connected peer is
// "mute" in ZeroMQ terms - binding gives it an endpoint but not a peer, so
// send() blocks forever without a timeout. This reproduces the coordinator's
// zero-workers hang scenario. With send_timeout_ms set, the mute-send EAGAIN
// must surface as an exception instead of blocking indefinitely.
TEST(ZmqTransport, SendTimesOutAndThrowsWhenNoPeerConnected) {
    zmq::context_t ctx(0);
    mas::ZmqPushSink sink(ctx, "inproc://t4", /*bind=*/true, /*send_timeout_ms=*/100);
    EXPECT_THROW(sink.send(mas::Message{"hello"}), std::runtime_error);
}

} // namespace
