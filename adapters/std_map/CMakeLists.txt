cmake_minimum_required(VERSION 3.16)
project(string_index_std_map_adapter LANGUAGES CXX)

add_library(std_map_adapter STATIC std_map_adapter.cpp)
target_compile_features(std_map_adapter PUBLIC cxx_std_17)
target_include_directories(std_map_adapter PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")
if(DEFINED STRING_INDEX_CONTRACT_INCLUDE_DIR)
  target_include_directories(std_map_adapter PUBLIC
    "${STRING_INDEX_CONTRACT_INCLUDE_DIR}")
else()
  target_include_directories(std_map_adapter PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/../../src")
endif()
