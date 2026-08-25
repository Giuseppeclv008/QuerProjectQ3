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
#include <vector>
#include <algorithm>

namespace fs = std::filesystem;

namespace {

// A directory of this test's own, so the leftovers checks below mean something.
fs::path scratch(const std::string& name) {
    const auto d = fs::temp_directory_path() / ("mas_pub_" + name);
    fs::remove_all(d);
    fs::create_directories(d);
    return d;
}

// Everything in `dir`, sorted -- the destination plus any temp that survived.
// Scans the whole directory rather than a name prefix: the temp's name is
// deliberately unrelated to the destination's, so a prefix scan would miss
// exactly the litter these checks exist to catch.
std::string leftovers(const fs::path& dir) {
    std::vector<std::string> names;
    for (const auto& e : fs::directory_iterator(dir))
        names.push_back(e.path().filename().string());
    std::sort(names.begin(), names.end());
    std::string out;
    for (const auto& n : names) out += n + " ";
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
    EXPECT_EQ(leftovers(d), "day.parquet ");
    // The temp lives in the destination's directory (the rename must stay on
    // one filesystem) but its name owes nothing to the destination's: a name
    // derived from the destination inherits its length and its glob
    // metacharacters, which is how export verification came to read the wrong
    // file, and how a near-NAME_MAX basename became unpublishable.
    EXPECT_EQ(fs::path(seen_tmp).parent_path(), d) << seen_tmp;
    const auto tmp_base = fs::path(seen_tmp).filename().string();
    EXPECT_TRUE(tmp_base.rfind(".mas-publish.", 0) == 0) << tmp_base;
    EXPECT_TRUE(std::string_view(tmp_base).ends_with(".tmp")) << tmp_base;
    EXPECT_EQ(tmp_base.find("day"), std::string::npos) << tmp_base;
}

TEST(AtomicPublish, GlobMetacharactersInTheDestinationDoNotReachTheTemp) {
    // read_parquet() treats [, ?, * as pattern syntax. A temp spelled
    // <destination>+suffix hands those characters to every verifier that reads
    // the temp back, so verification could match a different file entirely --
    // false negative (good export refused) or false positive (decoy verified).
    const auto d = scratch("meta");
#ifdef _WIN32
    // '*' and '?' are not legal in an NTFS name at all, so the destination
    // itself cannot exist here; '[' and ']' are, and are still glob syntax to
    // read_parquet(). Same property, the subset the filesystem allows.
    const auto target = (d / "e[xy]port.parquet").string();
#else
    const auto target = (d / "e[xy]port-*-?.parquet").string();
#endif
    std::string tmp;
    mas::publish_atomically(target, [&](const std::string& t) {
        tmp = t;
        std::ofstream(t) << "x";
    });
    const auto tmp_base = fs::path(tmp).filename().string();
    EXPECT_EQ(tmp_base.find_first_of("[]*?"), std::string::npos) << tmp_base;
    EXPECT_TRUE(fs::exists(target));
}

TEST(AtomicPublish, ANearNameMaxBasenameIsStillPublishable) {
    // 250 characters is legal on every supported filesystem (NAME_MAX 255).
    // The old destination-derived temp appended ~12 characters and pushed a
    // legal name over the limit; the writer then failed with an IO error
    // quoting the temp and never saying the name was the problem.
    const auto d = scratch("namemax");
#ifdef _WIN32
    // On Windows the binding limit is not the component's NAME_MAX but the
    // whole path's MAX_PATH: 259 characters for a process not manifested
    // longPathAware (this one is not), regardless of the registry policy. A
    // 250-char basename under %TEMP% is over it before any suffix, so the
    // POSIX fixture cannot run here. Same property, the platform's constant:
    // a destination that sits AT the limit must still publish, which the old
    // "destination + suffix" temp could not have done.
    const std::string dir = d.string();
    const int room = 259 - static_cast<int>(dir.size()) - 1 /* '\\' */ - 8 /* .parquet */;
    if (room < 32)   // the temp must stay shorter than the destination
        GTEST_SKIP() << "temp directory too deep for a MAX_PATH-length destination: " << dir;
    const std::string base = std::string(static_cast<size_t>(room), 'a') + ".parquet";
#else
    const std::string base = std::string(242, 'a') + ".parquet"; // 250 chars
#endif
    const auto target = (d / base).string();
    mas::publish_atomically(target, [](const std::string& t) {
        std::ofstream(t) << "x";
    });
    EXPECT_TRUE(fs::exists(target));
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
    EXPECT_EQ(leftovers(d), "") << "a partial temp survived";
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
    EXPECT_EQ(leftovers(d), "") << "the directory was left behind";
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
    EXPECT_EQ(leftovers(d), "day.parquet ");
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
