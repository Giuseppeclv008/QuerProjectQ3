#include "mas/Message.hpp"
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
    const auto m = mas::encode(mas::WorkResult{"/data/day-01.csv", 765711, 12.5});
    const auto r = mas::decode_result(m);
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(r->in_path, "/data/day-01.csv");
    EXPECT_EQ(r->events, 765711);
    EXPECT_DOUBLE_EQ(r->seconds, 12.5);
}

TEST(MessageCodec, StopIsRecognized) {
    EXPECT_TRUE(mas::is_stop(mas::make_stop()));
    EXPECT_FALSE(mas::is_stop(mas::encode(mas::WorkItem{"x"})));
}

TEST(MessageCodec, MalformedPayloadsDecodeToNullopt) {
    EXPECT_FALSE(mas::decode_work(mas::Message{"RESULT\n/x\n1\n0.1"}).has_value()); // wrong tag
    EXPECT_FALSE(mas::decode_work(mas::Message{"WORK"}).has_value());               // missing field
    EXPECT_FALSE(mas::decode_work(mas::Message{"WORK\n"}).has_value());             // empty path
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\nnotanumber\n0.1"}).has_value());
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\n5"}).has_value());    // missing field
    EXPECT_FALSE(mas::decode_result(mas::Message{""}).has_value());
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
