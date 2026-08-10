cmake_minimum_required(VERSION 3.16)
project(mas_telemetry LANGUAGES CXX)

find_package(Threads REQUIRED)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip
  URL_HASH SHA256=1f357c27ca988c3f7c6b4bf68a9395005ac6761f034046e9dde0896e3aba00e4)
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)

# ZeroMQ: libzmq compiled from source + cppzmq header-only bindings (spec §15).
# cppzmq's CMake picks up the in-tree `libzmq` target automatically.
set(ZMQ_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WITH_PERF_TOOL OFF CACHE BOOL "" FORCE)
set(WITH_DOCS OFF CACHE BOOL "" FORCE)
set(ENABLE_DRAFTS OFF CACHE BOOL "" FORCE)
set(BUILD_STATIC OFF CACHE BOOL "" FORCE)
# libzmq 4.3.5 declares cmake_minimum_required(3.0.2); CMake >= 4.0 refuses
# pre-3.5 compatibility. This variable propagates into the FetchContent
# subdirectory (cmake_policy() would not) and is the mechanism CMake's own
# error message documents.
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)
FetchContent_Declare(libzmq
  URL https://github.com/zeromq/libzmq/releases/download/v4.3.5/zeromq-4.3.5.tar.gz
  URL_HASH SHA256=6653ef5910f17954861fe72332e68b03ca6e4d9c7160eb3a8de5a5a913bfab43)
set(CPPZMQ_BUILD_TESTS OFF CACHE BOOL "" FORCE)
FetchContent_Declare(cppzmq
  URL https://github.com/zeromq/cppzmq/archive/refs/tags/v4.10.0.tar.gz
  URL_HASH SHA256=c81c81bba8a7644c84932225f018b5088743a22999c6d82a2b5f5cd1e6942b74)
FetchContent_MakeAvailable(libzmq cppzmq)
unset(CMAKE_POLICY_VERSION_MINIMUM)

# DuckDB v1.2.2 prebuilt binary (never build from source — 30+ min).
if(WIN32)
  set(DUCKDB_ASSET libduckdb-windows-amd64.zip)
  set(DUCKDB_LIBNAME duckdb.dll)
  set(DUCKDB_IMPLIB duckdb.lib)        # MSVC links against the import lib,
                                       # not the DLL; ELF and Mach-O need no
                                       # equivalent, which is why the other
                                       # two branches do not set it.
  # Pinned from the first verified download on the Windows target box
  # (2026-08-10, matches the official v1.2.2 release asset).
  set(DUCKDB_SHA256 8fa8b50b49d0ec9a84a95edff9bfa926b8ae336d5d9a3a6442ea494606ebc9c7)
elseif(APPLE)
  set(DUCKDB_ASSET libduckdb-osx-universal.zip)
  set(DUCKDB_LIBNAME libduckdb.dylib)
  set(DUCKDB_SHA256 150588828afd4e84cc7b13f69ba62247147a20c814a8b9a21efa55531d2fd3c9)
else()
  set(DUCKDB_ASSET libduckdb-linux-amd64.zip)
  set(DUCKDB_LIBNAME libduckdb.so)
  # Not pinned: only the macOS asset has been downloaded/verified so far (dev
  # machine is macOS). Compute with `shasum -a 256` after this branch's first
  # real download and fill in, mirroring the APPLE branch above.
  set(DUCKDB_SHA256 "")
endif()
if(NOT DUCKDB_SHA256)
  message(WARNING
    "DuckDB asset ${DUCKDB_ASSET} is downloaded WITHOUT hash verification. "
    "The macOS asset is pinned; this platform's is not, because it has never "
    "been fetched here. Run `shasum -a 256` on the downloaded zip and fill in "
    "DUCKDB_SHA256 in CMakeLists.txt.")
endif()
if(DUCKDB_SHA256)
  FetchContent_Declare(duckdb_prebuilt
    URL https://github.com/duckdb/duckdb/releases/download/v1.2.2/${DUCKDB_ASSET}
    URL_HASH SHA256=${DUCKDB_SHA256})
