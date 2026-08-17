#include "mas/agent/Message.hpp"
#include "fakes/FakeTransport.hpp"
#include <gtest/gtest.h>

namespace {

TEST(MessageCodec, WorkItemRoundtrips) {
    const auto m = mas::encode(mas::WorkItem{"/data/day-01.csv"});
    const auto w = mas::decode_work(m);
    ASSERT_TRUE(w.has_value());
    EXPECT_EQ(w->in_path, "/data/day-01.csv");
}

TEST(MessageCodec, WorkResultRoundtrips) {
    const auto m = mas::encode(mas::WorkResult{"/data/day-01.csv", 765711, 12.5, "w1"});
    const auto r = mas::decode_result(m);
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(r->in_path, "/data/day-01.csv");
    EXPECT_EQ(r->events, 765711);
    EXPECT_DOUBLE_EQ(r->seconds, 12.5);
    EXPECT_EQ(r->worker_id, "w1");
}

TEST(MessageCodec, StopIsRecognized) {
    EXPECT_TRUE(mas::is_stop(mas::make_stop()));
    EXPECT_FALSE(mas::is_stop(mas::encode(mas::WorkItem{"x"})));
}

TEST(MessageCodec, MalformedPayloadsDecodeToNullopt) {
    EXPECT_FALSE(mas::decode_work(mas::Message{"RESULT\n/x\n1\n0.1\nw1"}).has_value()); // wrong tag
    EXPECT_FALSE(mas::decode_work(mas::Message{"WORK"}).has_value());               // missing field
    EXPECT_FALSE(mas::decode_work(mas::Message{"WORK\n"}).has_value());             // empty path
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\nnotanumber\n0.1\nw1"}).has_value());
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\n5"}).has_value());    // missing field
    EXPECT_FALSE(mas::decode_result(mas::Message{""}).has_value());
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\n5garbage\n0.1\nw1"}).has_value());
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\n5\n1.2extra\nw1"}).has_value());
}

TEST(Message, HeartbeatRoundTrip) {
    const mas::Heartbeat h{"w1", 42};
    const auto m = mas::encode(h);
    const auto d = mas::decode_heartbeat(m);
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(d->worker_id, "w1");
    EXPECT_EQ(d->seq, 42);
}

TEST(Message, DecodeHeartbeatRejectsMalformed) {
    // wrong tag, wrong arity, empty id, non-numeric / trailing-garbage /
    // negative seq: all rejected, none throw.
    EXPECT_FALSE(mas::decode_heartbeat({"WORK\nw1\n1"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\n1\nextra"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\n\n1"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\nabc"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\n1x"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\n-1"}).has_value());
}

TEST(Message, ResultRoundTripCarriesWorkerId) {
    const mas::WorkResult r{"day.csv", 123, 4.5, "w2"};
    const auto d = mas::decode_result(mas::encode(r));
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(d->in_path, "day.csv");
    EXPECT_EQ(d->events, 123);
    EXPECT_DOUBLE_EQ(d->seconds, 4.5);
    EXPECT_EQ(d->worker_id, "w2");
}

TEST(Message, DecodeResultRejectsMissingOrEmptyWorkerId) {
    // Plan 3's 4-line RESULT frame is no longer valid.
    EXPECT_FALSE(mas::decode_result({"RESULT\nday.csv\n123\n4.5"}).has_value());
    EXPECT_FALSE(mas::decode_result({"RESULT\nday.csv\n123\n4.5\n"}).has_value());
}

TEST(Message, ClaimRoundTrips) {
    const auto m = mas::encode(mas::WorkClaim{"day.csv", "w3"});
    const auto d = mas::decode_claim(m);
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(d->in_path, "day.csv");
    EXPECT_EQ(d->worker_id, "w3");
}

TEST(Message, DecodeClaimRejectsWrongShape) {
    EXPECT_FALSE(mas::decode_claim({"CLAIM\nday.csv"}).has_value());
    EXPECT_FALSE(mas::decode_claim({"CLAIM\n\nw3"}).has_value());
    EXPECT_FALSE(mas::decode_claim({"CLAIM\nday.csv\n"}).has_value());
    EXPECT_FALSE(mas::decode_claim({"RESULT\nday.csv\nw3"}).has_value());
}

TEST(Message, GoodbyeRoundTrips) {
    const auto m = mas::encode(mas::Goodbye{"w3"});
    const auto d = mas::decode_goodbye(m);
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(d->worker_id, "w3");
}

TEST(Message, DecodeGoodbyeRejectsWrongShape) {
    EXPECT_FALSE(mas::decode_goodbye({"BYE"}).has_value());
    EXPECT_FALSE(mas::decode_goodbye({"BYE\n"}).has_value());
    EXPECT_FALSE(mas::decode_goodbye({"HB\nw3"}).has_value());
}

TEST(Message, ClaimGoodbyeAndResultDoNotCrossDecode) {
    // All three share the results socket; a decoder accepting a sibling's
    // frame would mis-route lifecycle state.
    const auto claim = mas::encode(mas::WorkClaim{"d.csv", "w1"});
    const auto bye = mas::encode(mas::Goodbye{"w1"});
    const auto res = mas::encode(mas::WorkResult{"d.csv", 1, 0.1, "w1"});
    EXPECT_FALSE(mas::decode_result(claim).has_value());
    EXPECT_FALSE(mas::decode_result(bye).has_value());
    EXPECT_FALSE(mas::decode_claim(res).has_value());
    EXPECT_FALSE(mas::decode_claim(bye).has_value());
    EXPECT_FALSE(mas::decode_goodbye(claim).has_value());
    EXPECT_FALSE(mas::decode_goodbye(res).has_value());
}

TEST(FakeTransport, SourceDrainsQueueThenReturnsNullopt) {
    mas::test::FakeSource src;
    src.queue.push_back(mas::Message{"a"});
    src.queue.push_back(mas::Message{"b"});
    const auto m1 = src.recv();
    ASSERT_TRUE(m1.has_value());
    EXPECT_EQ(m1->payload, "a");
    ASSERT_TRUE(src.recv().has_value());
    EXPECT_FALSE(src.recv().has_value());   // drained => closed source
}

TEST(FakeTransport, SinkRecordsSends) {
    mas::test::FakeSink sink;
    sink.send(mas::Message{"x"});
    ASSERT_EQ(sink.sent.size(), 1u);
    EXPECT_EQ(sink.sent[0].payload, "x");
}

} // namespace

TEST(Message, DecodeResultRejectsOutOfRangeNumerics) {
    // The coordinator adds `events` straight into total_events: two frames
    // carrying LLONG_MAX are signed-overflow UB, one is a garbage headline.
    // "inf"/"nan" stod-parse with full consumption, so seconds needs an
    // explicit range check too. The codec already rejects trailing garbage
    // and caps frame size for this threat model; these were the last open
    // numeric paths.
    auto reject = [](const std::string& events, const std::string& seconds) {
        const mas::Message m{"RESULT\nd1.csv\n" + events + "\n" + seconds + "\nw1"};
        EXPECT_FALSE(mas::decode_result(m).has_value())
            << "events=" << events << " seconds=" << seconds;
    };
    reject("9223372036854775807", "0.1");   // LLONG_MAX
    reject("1000000000001", "0.1");         // just past the cap
    reject("-3", "0.1");                    // below the -2 failure channel
    reject("5", "inf");
    reject("5", "nan");
    reject("5", "-0.5");
    reject("5", "1e12");

    // The boundary values survive. The failure channel is two codes wide:
    // -1 is an unreadable input, -2 a store that died with rows already
    // written -- the coordinator re-dispatches the second and not the first.
    EXPECT_TRUE(mas::decode_result(
        mas::Message{"RESULT\nd1.csv\n-1\n0\nw1"}).has_value());
    EXPECT_TRUE(mas::decode_result(
        mas::Message{"RESULT\nd1.csv\n-2\n0\nw1"}).has_value());
    EXPECT_TRUE(mas::decode_result(
        mas::Message{"RESULT\nd1.csv\n1000000000000\n0.5\nw1"}).has_value());
}
