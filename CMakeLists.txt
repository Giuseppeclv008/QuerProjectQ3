cmake_minimum_required(VERSION 3.16)
project(mas_telemetry LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip
  URL_HASH SHA256=1f357c27ca988c3f7c6b4bf68a9395005ac6761f034046e9dde0896e3aba00e4)
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)

# DuckDB v1.2.2 prebuilt binary (never build from source — 30+ min).
if(APPLE)
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

add_library(mas_core
  core/src/CapEventExtractor.cpp
  core/src/CsvRawReader.cpp
  core/src/CsvEventStore.cpp
  core/src/DuckDbEventStore.cpp
  core/src/Pipeline.cpp)
target_include_directories(mas_core PUBLIC core/include)
target_link_libraries(mas_core PRIVATE duckdb_imported)

add_executable(clean_exe core/src/clean_main.cpp)
target_link_libraries(clean_exe PRIVATE mas_core)
set_target_properties(clean_exe PROPERTIES OUTPUT_NAME clean)

enable_testing()
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp
  tests/test_csv_raw_reader.cpp
  tests/test_pipeline.cpp
  tests/test_duckdb_smoke.cpp
  tests/test_duckdb_event_store.cpp)
target_link_libraries(unit_tests PRIVATE mas_core GTest::gtest_main duckdb_imported)
set_target_properties(unit_tests clean_exe PROPERTIES
  BUILD_RPATH "${duckdb_prebuilt_SOURCE_DIR}")
include(GoogleTest)
gtest_discover_tests(unit_tests)
