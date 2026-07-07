#include "mas/ZmqTransport.hpp"
#include <gtest/gtest.h>

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

} // namespace
