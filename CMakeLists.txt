cmake_minimum_required(VERSION 3.16)
project(string_index_benchmark LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

set(TLI_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/upstream/TLI")
if(NOT EXISTS "${TLI_ROOT}/benchmark.h")
  message(FATAL_ERROR "Missing TLI sources. Run: python3 scripts/setup.py")
endif()
find_path(BOOST_HEADERS boost/chrono.hpp
  HINTS "${CMAKE_CURRENT_SOURCE_DIR}/.deps/boost/usr/include")
if(NOT BOOST_HEADERS)
  message(FATAL_ERROR "Missing Boost headers. Run: python3 scripts/setup.py")
endif()
find_package(Threads REQUIRED)

# Build only the string setup entrypoint; keep upstream's full build untouched.
add_executable(string_benchmark src/string_benchmark.cpp)
add_executable(generate "${TLI_ROOT}/generate.cc")
add_executable(benchmark_contract_test tests/benchmark_contract_test.cpp)
target_include_directories(benchmark_contract_test PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src")
target_include_directories(generate SYSTEM PRIVATE "${TLI_ROOT}/competitors/finedex/include")
foreach(target string_benchmark generate benchmark_contract_test)
  target_include_directories(${target} SYSTEM PRIVATE "${TLI_ROOT}" "${BOOST_HEADERS}")
  target_compile_definitions(${target} PRIVATE BOOST_CHRONO_HEADER_ONLY BOOST_ERROR_CODE_HEADER_ONLY)
  target_compile_options(${target} PRIVATE -march=native)
  target_link_libraries(${target} PRIVATE Threads::Threads)
endforeach()

include(CTest)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
add_test(NAME benchmark_contract_checks COMMAND benchmark_contract_test)
set_tests_properties(benchmark_contract_checks PROPERTIES TIMEOUT 30)
add_test(NAME lookup_workload_checks
  COMMAND "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/lookup_workload_test.py")
set_tests_properties(lookup_workload_checks PROPERTIES TIMEOUT 30)
add_test(NAME real_dataset_checks
  COMMAND "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/real_dataset_test.py")
set_tests_properties(real_dataset_checks PROPERTIES TIMEOUT 30)
add_test(NAME string_benchmark_smoke
  COMMAND "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/scripts/smoke.py"
    --build-dir "${CMAKE_CURRENT_BINARY_DIR}")
set_tests_properties(string_benchmark_smoke PROPERTIES TIMEOUT 180)
