cmake_minimum_required(VERSION 3.16)
project(mas_telemetry LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip)
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)

add_library(mas_core
  core/src/CapEventExtractor.cpp
  core/src/CsvRawReader.cpp
  core/src/Pipeline.cpp)
target_include_directories(mas_core PUBLIC core/include)

add_executable(clean_exe core/src/clean_main.cpp)
target_link_libraries(clean_exe PRIVATE mas_core)
set_target_properties(clean_exe PROPERTIES OUTPUT_NAME clean)

enable_testing()
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp
  tests/test_csv_raw_reader.cpp
  tests/test_pipeline.cpp)
target_link_libraries(unit_tests PRIVATE mas_core GTest::gtest_main)
include(GoogleTest)
gtest_discover_tests(unit_tests)
