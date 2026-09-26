cmake_minimum_required(VERSION 3.16)
project(string_index_sorted_vector_adapter LANGUAGES CXX)

add_library(sorted_vector_adapter STATIC sorted_vector_adapter.cpp)
target_compile_features(sorted_vector_adapter PUBLIC cxx_std_17)
target_include_directories(sorted_vector_adapter PUBLIC
  "${CMAKE_CURRENT_SOURCE_DIR}")
if(DEFINED STRING_INDEX_CONTRACT_INCLUDE_DIR)
  target_include_directories(sorted_vector_adapter PUBLIC
    "${STRING_INDEX_CONTRACT_INCLUDE_DIR}")
else()
  target_include_directories(sorted_vector_adapter PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/../../src")
endif()
