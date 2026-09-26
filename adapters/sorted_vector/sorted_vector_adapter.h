#pragma once

#include "index_contract.h"

#include <memory>

std::unique_ptr<string_index_bench::v1::Index> make_sorted_vector_index(
    const string_index_bench::v1::Parameters& parameters);
