// publish_atomically sits on the only write path of both Parquet writers, so
// its edge cases are worth pinning directly rather than through a store. These
// began as a throwaway probe written to answer a review question ("can write_to
// succeed but produce nothing? can a crash leave a temp nothing reaps?"); the
// probe found that a directory at the temp path was renamed into place without
// complaint, which is how they became tests.
#include "mas/store/AtomicPublish.hpp"

#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>

namespace fs = std::filesystem;

namespace {

// A directory of this test's own, so the leftovers checks below mean something.
fs::path scratch(const std::string& name) {
    const auto d = fs::temp_directory_path() / ("mas_pub_" + name);
    fs::remove_all(d);
    fs::create_directories(d);
    return d;
}

// Everything in `dir` whose name starts with `stem` -- the destination plus any
// temp that survived.
std::string leftovers(const fs::path& dir, const std::string& stem) {
    std::string out;
    for (const auto& e : fs::directory_iterator(dir)) {
        const auto n = e.path().filename().string();
        if (n.rfind(stem, 0) == 0) out += n + " ";
    }
    return out;
}

std::string threw(const std::function<void()>& f) {
    try {
        f();
    } catch (const std::exception& e) {
        return e.what();
    }
    return "<no throw>";
}

} // namespace

TEST(AtomicPublish, PublishesThroughATempAndLeavesNone) {
    const auto d = scratch("happy");
    const auto target = (d / "day.parquet").string();
    std::string seen_tmp;
    mas::publish_atomically(target, [&](const std::string& tmp) {
        seen_tmp = tmp;
        std::ofstream(tmp) << "finished";
        // Nothing is visible under the destination's name until the rename.
        EXPECT_FALSE(fs::exists(target)) << "published before write_to returned";
    });
    EXPECT_TRUE(fs::exists(target));
    EXPECT_EQ(leftovers(d, "day.parquet"), "day.parquet ");
    EXPECT_NE(seen_tmp.find("day.parquet.tmp."), std::string::npos) << seen_tmp;
}

TEST(AtomicPublish, AThrowFromWriteToPropagatesUnmaskedAndRemovesTheTemp) {
    // The cleanup path must not swallow or replace what actually went wrong --
    // an ENOSPC from DuckDB is the message the operator needs, not a rename
    // failure that happened because of it.
    const auto d = scratch("throws");
    const auto target = (d / "day.parquet").string();
    const auto msg = threw([&] {
        mas::publish_atomically(target, [](const std::string& tmp) {
            std::ofstream(tmp) << "partial";
            throw std::runtime_error("No space left on device");
        });
    });
    EXPECT_EQ(msg, "No space left on device");
    EXPECT_FALSE(fs::exists(target));
    EXPECT_EQ(leftovers(d, "day.parquet"), "") << "a partial temp survived";
}

TEST(AtomicPublish, RefusesToPublishWhenWriteToProducedNothing) {
    const auto d = scratch("nothing");
    const auto target = (d / "day.parquet").string();
    const auto msg = threw([&] {
        mas::publish_atomically(target, [](const std::string&) {});
    });
    EXPECT_NE(msg.find("nothing wrote a file"), std::string::npos) << msg;
    EXPECT_FALSE(fs::exists(target));
}

TEST(AtomicPublish, RefusesToPublishADirectory) {
    // rename() is indifferent to what it moves: handed a directory it renames
    // the directory, and store/day.parquet becomes a directory that every
    // reader then globs. DuckDB's COPY produces a directory when partitioning
    // is on, so this is one option away from reachable.
    const auto d = scratch("dir");
    const auto target = (d / "day.parquet").string();
    const auto msg = threw([&] {
        mas::publish_atomically(target, [](const std::string& tmp) {
            fs::create_directory(tmp);
        });
    });
    EXPECT_NE(msg.find("nothing wrote a file"), std::string::npos) << msg;
    EXPECT_FALSE(fs::exists(target)) << "a directory was published as a Parquet file";
    EXPECT_EQ(leftovers(d, "day.parquet"), "") << "the directory was left behind";
}

TEST(AtomicPublish, TwoPublishesToOnePathUseDifferentTemps) {
    // What the per-store token buys: the pid alone is shared by two writers
    // inside one process, and then both would write one temp.
    const auto d = scratch("twice");
    const auto target = (d / "day.parquet").string();
    std::string first, second;
    mas::publish_atomically(target, [&](const std::string& t) {
        first = t;
        std::ofstream(t) << "one";
    });
    mas::publish_atomically(target, [&](const std::string& t) {
        second = t;
        std::ofstream(t) << "two";
    });
    EXPECT_NE(first, second);
    EXPECT_EQ(leftovers(d, "day.parquet"), "day.parquet ");
}

TEST(AtomicPublish, TheTempNameCannotBeMistakenForAParquetFile) {
    // The reason a temp left by a crash is litter rather than data: nothing
    // reaps them, so the name has to keep them out of every reader's glob.
    const auto d = scratch("glob");
    std::string tmp;
    mas::publish_atomically((d / "day.parquet").string(),
                            [&](const std::string& t) {
                                tmp = t;
                                std::ofstream(t) << "x";
                            });
    EXPECT_FALSE(std::string_view(tmp).ends_with(".parquet")) << tmp;
}
