#pragma once
#include <filesystem>
#include <string>
#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

namespace mas::test {

// Test artifacts under the system temp dir, pid-scoped -- never bare relative
// names in the build directory. A failing ASSERT_* skips the in-test cleanup
// and leaks the file, and in build/ a stale t_store_legacy.duckdb can change
// a later run's result; under the temp dir a leak is inert and two build
// directories' suites cannot collide. test_parquet_export and
// test_atomic_publish already worked this way; this makes it the convention.
inline std::string temp_artifact(const std::string& name) {
#ifdef _WIN32
    const int pid = _getpid();
#else
    const int pid = static_cast<int>(::getpid());
#endif
    const auto dir = std::filesystem::temp_directory_path() /
                     ("mas-tests-" + std::to_string(pid));
    std::filesystem::create_directories(dir);
    return (dir / name).string();
}

} // namespace mas::test
