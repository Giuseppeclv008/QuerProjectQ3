#include <gtest/gtest.h>
#include <string>
#include <zmq.hpp>

// Build/link sanity for libzmq + cppzmq: one PUSH/PULL roundtrip over inproc.
TEST(ZmqSmoke, InprocPushPullRoundtrip) {
    zmq::context_t ctx(0);  // inproc needs no I/O threads
    zmq::socket_t pull(ctx, zmq::socket_type::pull);
    pull.bind("inproc://smoke");            // inproc: bind must precede connect
    zmq::socket_t push(ctx, zmq::socket_type::push);
    push.connect("inproc://smoke");

    push.send(zmq::buffer(std::string("ping")), zmq::send_flags::none);
    zmq::message_t msg;
    const auto n = pull.recv(msg, zmq::recv_flags::none);
    ASSERT_TRUE(n.has_value());
    EXPECT_EQ(msg.to_string(), "ping");
}