else()
  FetchContent_Declare(duckdb_prebuilt
    URL https://github.com/duckdb/duckdb/releases/download/v1.2.2/${DUCKDB_ASSET})
endif()
FetchContent_MakeAvailable(duckdb_prebuilt)

add_library(duckdb_imported SHARED IMPORTED GLOBAL)
set_target_properties(duckdb_imported PROPERTIES
  IMPORTED_LOCATION "${duckdb_prebuilt_SOURCE_DIR}/${DUCKDB_LIBNAME}"
  INTERFACE_INCLUDE_DIRECTORIES "${duckdb_prebuilt_SOURCE_DIR}")
if(WIN32)
  set_target_properties(duckdb_imported PROPERTIES
    IMPORTED_IMPLIB "${duckdb_prebuilt_SOURCE_DIR}/${DUCKDB_IMPLIB}")
endif()

# Cleaning hot path: C++20 stdlib only, no DuckDB, no ZeroMQ. This is what the
# portable benchmark build (MAS_BENCH_ONLY) compiles, and the reason the split
# exists -- Pipeline.cpp used to drag duckdb_imported into anything that cleaned.
add_library(mas_clean_core
  core/src/domain/CapEventExtractor.cpp
  core/src/domain/CapEventExtractorFlat.cpp
  core/src/store/CsvRawReader.cpp)
target_include_directories(mas_clean_core PUBLIC core/include)

add_library(mas_store
  core/src/store/CsvEventStore.cpp
  core/src/store/DuckDbEventStore.cpp
  core/src/domain/Pipeline.cpp)
target_link_libraries(mas_store PUBLIC mas_clean_core PRIVATE duckdb_imported)

add_library(mas_agent
  core/src/agent/Message.cpp
  core/src/agent/CleaningWorker.cpp
  core/src/agent/Coordinator.cpp)
target_link_libraries(mas_agent PUBLIC mas_store)

# Back-compat alias so existing target_link_libraries(... mas_core) keeps working.
add_library(mas_core INTERFACE)
target_link_libraries(mas_core INTERFACE mas_agent)

add_library(mas_transport core/src/transport/ZmqTransport.cpp)
target_include_directories(mas_transport PUBLIC core/include)
target_link_libraries(mas_transport PUBLIC cppzmq)

add_executable(clean_exe core/src/apps/clean_main.cpp)
target_link_libraries(clean_exe PRIVATE mas_core)
set_target_properties(clean_exe PROPERTIES OUTPUT_NAME clean)

add_executable(merge_exe core/src/apps/merge_main.cpp)
target_link_libraries(merge_exe PRIVATE mas_core)
set_target_properties(merge_exe PROPERTIES OUTPUT_NAME mas_merge)

add_executable(worker_exe core/src/apps/worker_main.cpp)
target_link_libraries(worker_exe PRIVATE mas_core mas_transport)
set_target_properties(worker_exe PROPERTIES OUTPUT_NAME mas_worker)

add_executable(coordinator_exe core/src/apps/coordinator_main.cpp)
target_link_libraries(coordinator_exe PRIVATE mas_core mas_transport)
set_target_properties(coordinator_exe PROPERTIES OUTPUT_NAME mas_coordinator)

add_executable(monolith_exe core/src/apps/monolith_main.cpp)
target_link_libraries(monolith_exe PRIVATE mas_core Threads::Threads)
set_target_properties(monolith_exe PROPERTIES OUTPUT_NAME mas_monolith)

if(WIN32)
  # /usr/bin/time -l stand-in for bench/run_bench.sh: prints the same two
  # stderr lines (real/user/sys + peak RSS in bytes) so parse_time() keeps a
  # single parser across macOS and Windows.
  add_executable(win_time_exe bench/win_time.cpp)
  target_link_libraries(win_time_exe PRIVATE psapi)
  set_target_properties(win_time_exe PROPERTIES OUTPUT_NAME win_time)
endif()

enable_testing()
add_executable(unit_tests
  tests/test_cap_event.cpp
  tests/test_cap_event_extractor.cpp
  tests/test_cap_event_extractor_flat.cpp
  tests/test_csv_raw_reader.cpp
  tests/test_pipeline.cpp
  tests/test_duckdb_smoke.cpp
  tests/test_duckdb_event_store.cpp
  tests/test_zmq_smoke.cpp
  tests/test_message.cpp
  tests/test_zmq_transport.cpp
  tests/test_cleaning_worker.cpp
  tests/test_coordinator.cpp)
target_link_libraries(unit_tests PRIVATE mas_core mas_transport GTest::gtest_main duckdb_imported cppzmq)
target_include_directories(unit_tests PRIVATE tests)
set_target_properties(unit_tests clean_exe merge_exe worker_exe coordinator_exe monolith_exe PROPERTIES
  BUILD_RPATH "${duckdb_prebuilt_SOURCE_DIR}")
if(WIN32)
  # No rpath on Windows: the loader looks next to the .exe, so copy each
  # runtime DLL beside every binary that links it (libzmq builds shared and
  # its DLL lands deep in _deps).
  foreach(tgt clean_exe merge_exe worker_exe coordinator_exe monolith_exe unit_tests)
    add_custom_command(TARGET ${tgt} POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
              "${duckdb_prebuilt_SOURCE_DIR}/${DUCKDB_LIBNAME}"
              "$<TARGET_FILE_DIR:${tgt}>")
  endforeach()
  foreach(tgt worker_exe coordinator_exe unit_tests)
    add_custom_command(TARGET ${tgt} POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
              "$<TARGET_FILE:libzmq>" "$<TARGET_FILE_DIR:${tgt}>")
  endforeach()
endif()
include(GoogleTest)
gtest_discover_tests(unit_tests)
