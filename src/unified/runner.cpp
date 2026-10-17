#include "runner.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <condition_variable>
#include <cstdio>
#include <fstream>
#include <filesystem>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#if defined(SIB_ADAPTER_FACTORY_HEADER) != defined(SIB_ADAPTER_FACTORY_SYMBOL)
#error "SIB_ADAPTER_FACTORY_HEADER and SIB_ADAPTER_FACTORY_SYMBOL must be defined together"
#endif

#if defined(SIB_ADAPTER_FACTORY_HEADER)
#include SIB_ADAPTER_FACTORY_HEADER
#endif

namespace string_index_bench::unified {
namespace {

using Clock = std::chrono::steady_clock;

struct UnsignedByteLess {
  bool operator()(std::string_view left, std::string_view right) const {
    return std::lexicographical_compare(
        left.begin(), left.end(), right.begin(), right.end(),
        [](char a, char b) {
          return static_cast<unsigned char>(a) <
                 static_cast<unsigned char>(b);
        });
  }
};

using Oracle = std::map<std::string, v1::Value, UnsignedByteLess>;

[[noreturn]] void malformed(const std::string& message) {
  throw std::runtime_error("malformed interchange: " + message);
}

void read_exact(std::istream& input, char* destination, std::size_t size,
                const char* field) {
  std::size_t offset = 0;
  constexpr std::size_t kChunkSize = std::size_t{1} << 30U;
  while (offset < size) {
    const std::size_t chunk = std::min(kChunkSize, size - offset);
    input.read(destination + offset, static_cast<std::streamsize>(chunk));
    if (!input || static_cast<std::size_t>(input.gcount()) != chunk) {
      malformed(std::string("truncated ") + field);
    }
    offset += chunk;
  }
}

std::uint64_t read_u64(std::istream& input, const char* field) {
  std::array<unsigned char, 8> bytes{};
  read_exact(input, reinterpret_cast<char*>(bytes.data()), bytes.size(), field);
  std::uint64_t value = 0;
  for (std::size_t i = 0; i < bytes.size(); ++i) {
    value |= static_cast<std::uint64_t>(bytes[i]) << (8U * i);
  }
  return value;
}

std::uint64_t remaining_bytes(std::istream& input) {
  const std::istream::pos_type current = input.tellg();
  if (current == std::istream::pos_type(-1)) {
    malformed("input stream is not seekable");
  }
  input.seekg(0, std::ios::end);
  const std::istream::pos_type end = input.tellg();
  input.seekg(current);
  if (!input || end == std::istream::pos_type(-1) || end < current) {
    malformed("cannot determine remaining input size");
  }
  const auto remaining = static_cast<std::uintmax_t>(end - current);
  if (remaining > std::numeric_limits<std::uint64_t>::max()) {
    malformed("input exceeds supported size");
  }
  return static_cast<std::uint64_t>(remaining);
}

void require_record_capacity(std::uint64_t count, std::uint64_t bytes,
                             std::uint64_t minimum_record_size,
                             const char* field) {
  if (count > bytes / minimum_record_size) {
    malformed(std::string(field) + " exceeds remaining input bytes");
  }
}

std::string read_key(std::istream& input, const char* field,
                     std::uint64_t trailing_bytes) {
  const std::uint64_t length = read_u64(input, field);
  const std::uint64_t remaining = remaining_bytes(input);
  if (trailing_bytes > remaining || length > remaining - trailing_bytes) {
    malformed(std::string(field) + " length exceeds remaining input bytes");
  }
  if (length > static_cast<std::uint64_t>(
                   std::numeric_limits<std::size_t>::max())) {
    malformed(std::string(field) + " exceeds addressable size");
  }
  std::string key;
  if (length > static_cast<std::uint64_t>(key.max_size())) {
    malformed(std::string(field) + " exceeds string maximum size");
  }
  key.resize(static_cast<std::size_t>(length));
  read_exact(input, key.data(), key.size(), field);
  return key;
}

void check_magic(std::istream& input, const std::array<char, 8>& expected,
                 const char* kind) {
  std::array<char, 8> actual{};
  read_exact(input, actual.data(), actual.size(), "magic");
  if (actual != expected) {
    malformed(std::string("wrong ") + kind + " magic");
  }
}

void check_eof(std::istream& input) {
  const int next = input.peek();
  if (next != std::char_traits<char>::eof()) {
    malformed("trailing bytes");
  }
  if (input.bad()) {
    malformed("input read failure");
  }
}

const char* support_name(v1::Support support) {
  switch (support) {
    case v1::Support::unsupported:
      return "unsupported";
    case v1::Support::native:
      return "native";
    case v1::Support::adapted:
      return "adapted";
  }
  return "invalid";
}

bool valid_support(v1::Support support) {
  return support == v1::Support::unsupported ||
         support == v1::Support::native || support == v1::Support::adapted;
}

bool supported(v1::Support support) {
  return support == v1::Support::native || support == v1::Support::adapted;
}

const char* operation_name(OperationKind kind) {
  switch (kind) {
    case OperationKind::find:
      return "find";
    case OperationKind::insert:
      return "insert";
    case OperationKind::update:
      return "update";
    case OperationKind::erase:
      return "erase";
    case OperationKind::scan:
      return "scan";
  }
  return "unknown";
}

OperationCounts count_operations(const std::vector<Operation>& operations) {
  OperationCounts counts;
  for (const auto& operation : operations) {
    switch (operation.kind) {
      case OperationKind::find:
        ++counts.find;
        break;
      case OperationKind::insert:
        ++counts.insert;
        break;
      case OperationKind::update:
        ++counts.update;
        break;
      case OperationKind::erase:
        ++counts.erase;
        break;
      case OperationKind::scan:
        ++counts.scan;
        break;
    }
    ++counts.total;
  }
  return counts;
}

bool any_adapted(const v1::Capabilities& capabilities) {
  return capabilities.bulk_load == v1::Support::adapted ||
         capabilities.find == v1::Support::adapted ||
         capabilities.insert == v1::Support::adapted ||
         capabilities.update == v1::Support::adapted ||
         capabilities.erase == v1::Support::adapted ||
         capabilities.scan == v1::Support::adapted ||
         capabilities.range_sum == v1::Support::adapted;
}

bool same_key_domain(const v1::KeyDomain& left, const v1::KeyDomain& right) {
  return left.min_bytes == right.min_bytes &&
         left.max_bytes == right.max_bytes &&
         left.fixed_bytes == right.fixed_bytes &&
         left.allows_nul == right.allows_nul &&
         left.allows_high_bit == right.allows_high_bit &&
         left.allows_prefix_pairs == right.allows_prefix_pairs;
}

bool same_capabilities(const v1::Capabilities& left,
                       const v1::Capabilities& right) {
  return same_key_domain(left.keys, right.keys) &&
         left.max_value == right.max_value &&
         left.bulk_load == right.bulk_load && left.find == right.find &&
         left.insert == right.insert && left.update == right.update &&
         left.erase == right.erase && left.scan == right.scan &&
         left.range_sum == right.range_sum &&
         left.concurrent_find == right.concurrent_find;
}

bool same_descriptor(const v1::Descriptor& left,
                     const v1::Descriptor& right) {
  return left.implementation == right.implementation &&
         left.implementation_version == right.implementation_version &&
         left.adapter_version == right.adapter_version &&
         same_capabilities(left.capabilities, right.capabilities) &&
         left.adaptation_notes == right.adaptation_notes;
}

std::optional<std::string> validate_descriptor(
    const v1::Descriptor& descriptor) {
  if (descriptor.implementation.empty() ||
      descriptor.implementation_version.empty() ||
      descriptor.adapter_version.empty()) {
    return "descriptor identity and version fields must be non-empty";
  }
  const auto& capabilities = descriptor.capabilities;
  const std::array<v1::Support, 7> support_values = {
      capabilities.bulk_load, capabilities.find, capabilities.insert,
      capabilities.update,    capabilities.erase, capabilities.scan,
      capabilities.range_sum};
  if (!std::all_of(support_values.begin(), support_values.end(),
                   valid_support)) {
    return "descriptor contains an invalid support value";
  }
  if (!supported(capabilities.bulk_load) || !supported(capabilities.find)) {
    return "static lookup requires bulk_load and find support";
  }
  const auto& keys = capabilities.keys;
  if (keys.max_bytes && keys.min_bytes > *keys.max_bytes) {
    return "key domain minimum exceeds maximum";
  }
  if (keys.fixed_bytes && *keys.fixed_bytes < keys.min_bytes) {
    return "fixed key length is below the minimum";
  }
  if (keys.fixed_bytes && keys.max_bytes &&
      *keys.fixed_bytes > *keys.max_bytes) {
    return "fixed key length is above the maximum";
  }
  if (any_adapted(capabilities) && descriptor.adaptation_notes.empty()) {
    return "adapted operations require adaptation notes";
  }
  return std::nullopt;
}

std::optional<std::string> check_key(const v1::KeyDomain& domain,
                                     std::string_view key,
                                     const std::string& location) {
  const std::uint64_t length = static_cast<std::uint64_t>(key.size());
  if (length < domain.min_bytes) {
    return location + " is shorter than the declared key minimum";
  }
  if (domain.max_bytes && length > *domain.max_bytes) {
    return location + " is longer than the declared key maximum";
  }
  if (domain.fixed_bytes && length != *domain.fixed_bytes) {
    return location + " does not match the declared fixed key length";
  }
  if (!domain.allows_nul && key.find('\0') != std::string_view::npos) {
    return location + " contains NUL but the key domain rejects it";
  }
  if (!domain.allows_high_bit &&
      std::any_of(key.begin(), key.end(), [](char byte) {
        return static_cast<unsigned char>(byte) >= 0x80U;
      })) {
    return location + " contains a high-bit byte but the key domain rejects it";
  }
  return std::nullopt;
}

bool is_proper_prefix(std::string_view prefix, std::string_view value) {
  return prefix.size() < value.size() &&
         std::equal(prefix.begin(), prefix.end(), value.begin());
}

std::optional<std::string> validate_domain(
    const v1::Capabilities& capabilities, const std::vector<v1::Row>& rows,
    const std::vector<Query>& queries, std::vector<std::string>* unique_keys) {
  unique_keys->clear();
  unique_keys->reserve(rows.size() + queries.size());
  for (std::size_t i = 0; i < rows.size(); ++i) {
    if (auto error = check_key(capabilities.keys, rows[i].key,
                               "row key at index " + std::to_string(i))) {
      return error;
    }
    if (rows[i].value > capabilities.max_value) {
      return "row value at index " + std::to_string(i) +
             " exceeds the declared maximum";
    }
    unique_keys->push_back(rows[i].key);
  }
  for (std::size_t i = 0; i < queries.size(); ++i) {
    if (auto error = check_key(capabilities.keys, queries[i].key,
                               "query key at index " + std::to_string(i))) {
      return error;
    }
    if (queries[i].expected_present &&
        queries[i].expected_value > capabilities.max_value) {
      return "query value at index " + std::to_string(i) +
             " exceeds the declared maximum";
    }
    unique_keys->push_back(queries[i].key);
  }
  std::sort(unique_keys->begin(), unique_keys->end(), UnsignedByteLess{});
  unique_keys->erase(
      std::unique(unique_keys->begin(), unique_keys->end()),
      unique_keys->end());
  if (!capabilities.keys.allows_prefix_pairs) {
    for (std::size_t i = 1; i < unique_keys->size(); ++i) {
      if (is_proper_prefix((*unique_keys)[i - 1], (*unique_keys)[i])) {
        return "complete row/query domain contains a prefix key pair";
      }
    }
  }
  return std::nullopt;
}

std::optional<std::string> validate_mixed_domain(
    const v1::Capabilities& capabilities, const std::vector<v1::Row>& rows,
    const std::vector<Operation>& operations,
    std::vector<std::string>* unique_keys) {
  std::vector<Query> operation_keys;
  operation_keys.reserve(operations.size());
  for (const auto& operation : operations) {
    operation_keys.push_back({operation.key, false, 0});
  }
  if (auto error =
          validate_domain(capabilities, rows, operation_keys, unique_keys)) {
    return error;
  }
  for (std::size_t i = 0; i < operations.size(); ++i) {
    const auto& operation = operations[i];
    if ((operation.kind == OperationKind::insert ||
         operation.kind == OperationKind::update) &&
        operation.value > capabilities.max_value) {
      return "operation value at index " + std::to_string(i) +
             " exceeds the declared maximum";
    }
    if (operation.kind == OperationKind::scan &&
        operation.limit >
            static_cast<std::uint64_t>(
                std::numeric_limits<std::size_t>::max())) {
      return "scan limit at index " + std::to_string(i) +
             " exceeds the native size range";
    }
  }
  return std::nullopt;
}

Oracle make_oracle(const std::vector<v1::Row>& rows) {
  Oracle oracle;
  for (std::size_t i = 0; i < rows.size(); ++i) {
    if (i != 0 && !UnsignedByteLess{}(rows[i - 1].key, rows[i].key)) {
      throw std::runtime_error(
          "malformed interchange: rows are not strictly increasing and unique "
          "under unsigned-byte ordering at index " +
          std::to_string(i));
    }
    oracle.emplace(rows[i].key, rows[i].value);
  }
  return oracle;
}

std::vector<unsigned char> allowed_bytes(const v1::KeyDomain& domain) {
  std::vector<unsigned char> bytes;
  for (unsigned int value = 0; value <= 0xffU; ++value) {
    if ((!domain.allows_nul && value == 0) ||
        (!domain.allows_high_bit && value >= 0x80U)) {
      continue;
    }
    bytes.push_back(static_cast<unsigned char>(value));
  }
  return bytes;
}

std::uint64_t saturated_power(std::uint64_t base, std::uint64_t exponent,
                              std::uint64_t limit) {
  std::uint64_t result = 1;
  while (exponent != 0) {
    if ((exponent & 1U) != 0) {
      if (base != 0 && result > limit / base) {
        return limit;
      }
      result *= base;
    }
    exponent >>= 1U;
    if (exponent != 0) {
      if (base != 0 && base > limit / base) {
        base = limit;
      } else {
        base *= base;
      }
    }
  }
  return std::min(result, limit);
}

std::optional<std::uint64_t> finite_domain_size_up_to(
    const v1::KeyDomain& domain, std::uint64_t limit) {
  const std::uint64_t alphabet = allowed_bytes(domain).size();
  if (domain.fixed_bytes) {
    return saturated_power(alphabet, *domain.fixed_bytes, limit);
  }
  if (!domain.max_bytes) {
    return std::nullopt;
  }
  std::uint64_t term = saturated_power(alphabet, domain.min_bytes, limit);
  std::uint64_t total = term;
  std::uint64_t length = domain.min_bytes;
  while (length < *domain.max_bytes && total < limit) {
    ++length;
    if (term > limit / alphabet) {
      term = limit;
    } else {
      term *= alphabet;
    }
    total = term >= limit - total ? limit : total + term;
  }
  return total;
}

bool advance_candidate(std::string* candidate,
                       const std::vector<unsigned char>& alphabet,
                       std::size_t prefix_length =
                           std::numeric_limits<std::size_t>::max()) {
  if (candidate->empty()) {
    return false;
  }
  const std::size_t length = std::min(prefix_length, candidate->size());
  for (std::size_t position = length; position != 0; --position) {
    const auto found = std::lower_bound(
        alphabet.begin(), alphabet.end(),
        static_cast<unsigned char>((*candidate)[position - 1]));
    if (found != alphabet.end() && std::next(found) != alphabet.end()) {
      const auto next = std::next(found);
      (*candidate)[position - 1] = static_cast<char>(*next);
      std::fill(candidate->begin() + static_cast<std::ptrdiff_t>(position),
                candidate->end(), static_cast<char>(alphabet.front()));
      return true;
    }
  }
  return false;
}

std::optional<std::string> find_absent_probe(
    const v1::KeyDomain& domain, const Oracle& oracle,
    const std::vector<std::string>& complete_domain) {
  const auto alphabet = allowed_bytes(domain);
  std::vector<std::uint64_t> lengths;
  if (domain.fixed_bytes) {
    lengths.push_back(*domain.fixed_bytes);
  } else {
    lengths.push_back(domain.min_bytes);
    std::uint64_t maximum_observed = 0;
    for (const auto& key : complete_domain) {
      maximum_observed = std::max(
          maximum_observed, static_cast<std::uint64_t>(key.size()));
      lengths.push_back(static_cast<std::uint64_t>(key.size()));
    }
    if (maximum_observed != std::numeric_limits<std::uint64_t>::max()) {
      lengths.push_back(std::max(domain.min_bytes, maximum_observed + 1));
    }
    if (domain.max_bytes) {
      lengths.push_back(*domain.max_bytes);
    }
  }
  std::sort(lengths.begin(), lengths.end());
  lengths.erase(std::unique(lengths.begin(), lengths.end()), lengths.end());

  for (const std::uint64_t length64 : lengths) {
    if (length64 < domain.min_bytes ||
        (domain.max_bytes && length64 > *domain.max_bytes) ||
        (domain.fixed_bytes && length64 != *domain.fixed_bytes) ||
        length64 > std::numeric_limits<std::size_t>::max()) {
      continue;
    }
    std::string candidate;
    try {
      if (length64 > static_cast<std::uint64_t>(candidate.max_size())) {
        continue;
      }
      candidate.assign(static_cast<std::size_t>(length64),
                       static_cast<char>(alphabet.front()));
    } catch (const std::bad_alloc&) {
      throw std::runtime_error(
          "harness cannot materialize an absent key in the declared domain");
    } catch (const std::length_error&) {
      continue;
    }

    const std::size_t attempt_limit =
        complete_domain.size() >
                (std::numeric_limits<std::size_t>::max() - 32) / 4
            ? std::numeric_limits<std::size_t>::max()
            : complete_domain.size() * 4 + 32;
    for (std::size_t attempt = 0; attempt < attempt_limit; ++attempt) {
      bool skipped_prefix = false;
      bool candidate_is_prefix = false;
      if (!domain.allows_prefix_pairs) {
        for (const auto& key : complete_domain) {
          if (is_proper_prefix(key, candidate)) {
            if (!advance_candidate(&candidate, alphabet, key.size())) {
              attempt = attempt_limit;
            }
            skipped_prefix = true;
            break;
          }
          if (is_proper_prefix(candidate, key)) {
            candidate_is_prefix = true;
          }
        }
        if (skipped_prefix) {
          continue;
        }
      }
      if (!candidate_is_prefix && oracle.find(candidate) == oracle.end()) {
        return candidate;
      }
      if (!advance_candidate(&candidate, alphabet)) {
        break;
      }
    }
  }
  return std::nullopt;
}

void validate_query_expectations(const Oracle& oracle,
                                 const std::vector<Query>& queries) {
  for (std::size_t i = 0; i < queries.size(); ++i) {
    const auto found = oracle.find(queries[i].key);
    const bool present = found != oracle.end();
    if (present != queries[i].expected_present) {
      throw std::runtime_error(
          "malformed interchange: expected presence disagrees with independent "
          "oracle at query index " +
          std::to_string(i));
    }
    if (present && found->second != queries[i].expected_value) {
      throw std::runtime_error(
          "malformed interchange: expected value disagrees with independent "
          "oracle at query index " +
          std::to_string(i));
    }
  }
}

std::unique_ptr<v1::Index> make_index(v1::Factory factory,
                                      const v1::Parameters& parameters,
                                      const std::string& location) {
  if (factory == nullptr) {
    throw std::runtime_error("adapter factory is null");
  }
  std::unique_ptr<v1::Index> index;
  try {
    index = factory(parameters);
  } catch (...) {
    throw std::runtime_error("adapter factory call failed at " + location);
  }
  if (!index) {
    throw std::runtime_error("adapter factory returned null at " + location);
  }
  return index;
}

template <class Function>
auto adapter_call(const std::string& call, const std::string& location,
                  Function&& function) -> decltype(function()) {
  try {
    return function();
  } catch (...) {
    throw std::runtime_error("adapter " + call + " call failed at " +
                             location);
  }
}

template <class Function>
auto adapter_indexed_call(const char* call, const char* location,
                          std::size_t index, Function&& function)
    -> decltype(function()) {
  try {
    return function();
  } catch (...) {
    throw std::runtime_error(std::string("adapter ") + call +
                             " call failed at " + location + " index " +
                             std::to_string(index));
  }
}

template <class Function>
auto adapter_repeated_call(const char* call, const char* location,
                           std::size_t repeat, std::size_t index,
                           Function&& function) -> decltype(function()) {
  try {
    return function();
  } catch (...) {
    throw std::runtime_error(std::string("adapter ") + call +
                             " call failed at " + location + " repeat " +
                             std::to_string(repeat) + " index " +
                             std::to_string(index));
  }
}

template <class Function>
auto adapter_operation_call(OperationKind kind, const char* stage,
                            std::size_t ordinal, Function&& function)
    -> decltype(function()) {
  try {
    return function();
  } catch (...) {
    throw std::runtime_error(std::string("adapter ") + operation_name(kind) +
                             " call failed at " + stage +
                             " operation ordinal " +
                             std::to_string(ordinal));
  }
}

template <class Function>
auto adapter_timed_operation_call(OperationKind kind, std::size_t repeat,
                                  std::size_t ordinal, Function&& function)
    -> decltype(function()) {
  try {
    return function();
  } catch (...) {
    throw std::runtime_error(std::string("adapter ") + operation_name(kind) +
                             " call failed at throughput repeat " +
                             std::to_string(repeat) +
                             " operation ordinal " +
                             std::to_string(ordinal));
  }
}

void require_stable_descriptor(const v1::Descriptor& expected,
                               const v1::Descriptor& actual) {
  if (!same_descriptor(expected, actual)) {
    throw std::runtime_error(
        "descriptor changed between fresh instances or across build");
  }
}

void require_find(const std::optional<v1::Value>& actual, bool expected_present,
                  v1::Value expected_value, const std::string& location) {
  if (actual.has_value() != expected_present) {
    throw std::runtime_error(location +
                             (expected_present ? " produced a false miss"
                                               : " produced a false hit"));
  }
  if (expected_present && *actual != expected_value) {
    throw std::runtime_error(location + " returned the wrong value");
  }
}

void validate_memory(const v1::MemoryUsage& memory) {
  if (memory.accounting_notes.empty()) {
    throw std::runtime_error("memory accounting notes must be non-empty");
  }
}

std::uint64_t elapsed_ns(Clock::time_point start, Clock::time_point end) {
  const auto duration =
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
  return duration <= 0 ? 0 : static_cast<std::uint64_t>(duration);
}

void consume(const std::optional<v1::Value>& value, std::uint64_t* checksum) {
  const std::uint64_t token =
      value ? (*value ^ UINT64_C(0x9e3779b97f4a7c15))
            : UINT64_C(0xd1b54a32d192ed03);
  *checksum ^= token + UINT64_C(0x9e3779b97f4a7c15) + (*checksum << 6U) +
               (*checksum >> 2U);
}

void consume_u64(std::uint64_t value, std::uint64_t* checksum) {
  consume(std::optional<v1::Value>(value), checksum);
}

void consume_rows(const std::vector<v1::Row>& rows,
                  std::uint64_t* checksum) {
  consume_u64(static_cast<std::uint64_t>(rows.size()), checksum);
  for (const auto& row : rows) {
    consume_u64(static_cast<std::uint64_t>(row.key.size()), checksum);
    for (unsigned char byte : row.key) {
      consume_u64(byte, checksum);
    }
    consume_u64(row.value, checksum);
  }
}

struct ParallelWorkerResult {
  WorkerSample sample;
  std::string error;
};

struct ParallelRunResult {
  std::uint64_t lookup_ns = 0;
  std::vector<WorkerSample> workers;
};

std::pair<std::size_t, std::size_t> balanced_partition(
    std::size_t operations, std::size_t workers, std::size_t worker) {
  const std::size_t quotient = operations / workers;
  const std::size_t remainder = operations % workers;
  const std::size_t begin =
      worker * quotient + std::min(worker, remainder);
  const std::size_t count = quotient + (worker < remainder ? 1 : 0);
  return {begin, begin + count};
}

ParallelRunResult run_synchronized_workers(
    const v1::Index& index, const std::vector<Query>& queries,
    const Oracle* oracle, std::size_t worker_count,
    const std::string& phase) {
  struct Gate {
    std::mutex mutex;
    std::condition_variable condition;
    std::size_t ready = 0;
    std::size_t completed = 0;
    bool released = false;
    bool cancelled = false;
  } gate;

  std::vector<ParallelWorkerResult> results(worker_count);
  std::vector<std::thread> workers;
  workers.reserve(worker_count);
  try {
    for (std::size_t worker = 0; worker < worker_count; ++worker) {
      const auto bounds =
          balanced_partition(queries.size(), worker_count, worker);
      results[worker].sample.worker = worker;
      results[worker].sample.operations = bounds.second - bounds.first;
      workers.emplace_back([&, worker, bounds] {
        {
          std::unique_lock<std::mutex> lock(gate.mutex);
          ++gate.ready;
          gate.condition.notify_all();
          gate.condition.wait(
              lock, [&] { return gate.released || gate.cancelled; });
          if (gate.cancelled) {
            return;
          }
        }

        const auto worker_start = Clock::now();
        std::uint64_t worker_checksum = 0;
        for (std::size_t query_index = bounds.first;
             query_index < bounds.second; ++query_index) {
          std::optional<v1::Value> value;
          try {
            value = index.find(queries[query_index].key);
          } catch (...) {
            results[worker].error =
                "adapter find call failed at " + phase + " worker " +
                std::to_string(worker) + " index " +
                std::to_string(query_index);
            break;
          }

          if (oracle != nullptr) {
            const auto expected = oracle->find(queries[query_index].key);
            const bool expected_present = expected != oracle->end();
            if (value.has_value() != expected_present) {
              results[worker].error =
                  phase + " worker " + std::to_string(worker) + " index " +
                  std::to_string(query_index) +
                  (expected_present ? " produced a false miss"
                                    : " produced a false hit");
              break;
            }
            if (expected_present && *value != expected->second) {
              results[worker].error =
                  phase + " worker " + std::to_string(worker) + " index " +
                  std::to_string(query_index) + " returned the wrong value";
              break;
            }
          }
          consume(value, &worker_checksum);
        }
        const auto worker_end = Clock::now();
        results[worker].sample.checksum = worker_checksum;
        results[worker].sample.lookup_ns =
            elapsed_ns(worker_start, worker_end);
        {
          std::lock_guard<std::mutex> lock(gate.mutex);
          ++gate.completed;
        }
        gate.condition.notify_all();
      });
    }
  } catch (...) {
    {
      std::lock_guard<std::mutex> lock(gate.mutex);
      gate.cancelled = true;
    }
    gate.condition.notify_all();
    for (auto& worker : workers) {
      if (worker.joinable()) {
        worker.join();
      }
    }
    throw std::runtime_error("worker launch failed at " + phase);
  }

  Clock::time_point lookup_start;
  Clock::time_point lookup_end;
  {
    std::unique_lock<std::mutex> lock(gate.mutex);
    gate.condition.wait(lock, [&] { return gate.ready == worker_count; });
    lookup_start = Clock::now();
    gate.released = true;
    gate.condition.notify_all();
    gate.condition.wait(lock,
                        [&] { return gate.completed == worker_count; });
    lookup_end = Clock::now();
  }
  for (auto& worker : workers) {
    worker.join();
  }

  for (const auto& result : results) {
    if (!result.error.empty()) {
      throw std::runtime_error(result.error);
    }
  }

  ParallelRunResult run;
  run.lookup_ns = elapsed_ns(lookup_start, lookup_end);
  run.workers.reserve(worker_count);
  for (auto& result : results) {
    run.workers.push_back(std::move(result.sample));
  }
  return run;
}

std::string json_escape(std::string_view value) {
  std::ostringstream output;
  output << '"';
  constexpr char hex[] = "0123456789abcdef";
  for (unsigned char byte : value) {
    switch (byte) {
      case '"':
        output << "\\\"";
        break;
      case '\\':
        output << "\\\\";
        break;
      case '\b':
        output << "\\b";
        break;
      case '\f':
        output << "\\f";
        break;
      case '\n':
        output << "\\n";
        break;
      case '\r':
        output << "\\r";
        break;
      case '\t':
        output << "\\t";
        break;
      default:
        if (byte < 0x20U || byte >= 0x7fU) {
          output << "\\u00" << hex[byte >> 4U] << hex[byte & 0x0fU];
        } else {
          output << static_cast<char>(byte);
        }
    }
  }
  output << '"';
  return output.str();
}

void write_optional_u64(std::ostream& output,
                        const std::optional<std::uint64_t>& value) {
  if (value) {
    output << *value;
  } else {
    output << "null";
  }
}

std::size_t parse_count(const std::string& text, const char* option,
                        bool allow_zero) {
  if (text.empty() || text[0] == '-') {
    throw std::invalid_argument(std::string(option) +
                                " must be an unsigned integer");
  }
  std::size_t consumed = 0;
  unsigned long long value = 0;
  try {
    value = std::stoull(text, &consumed, 10);
  } catch (const std::exception&) {
    throw std::invalid_argument(std::string(option) +
                                " must be an unsigned integer");
  }
  if (consumed != text.size() ||
      value > std::numeric_limits<std::size_t>::max()) {
    throw std::invalid_argument(std::string(option) +
                                " is outside the supported range");
  }
  if (!allow_zero && value == 0) {
    throw std::invalid_argument(std::string(option) + " must be positive");
  }
  return static_cast<std::size_t>(value);
}

std::filesystem::path canonical_cli_path(const std::string& path,
                                         const char* role) {
  std::error_code error;
  const auto canonical = std::filesystem::weakly_canonical(path, error);
  if (error) {
    throw std::invalid_argument(std::string("cannot resolve ") + role +
                                " path");
  }
  return canonical;
}

void preflight_cli_paths(const std::map<std::string, std::string>& options,
                         const std::string& workload_option) {
  for (const char* option : {"--result", "--samples"}) {
    std::error_code error;
    const bool exists = std::filesystem::exists(options.at(option), error);
    if (error) {
      throw std::invalid_argument(std::string("cannot inspect ") + option +
                                  " path");
    }
    if (exists) {
      throw std::invalid_argument(std::string(option) +
                                  " path already exists");
    }
  }

  const auto rows = canonical_cli_path(options.at("--rows"), "rows input");
  const auto workload = canonical_cli_path(
      options.at(workload_option),
      workload_option == "--queries" ? "queries input" : "operations input");
  const auto result =
      canonical_cli_path(options.at("--result"), "result output");
  const auto samples =
      canonical_cli_path(options.at("--samples"), "samples output");
  if (result == samples) {
    throw std::invalid_argument("result and samples paths collide");
  }
  if (result == rows || result == workload || samples == rows ||
      samples == workload) {
    throw std::invalid_argument("an output path collides with an input path");
  }
}

bool ascii_alpha(char value) {
  return (value >= 'A' && value <= 'Z') ||
         (value >= 'a' && value <= 'z');
}

bool ascii_alnum(char value) {
  return ascii_alpha(value) || (value >= '0' && value <= '9');
}

std::pair<std::string, std::string> parse_parameter_argument(
    const std::string& argument) {
  const std::size_t separator = argument.find('=');
  if (separator == std::string::npos) {
    throw std::invalid_argument(
        "--parameter must have the form NAME=VALUE");
  }
  const std::string name = argument.substr(0, separator);
  if (name.empty() || name.size() > 128 || !ascii_alpha(name.front()) ||
      !std::all_of(name.begin() + 1, name.end(), [](char value) {
        return ascii_alnum(value) || value == '_' || value == '.' ||
               value == '-';
      })) {
    throw std::invalid_argument("invalid --parameter name");
  }
  return {name, argument.substr(separator + 1)};
}

void write_exclusive_file(const std::string& path, const std::string& contents,
                          const char* role) {
  std::FILE* output = std::fopen(path.c_str(), "wx");
  if (output == nullptr) {
    throw std::runtime_error(std::string("cannot exclusively create ") + role +
                             " output");
  }
  const bool wrote =
      contents.empty() ||
      std::fwrite(contents.data(), 1, contents.size(), output) ==
          contents.size();
  const bool closed = std::fclose(output) == 0;
  if (!wrote || !closed) {
    throw std::runtime_error(std::string("cannot write ") + role + " output");
  }
}

}  // namespace

std::vector<v1::Row> read_rows(std::istream& input) {
  check_magic(input, {'S', 'I', 'B', 'R', 'O', 'W', '1', '\0'}, "rows");
  const std::uint64_t count = read_u64(input, "row count");
  const std::uint64_t bytes = remaining_bytes(input);
  constexpr std::uint64_t kMinimumRowBytes = 16;
  require_record_capacity(count, bytes, kMinimumRowBytes, "row count");
  std::vector<v1::Row> rows;
  for (std::uint64_t i = 0; i < count; ++i) {
    v1::Row row;
    const std::uint64_t records_after = count - i - 1;
    row.key = read_key(input, "row key",
                       8 + records_after * kMinimumRowBytes);
    row.value = read_u64(input, "row value");
    rows.push_back(std::move(row));
  }
  check_eof(input);
  // Sorting is part of the interchange contract, not adapter behavior.
  (void)make_oracle(rows);
  return rows;
}

std::vector<Query> read_queries(std::istream& input) {
  check_magic(input, {'S', 'I', 'B', 'Q', 'R', 'Y', '1', '\0'}, "queries");
  const std::uint64_t count = read_u64(input, "query count");
  const std::uint64_t bytes = remaining_bytes(input);
  constexpr std::uint64_t kMinimumQueryBytes = 17;
  require_record_capacity(count, bytes, kMinimumQueryBytes, "query count");
  std::vector<Query> queries;
  for (std::uint64_t i = 0; i < count; ++i) {
    char presence = 0;
    read_exact(input, &presence, 1, "query expected-presence");
    if (presence != 0 && presence != 1) {
      malformed("query expected-presence must be 0 or 1 at index " +
                std::to_string(i));
    }
    Query query;
    query.expected_present = presence == 1;
    query.expected_value = read_u64(input, "query expected-value");
    const std::uint64_t records_after = count - i - 1;
    query.key = read_key(input, "query key",
                         records_after * kMinimumQueryBytes);
    queries.push_back(std::move(query));
  }
  check_eof(input);
  return queries;
}

std::vector<Operation> read_operations(std::istream& input) {
  check_magic(input, {'S', 'I', 'B', 'O', 'P', 'S', '1', '\0'},
              "operations");
  const std::uint64_t count = read_u64(input, "operation count");
  const std::uint64_t bytes = remaining_bytes(input);
  constexpr std::uint64_t kMinimumOperationBytes = 25;
  require_record_capacity(count, bytes, kMinimumOperationBytes,
                          "operation count");
  std::vector<Operation> operations;
  for (std::uint64_t i = 0; i < count; ++i) {
    char raw_kind = 0;
    read_exact(input, &raw_kind, 1, "operation kind");
    const auto kind_value = static_cast<unsigned char>(raw_kind);
    if (kind_value < static_cast<unsigned char>(OperationKind::find) ||
        kind_value > static_cast<unsigned char>(OperationKind::scan)) {
      malformed("unknown operation kind at index " + std::to_string(i));
    }
    Operation operation;
    operation.kind = static_cast<OperationKind>(kind_value);
    operation.value = read_u64(input, "operation value");
    operation.limit = read_u64(input, "operation limit");
    if (operation.kind != OperationKind::insert &&
        operation.kind != OperationKind::update && operation.value != 0) {
      malformed("unused operation value must be zero at index " +
                std::to_string(i));
    }
    if (operation.kind != OperationKind::scan && operation.limit != 0) {
      malformed("unused operation limit must be zero at index " +
                std::to_string(i));
    }
    const std::uint64_t records_after = count - i - 1;
    operation.key = read_key(input, "operation key",
                             records_after * kMinimumOperationBytes);
    operations.push_back(std::move(operation));
  }
  check_eof(input);
  return operations;
}

std::vector<v1::Row> read_rows_file(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open rows input");
  }
  return read_rows(input);
}

std::vector<Query> read_queries_file(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open queries input");
  }
  return read_queries(input);
}

std::vector<Operation> read_operations_file(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open operations input");
  }
  return read_operations(input);
}

RunReport run_static_lookup(v1::Factory factory,
                            const v1::Parameters& parameters,
                            const std::vector<v1::Row>& rows,
                            const std::vector<Query>& queries,
                            std::size_t repeats,
                            std::size_t latency_sample_count,
                            std::size_t threads) {
  RunReport report;
  report.parameters = parameters;
  report.execution.threads = threads;
  report.execution.mode =
      threads == 1 ? "serial" : "shared-index-read-only";
  if (threads == 0 || threads > 256) {
    report.status = "invalid_input";
    report.stage = "execution";
    report.reason = "threads must be in the range 1..256";
    return report;
  }
  if (!queries.empty() && threads > queries.size()) {
    report.status = "invalid_input";
    report.stage = "execution";
    report.reason = "threads must not exceed the nonempty query count";
    return report;
  }
  if (repeats == 0) {
    report.stage = "configuration";
    report.reason = "repeats must be positive";
    return report;
  }

  Oracle oracle;
  try {
    report.stage = "interchange";
    oracle = make_oracle(rows);
    validate_query_expectations(oracle, queries);

    report.stage = "descriptor";
    auto descriptor_instance =
        make_index(factory, parameters, "descriptor instance");
    const v1::Descriptor descriptor = adapter_call(
        "descriptor", "descriptor instance",
        [&] { return descriptor_instance->descriptor(); });
    report.descriptor = descriptor;
    if (auto error = validate_descriptor(descriptor)) {
      report.status = "invalid_input";
      report.reason = *error;
      return report;
    }
    report.validation.descriptor_checked = true;

    if (threads > 1 && !descriptor.capabilities.concurrent_find) {
      report.status = "unsupported";
      report.stage = "capability";
      report.reason =
          "adapter does not declare shared-index concurrent find support";
      return report;
    }

    std::vector<std::string> unique_keys;
    if (auto error = validate_domain(descriptor.capabilities, rows, queries,
                                     &unique_keys)) {
      report.status = "unsupported";
      report.stage = "capability";
      report.reason = *error;
      return report;
    }
    report.validation.capability_domain_checked = true;
    descriptor_instance.reset();

    report.stage = "correctness";
    auto empty_index = make_index(factory, parameters, "empty-build instance");
    require_stable_descriptor(
        descriptor, adapter_call("descriptor", "empty-build instance before build",
                                 [&] { return empty_index->descriptor(); }));
    const std::vector<v1::Row> empty_rows;
    adapter_call("bulk_load", "empty-build instance",
                 [&] { empty_index->bulk_load(empty_rows); });
    require_stable_descriptor(
        descriptor, adapter_call("descriptor", "empty-build instance after build",
                                 [&] { return empty_index->descriptor(); }));
    for (std::size_t i = 0; i < unique_keys.size(); ++i) {
      require_find(adapter_indexed_call(
                       "find", "empty-build domain", i,
                       [&] { return empty_index->find(unique_keys[i]); }),
                   false, 0,
                   "empty build probe at domain index " + std::to_string(i));
    }
    report.validation.empty_build_checked = true;
    empty_index.reset();

    auto correctness_index =
        make_index(factory, parameters, "populated correctness instance");
    require_stable_descriptor(
        descriptor,
        adapter_call("descriptor", "populated instance before build",
                     [&] { return correctness_index->descriptor(); }));
    adapter_call("bulk_load", "populated correctness instance",
                 [&] { correctness_index->bulk_load(rows); });
    require_stable_descriptor(
        descriptor,
        adapter_call("descriptor", "populated instance after build",
                     [&] { return correctness_index->descriptor(); }));
    for (std::size_t i = 0; i < rows.size(); ++i) {
      require_find(adapter_indexed_call(
                       "find", "represented row", i,
                       [&] { return correctness_index->find(rows[i].key); }),
                   true, rows[i].value,
                   "represented row at index " + std::to_string(i));
      ++report.validation.rows_checked;
    }
    bool workload_absent_key_checked = false;
    for (std::size_t i = 0; i < queries.size(); ++i) {
      require_find(adapter_indexed_call(
                       "find", "query", i,
                       [&] { return correctness_index->find(queries[i].key); }),
                   queries[i].expected_present, queries[i].expected_value,
                   "query at index " + std::to_string(i));
      workload_absent_key_checked =
          workload_absent_key_checked || !queries[i].expected_present;
      ++report.validation.queries_checked;
    }
    if (workload_absent_key_checked) {
      report.validation.absent_key_checked = true;
    } else {
      const std::uint64_t domain_limit =
          rows.size() >= std::numeric_limits<std::uint64_t>::max()
              ? std::numeric_limits<std::uint64_t>::max()
              : static_cast<std::uint64_t>(rows.size()) + 1;
      const auto finite_size = finite_domain_size_up_to(
          descriptor.capabilities.keys, domain_limit);
      if (finite_size && *finite_size == rows.size()) {
        report.validation.finite_domain_full = true;
      } else {
        auto absent = find_absent_probe(descriptor.capabilities.keys, oracle,
                                        unique_keys);
        if (!absent) {
          throw std::runtime_error(
              "harness could not validate an absent key in the declared domain");
        }
        require_find(adapter_call(
                         "find", "populated absent-key probe",
                         [&] { return correctness_index->find(*absent); }),
                     false, 0, "populated absent-key probe");
        report.validation.absent_key_checked = true;
      }
    }
    if (!rows.empty()) {
      require_find(adapter_indexed_call(
                       "find", "represented boundary", 0,
                       [&] { return correctness_index->find(rows.front().key); }),
                   true,
                   rows.front().value, "first represented-key boundary");
      require_find(adapter_indexed_call(
                       "find", "represented boundary", rows.size() - 1,
                       [&] { return correctness_index->find(rows.back().key); }),
                   true,
                   rows.back().value, "last represented-key boundary");
    }
    report.validation.represented_boundaries_checked = true;
    report.memory = adapter_call(
        "memory_usage", "populated correctness instance",
        [&] { return correctness_index->memory_usage(); });
    validate_memory(*report.memory);
    correctness_index.reset();

    if (queries.empty()) {
      report.status = "skipped";
      report.stage = "measurement";
      report.reason = "query workload is empty";
      return report;
    }

    if (threads > 1) {
      auto concurrent_index =
          make_index(factory, parameters, "concurrent correctness instance");
      require_stable_descriptor(
          descriptor,
          adapter_call("descriptor", "concurrent correctness instance before build",
                       [&] { return concurrent_index->descriptor(); }));
      adapter_call("bulk_load", "concurrent correctness instance",
                   [&] { concurrent_index->bulk_load(rows); });
      require_stable_descriptor(
          descriptor,
          adapter_call("descriptor", "concurrent correctness instance after build",
                       [&] { return concurrent_index->descriptor(); }));
      (void)run_synchronized_workers(*concurrent_index, queries, &oracle,
                                     threads, "concurrent correctness query");
      report.validation.concurrent_find_checked = true;
      report.validation.concurrent_queries_checked = queries.size();
      concurrent_index.reset();
    }

    report.samples.reserve(repeats);
    const std::size_t measured_latency_count =
        std::min(latency_sample_count, queries.size());
    if (measured_latency_count != 0 &&
        repeats <= std::numeric_limits<std::size_t>::max() /
                       measured_latency_count) {
      report.latency_samples.reserve(repeats * measured_latency_count);
    }
    for (std::size_t repeat = 0; repeat < repeats; ++repeat) {
      report.stage = "throughput";
      const auto build_start = Clock::now();
      auto throughput_index = make_index(
          factory, parameters,
          "throughput repeat " + std::to_string(repeat));
      adapter_call("bulk_load", "throughput repeat " + std::to_string(repeat),
                   [&] { throughput_index->bulk_load(rows); });
      const auto build_end = Clock::now();
      require_stable_descriptor(
          descriptor,
          adapter_call("descriptor",
                       "throughput repeat " + std::to_string(repeat),
                       [&] { return throughput_index->descriptor(); }));

      ThroughputSample sample;
      sample.repeat = repeat;
      sample.build_ns = elapsed_ns(build_start, build_end);
      sample.operations = queries.size();
      if (threads == 1) {
        WorkerSample worker;
        worker.worker = 0;
        worker.operations = queries.size();
        const auto lookup_start = Clock::now();
        for (std::size_t i = 0; i < queries.size(); ++i) {
          consume(adapter_repeated_call(
                      "find", "throughput query", repeat, i,
                      [&] { return throughput_index->find(queries[i].key); }),
                  &worker.checksum);
        }
        const auto lookup_end = Clock::now();
        worker.lookup_ns = elapsed_ns(lookup_start, lookup_end);
        sample.lookup_ns = worker.lookup_ns;
        sample.workers.push_back(worker);
      } else {
        ParallelRunResult parallel = run_synchronized_workers(
            *throughput_index, queries, nullptr, threads,
            "throughput repeat " + std::to_string(repeat));
        sample.lookup_ns = parallel.lookup_ns;
        sample.workers = std::move(parallel.workers);
      }
      for (const auto& worker : sample.workers) {
        consume(worker.checksum, &report.checksum);
      }
      if (sample.lookup_ns == 0) {
        throw std::runtime_error(
            "throughput lookup duration is below clock resolution");
      }
      sample.throughput_ops_per_second =
          static_cast<double>(sample.operations) * 1.0e9 /
          static_cast<double>(sample.lookup_ns);
      const v1::MemoryUsage memory = adapter_call(
          "memory_usage", "throughput repeat " + std::to_string(repeat),
          [&] { return throughput_index->memory_usage(); });
      validate_memory(memory);
      if (repeat == 0) {
        report.memory = memory;
      }
      throughput_index.reset();

      if (measured_latency_count != 0) {
        report.stage = "latency";
        const auto latency_build_start = Clock::now();
        auto latency_index = make_index(
            factory, parameters,
            "latency repeat " + std::to_string(repeat));
        adapter_call("bulk_load", "latency repeat " + std::to_string(repeat),
                     [&] { latency_index->bulk_load(rows); });
        const auto latency_build_end = Clock::now();
        sample.latency_build_ns =
            elapsed_ns(latency_build_start, latency_build_end);
        require_stable_descriptor(
            descriptor,
            adapter_call("descriptor",
                         "latency repeat " + std::to_string(repeat),
                         [&] { return latency_index->descriptor(); }));
        const std::size_t quotient =
            queries.size() / measured_latency_count;
        const std::size_t remainder =
            queries.size() % measured_latency_count;
        for (std::size_t i = 0; i < measured_latency_count; ++i) {
          const std::size_t query_index =
              i * quotient + std::min(i, remainder);
          const auto start = Clock::now();
          const auto value = adapter_repeated_call(
              "find", "latency sample", repeat, i,
              [&] { return latency_index->find(queries[query_index].key); });
          const auto end = Clock::now();
          consume(value, &report.checksum);
          report.latency_samples.push_back(
              {repeat, i, query_index, elapsed_ns(start, end)});
        }
        latency_index.reset();
      }
      report.samples.push_back(sample);
    }

    report.status = "passed";
    report.stage = "complete";
    report.reason.clear();
    return report;
  } catch (const std::exception& error) {
    report.status = report.stage == "interchange" ? "invalid_input" : "failed";
    report.reason = error.what();
    report.samples.clear();
    report.latency_samples.clear();
    report.checksum = 0;
    return report;
  } catch (...) {
    report.status = "failed";
    report.reason = "unknown exception";
    report.samples.clear();
    report.latency_samples.clear();
    report.checksum = 0;
    return report;
  }
}

MixedRunReport run_mixed_operations(
    v1::Factory factory, const v1::Parameters& parameters,
    const std::vector<v1::Row>& rows,
    const std::vector<Operation>& operations, std::size_t repeats,
    std::size_t latency_sample_count, std::size_t threads) {
  MixedRunReport report;
  report.parameters = parameters;
  report.execution.threads = threads;
  report.execution.mode =
      threads == 1 ? "serial" : "unsupported-mixed-parallel";
  report.configured_operations = count_operations(operations);
  report.initial_key_count = rows.size();
  std::set<std::string, UnsignedByteLess> touched_keys;
  for (const auto& operation : operations) {
    touched_keys.insert(operation.key);
  }
  report.touched_keys = touched_keys.size();

  if (threads == 0) {
    report.status = "invalid_input";
    report.stage = "execution";
    report.reason = "threads must be in the range 1..256";
    return report;
  }
  if (threads > 1) {
    report.status = "unsupported";
    report.stage = "execution";
    report.reason = "mixed operations require serial execution";
    return report;
  }
  if (latency_sample_count != 0) {
    report.status = "invalid_input";
    report.stage = "configuration";
    report.reason = "mixed operation latency samples must be zero";
    return report;
  }
  if (repeats == 0) {
    report.status = "invalid_input";
    report.stage = "configuration";
    report.reason = "repeats must be positive";
    return report;
  }

  try {
    report.stage = "interchange";
    Oracle oracle = make_oracle(rows);

    report.stage = "descriptor";
    auto descriptor_instance =
        make_index(factory, parameters, "descriptor instance");
    const v1::Descriptor descriptor = adapter_call(
        "descriptor", "descriptor instance",
        [&] { return descriptor_instance->descriptor(); });
    report.descriptor = descriptor;
    if (auto error = validate_descriptor(descriptor)) {
      report.status = "invalid_input";
      report.reason = *error;
      return report;
    }
    report.validation.descriptor_checked = true;

    std::vector<std::string> unsupported_operations;
    if (report.configured_operations.insert != 0 &&
        !supported(descriptor.capabilities.insert)) {
      unsupported_operations.push_back("insert");
    }
    if (report.configured_operations.update != 0 &&
        !supported(descriptor.capabilities.update)) {
      unsupported_operations.push_back("update");
    }
    if (report.configured_operations.erase != 0 &&
        !supported(descriptor.capabilities.erase)) {
      unsupported_operations.push_back("erase");
    }
    if (report.configured_operations.scan != 0 &&
        !supported(descriptor.capabilities.scan)) {
      unsupported_operations.push_back("scan");
    }
    if (!unsupported_operations.empty()) {
      report.status = "unsupported";
      report.stage = "capabilities";
      report.reason = "unsupported operation kinds: ";
      for (std::size_t i = 0; i < unsupported_operations.size(); ++i) {
        if (i != 0) {
          report.reason += ", ";
        }
        report.reason += unsupported_operations[i];
      }
      return report;
    }

    std::vector<std::string> complete_domain;
    if (auto error = validate_mixed_domain(
            descriptor.capabilities, rows, operations, &complete_domain)) {
      report.status = "unsupported";
      report.stage = "capabilities";
      report.reason = *error;
      return report;
    }
    report.validation.capability_domain_checked = true;
    descriptor_instance.reset();

    report.stage = "correctness";
    auto empty_index = make_index(factory, parameters, "empty-build instance");
    require_stable_descriptor(
        descriptor, adapter_call("descriptor", "empty-build instance before build",
                                 [&] { return empty_index->descriptor(); }));
    const std::vector<v1::Row> empty_rows;
    adapter_call("bulk_load", "empty-build instance",
                 [&] { empty_index->bulk_load(empty_rows); });
    require_stable_descriptor(
        descriptor, adapter_call("descriptor", "empty-build instance after build",
                                 [&] { return empty_index->descriptor(); }));
    for (std::size_t i = 0; i < complete_domain.size(); ++i) {
      require_find(adapter_indexed_call(
                       "find", "mixed empty-build domain", i,
                       [&] { return empty_index->find(complete_domain[i]); }),
                   false, 0,
                   "mixed empty-build probe at domain index " +
                       std::to_string(i));
    }
    for (std::size_t i = 0; i < operations.size(); ++i) {
      if (operations[i].kind != OperationKind::scan) {
        continue;
      }
      const auto scan_rows = adapter_operation_call(
          OperationKind::scan, "empty-build validation", i, [&] {
            return empty_index->scan(
                operations[i].key,
                static_cast<std::size_t>(operations[i].limit));
          });
      if (!scan_rows.empty()) {
        throw std::runtime_error(
            "empty-build scan returned rows at operation ordinal " +
            std::to_string(i));
      }
    }
    report.validation.empty_build_checked = true;
    empty_index.reset();

    auto correctness_index =
        make_index(factory, parameters, "mixed correctness instance");
    require_stable_descriptor(
        descriptor,
        adapter_call("descriptor", "mixed correctness instance before build",
                     [&] { return correctness_index->descriptor(); }));
    adapter_call("bulk_load", "mixed correctness instance",
                 [&] { correctness_index->bulk_load(rows); });
    require_stable_descriptor(
        descriptor,
        adapter_call("descriptor", "mixed correctness instance after build",
                     [&] { return correctness_index->descriptor(); }));
    for (std::size_t i = 0; i < rows.size(); ++i) {
      require_find(adapter_indexed_call(
                       "find", "mixed represented row", i,
                       [&] { return correctness_index->find(rows[i].key); }),
                   true, rows[i].value,
                   "mixed represented row at index " + std::to_string(i));
      ++report.validation.initial_rows_checked;
    }
    if (!rows.empty()) {
      require_find(adapter_indexed_call(
                       "find", "mixed represented boundary", 0,
                       [&] { return correctness_index->find(rows.front().key); }),
                   true, rows.front().value,
                   "first mixed represented-key boundary");
      require_find(adapter_indexed_call(
                       "find", "mixed represented boundary", rows.size() - 1,
                       [&] { return correctness_index->find(rows.back().key); }),
                   true, rows.back().value,
                   "last mixed represented-key boundary");
    }
    report.validation.represented_boundaries_checked = true;

    MixedOutcomeCounts outcomes;
    for (std::size_t i = 0; i < operations.size(); ++i) {
      const auto& operation = operations[i];
      switch (operation.kind) {
        case OperationKind::find: {
          const auto expected = oracle.find(operation.key);
          const auto actual = adapter_operation_call(
              operation.kind, "correctness", i,
              [&] { return correctness_index->find(operation.key); });
          require_find(actual, expected != oracle.end(),
                       expected == oracle.end() ? 0 : expected->second,
                       "mixed find at operation ordinal " +
                           std::to_string(i));
          if (expected == oracle.end()) {
            ++outcomes.find_misses;
          } else {
            ++outcomes.find_hits;
          }
          break;
        }
        case OperationKind::insert: {
          const auto expected = oracle.emplace(operation.key, operation.value);
          const auto actual = adapter_operation_call(
              operation.kind, "correctness", i, [&] {
                return correctness_index->insert(operation.key,
                                                 operation.value);
              });
          const auto expected_result =
              expected.second ? v1::InsertResult::inserted
                              : v1::InsertResult::already_exists;
          if (actual != expected_result) {
            throw std::runtime_error(
                "mixed insert returned the wrong result at operation ordinal " +
                std::to_string(i));
          }
          if (actual == v1::InsertResult::inserted) {
            ++outcomes.insert_inserted;
          } else {
            ++outcomes.insert_already_exists;
          }
          require_find(adapter_operation_call(
                           OperationKind::find, "affected-key validation", i,
                           [&] { return correctness_index->find(operation.key); }),
                       true, expected.first->second,
                       "affected insert key at operation ordinal " +
                           std::to_string(i));
          break;
        }
        case OperationKind::update: {
          const auto expected = oracle.find(operation.key);
          const bool present = expected != oracle.end();
          if (present) {
            expected->second = operation.value;
          }
          const auto actual = adapter_operation_call(
              operation.kind, "correctness", i, [&] {
                return correctness_index->update(operation.key,
                                                 operation.value);
              });
          const auto expected_result =
              present ? v1::UpdateResult::updated
                      : v1::UpdateResult::not_found;
          if (actual != expected_result) {
            throw std::runtime_error(
                "mixed update returned the wrong result at operation ordinal " +
                std::to_string(i));
          }
          if (actual == v1::UpdateResult::updated) {
            ++outcomes.update_updated;
          } else {
            ++outcomes.update_not_found;
          }
          require_find(adapter_operation_call(
                           OperationKind::find, "affected-key validation", i,
                           [&] { return correctness_index->find(operation.key); }),
                       present, present ? operation.value : 0,
                       "affected update key at operation ordinal " +
                           std::to_string(i));
          break;
        }
        case OperationKind::erase: {
          const auto expected = oracle.find(operation.key);
          const bool present = expected != oracle.end();
          if (present) {
            oracle.erase(expected);
          }
          const auto actual = adapter_operation_call(
              operation.kind, "correctness", i,
              [&] { return correctness_index->erase(operation.key); });
          const auto expected_result =
              present ? v1::EraseResult::erased : v1::EraseResult::not_found;
          if (actual != expected_result) {
            throw std::runtime_error(
                "mixed erase returned the wrong result at operation ordinal " +
                std::to_string(i));
          }
          if (actual == v1::EraseResult::erased) {
            ++outcomes.erase_erased;
          } else {
            ++outcomes.erase_not_found;
          }
          require_find(adapter_operation_call(
                           OperationKind::find, "affected-key validation", i,
                           [&] { return correctness_index->find(operation.key); }),
                       false, 0,
                       "affected erase key at operation ordinal " +
                           std::to_string(i));
          break;
        }
        case OperationKind::scan: {
          std::vector<v1::Row> expected;
          const std::size_t limit =
              static_cast<std::size_t>(operation.limit);
          auto current = oracle.lower_bound(operation.key);
          while (current != oracle.end() && expected.size() < limit) {
            expected.push_back({current->first, current->second});
            ++current;
          }
          const auto actual = adapter_operation_call(
              operation.kind, "correctness", i, [&] {
                return correctness_index->scan(operation.key, limit);
              });
          if (actual.size() != expected.size() ||
              !std::equal(actual.begin(), actual.end(), expected.begin(),
                          [](const v1::Row& left, const v1::Row& right) {
                            return left.key == right.key &&
                                   left.value == right.value;
                          })) {
            throw std::runtime_error(
                "mixed scan returned wrong rows at operation ordinal " +
                std::to_string(i));
          }
          ++outcomes.scan_calls;
          outcomes.scan_rows += actual.size();
          break;
        }
      }
      ++report.validation.operations_checked;
    }
    report.validation.trace_replay_checked = true;
    report.validation.affected_keys_checked = true;

    for (std::size_t i = 0; i < complete_domain.size(); ++i) {
      const auto expected = oracle.find(complete_domain[i]);
      require_find(adapter_indexed_call(
                       "find", "mixed final state", i,
                       [&] { return correctness_index->find(complete_domain[i]); }),
                   expected != oracle.end(),
                   expected == oracle.end() ? 0 : expected->second,
                   "mixed final-state key at index " + std::to_string(i));
    }
    report.validation.touched_keys_checked = touched_keys.size();
    report.validation.final_state_checked = true;
    report.memory = adapter_call(
        "memory_usage", "mixed correctness instance",
        [&] { return correctness_index->memory_usage(); });
    validate_memory(*report.memory);
    const std::size_t final_key_count = oracle.size();
    correctness_index.reset();

    if (operations.empty()) {
      report.status = "skipped";
      report.stage = "measurement";
      report.reason = "mixed operation workload is empty";
      return report;
    }

    report.samples.reserve(repeats);
    for (std::size_t repeat = 0; repeat < repeats; ++repeat) {
      report.stage = "throughput";
      const auto build_start = Clock::now();
      auto index = make_index(
          factory, parameters,
          "mixed throughput repeat " + std::to_string(repeat));
      adapter_call("bulk_load",
                   "mixed throughput repeat " + std::to_string(repeat),
                   [&] { index->bulk_load(rows); });
      const auto build_end = Clock::now();
      require_stable_descriptor(
          descriptor,
          adapter_call("descriptor",
                       "mixed throughput repeat " + std::to_string(repeat),
                       [&] { return index->descriptor(); }));

      ThroughputSample sample;
      sample.repeat = repeat;
      sample.build_ns = elapsed_ns(build_start, build_end);
      sample.operations = operations.size();
      WorkerSample worker;
      worker.worker = 0;
      worker.operations = operations.size();
      const auto lookup_start = Clock::now();
      for (std::size_t i = 0; i < operations.size(); ++i) {
        const auto& operation = operations[i];
        switch (operation.kind) {
          case OperationKind::find:
            consume(adapter_timed_operation_call(
                        operation.kind, repeat, i,
                        [&] { return index->find(operation.key); }),
                    &worker.checksum);
            break;
          case OperationKind::insert:
            consume_u64(static_cast<std::uint64_t>(
                            adapter_timed_operation_call(
                                operation.kind, repeat, i, [&] {
                                  return index->insert(operation.key,
                                                       operation.value);
                                })),
                        &worker.checksum);
            break;
          case OperationKind::update:
            consume_u64(static_cast<std::uint64_t>(
                            adapter_timed_operation_call(
                                operation.kind, repeat, i, [&] {
                                  return index->update(operation.key,
                                                       operation.value);
                                })),
                        &worker.checksum);
            break;
          case OperationKind::erase:
            consume_u64(static_cast<std::uint64_t>(
                            adapter_timed_operation_call(
                                operation.kind, repeat, i,
                                [&] { return index->erase(operation.key); })),
                        &worker.checksum);
            break;
          case OperationKind::scan:
            consume_rows(adapter_timed_operation_call(
                             operation.kind, repeat, i, [&] {
                               return index->scan(
                                   operation.key,
                                   static_cast<std::size_t>(operation.limit));
                             }),
                         &worker.checksum);
            break;
        }
      }
      const auto lookup_end = Clock::now();
      worker.lookup_ns = elapsed_ns(lookup_start, lookup_end);
      sample.lookup_ns = worker.lookup_ns;
      sample.workers.push_back(worker);
      consume_u64(worker.checksum, &report.checksum);
      if (sample.lookup_ns == 0) {
        throw std::runtime_error(
            "mixed throughput duration is below clock resolution");
      }
      sample.throughput_ops_per_second =
          static_cast<double>(sample.operations) * 1.0e9 /
          static_cast<double>(sample.lookup_ns);
      const v1::MemoryUsage memory = adapter_call(
          "memory_usage",
          "mixed throughput repeat " + std::to_string(repeat),
          [&] { return index->memory_usage(); });
      validate_memory(memory);
      if (repeat == 0) {
        report.memory = memory;
      }
      report.samples.push_back(std::move(sample));
    }

    report.outcomes = outcomes;
    report.final_key_count = final_key_count;
    report.status = "passed";
    report.stage = "complete";
    report.reason.clear();
    return report;
  } catch (const std::exception& error) {
    report.status = report.stage == "interchange" ? "invalid_input" : "failed";
    report.reason = error.what();
    report.outcomes = {};
    report.final_key_count.reset();
    report.samples.clear();
    report.latency_samples.clear();
    report.checksum = 0;
    return report;
  } catch (...) {
    report.status = "failed";
    report.reason = "unknown exception";
    report.outcomes = {};
    report.final_key_count.reset();
    report.samples.clear();
    report.latency_samples.clear();
    report.checksum = 0;
    return report;
  }
}

namespace {

void write_parameters_json(std::ostream& output,
                           const v1::Parameters& parameters) {
  output << '[';
  for (std::size_t i = 0; i < parameters.size(); ++i) {
    if (i != 0) {
      output << ',';
    }
    output << "{\"name\": " << json_escape(parameters[i].first)
           << ", \"value\": " << json_escape(parameters[i].second) << '}';
  }
  output << ']';
}

void write_descriptor_json(std::ostream& output,
                           const std::optional<v1::Descriptor>& value) {
  if (!value) {
    output << "null";
    return;
  }
  const auto& descriptor = *value;
  const auto& capabilities = descriptor.capabilities;
  output << "{\n"
         << "    \"implementation\": "
         << json_escape(descriptor.implementation) << ",\n"
         << "    \"implementation_version\": "
         << json_escape(descriptor.implementation_version) << ",\n"
         << "    \"adapter_version\": "
         << json_escape(descriptor.adapter_version) << ",\n"
         << "    \"adaptation_notes\": "
         << json_escape(descriptor.adaptation_notes) << ",\n"
         << "    \"capabilities\": {\n"
         << "      \"keys\": {\"min_bytes\": "
         << capabilities.keys.min_bytes << ", \"max_bytes\": ";
  write_optional_u64(output, capabilities.keys.max_bytes);
  output << ", \"fixed_bytes\": ";
  write_optional_u64(output, capabilities.keys.fixed_bytes);
  output << ", \"allows_nul\": "
         << (capabilities.keys.allows_nul ? "true" : "false")
         << ", \"allows_high_bit\": "
         << (capabilities.keys.allows_high_bit ? "true" : "false")
         << ", \"allows_prefix_pairs\": "
         << (capabilities.keys.allows_prefix_pairs ? "true" : "false")
         << "},\n"
         << "      \"max_value\": " << capabilities.max_value << ",\n"
         << "      \"bulk_load\": "
         << json_escape(support_name(capabilities.bulk_load)) << ",\n"
         << "      \"find\": "
         << json_escape(support_name(capabilities.find)) << ",\n"
         << "      \"insert\": "
         << json_escape(support_name(capabilities.insert)) << ",\n"
         << "      \"update\": "
         << json_escape(support_name(capabilities.update)) << ",\n"
         << "      \"erase\": "
         << json_escape(support_name(capabilities.erase)) << ",\n"
         << "      \"scan\": "
         << json_escape(support_name(capabilities.scan)) << ",\n"
         << "      \"range_sum\": "
         << json_escape(support_name(capabilities.range_sum)) << ",\n"
         << "      \"concurrent_find\": "
         << (capabilities.concurrent_find ? "true" : "false") << "\n"
         << "    }\n"
         << "  }";
}

void write_memory_json(std::ostream& output,
                       const std::optional<v1::MemoryUsage>& memory) {
  if (!memory) {
    output << "null";
    return;
  }
  output << "{\"index_owned_bytes\": ";
  write_optional_u64(output, memory->index_owned_bytes);
  output << ", \"adapter_owned_bytes\": ";
  write_optional_u64(output, memory->adapter_owned_bytes);
  output << ", \"retained_input_bytes\": ";
  write_optional_u64(output, memory->retained_input_bytes);
  output << ", \"accounting_notes\": "
         << json_escape(memory->accounting_notes) << '}';
}

void write_throughput_samples_json(
    std::ostream& output, const std::vector<ThroughputSample>& samples) {
  output << '[';
  for (std::size_t i = 0; i < samples.size(); ++i) {
    const auto& sample = samples[i];
    if (i != 0) {
      output << ',';
    }
    output << "\n    {\"repeat\": " << sample.repeat
           << ", \"build_ns\": " << sample.build_ns
           << ", \"latency_build_ns\": ";
    write_optional_u64(output, sample.latency_build_ns);
    output << ", \"lookup_ns\": " << sample.lookup_ns
           << ", \"operations\": " << sample.operations
           << ", \"throughput_ops_per_second\": " << std::setprecision(17)
           << sample.throughput_ops_per_second << ", \"workers\": [";
    for (std::size_t j = 0; j < sample.workers.size(); ++j) {
      if (j != 0) {
        output << ',';
      }
      const auto& worker = sample.workers[j];
      output << "{\"worker\": " << worker.worker
             << ", \"operations\": " << worker.operations
             << ", \"lookup_ns\": " << worker.lookup_ns
             << ", \"checksum\": " << worker.checksum << '}';
    }
    output << "]}";
  }
  if (!samples.empty()) {
    output << '\n';
  }
  output << ']';
}

}  // namespace

void write_result_json(std::ostream& output, const RunReport& report) {
  output << "{\n"
         << "  \"schema\": \"string-index-static-run/v3\",\n"
         << "  \"status\": " << json_escape(report.status) << ",\n"
         << "  \"stage\": " << json_escape(report.stage) << ",\n"
         << "  \"reason\": " << json_escape(report.reason) << ",\n"
         << "  \"parameters\": ";
  write_parameters_json(output, report.parameters);
  output << ",\n"
         << "  \"execution\": {\"threads\": " << report.execution.threads
         << ", \"mode\": " << json_escape(report.execution.mode)
         << ", \"partition\": " << json_escape(report.execution.partition)
         << ", \"latency_mode\": "
         << json_escape(report.execution.latency_mode) << "},\n"
         << "  \"descriptor\": ";
  if (!report.descriptor) {
    output << "null";
  } else {
    const auto& descriptor = *report.descriptor;
    const auto& capabilities = descriptor.capabilities;
    output << "{\n"
           << "    \"implementation\": "
           << json_escape(descriptor.implementation) << ",\n"
           << "    \"implementation_version\": "
           << json_escape(descriptor.implementation_version) << ",\n"
           << "    \"adapter_version\": "
           << json_escape(descriptor.adapter_version) << ",\n"
           << "    \"adaptation_notes\": "
           << json_escape(descriptor.adaptation_notes) << ",\n"
           << "    \"capabilities\": {\n"
           << "      \"keys\": {\"min_bytes\": "
           << capabilities.keys.min_bytes << ", \"max_bytes\": ";
    write_optional_u64(output, capabilities.keys.max_bytes);
    output << ", \"fixed_bytes\": ";
    write_optional_u64(output, capabilities.keys.fixed_bytes);
    output << ", \"allows_nul\": "
           << (capabilities.keys.allows_nul ? "true" : "false")
           << ", \"allows_high_bit\": "
           << (capabilities.keys.allows_high_bit ? "true" : "false")
           << ", \"allows_prefix_pairs\": "
           << (capabilities.keys.allows_prefix_pairs ? "true" : "false")
           << "},\n"
           << "      \"max_value\": " << capabilities.max_value << ",\n"
           << "      \"bulk_load\": "
           << json_escape(support_name(capabilities.bulk_load)) << ",\n"
           << "      \"find\": "
           << json_escape(support_name(capabilities.find)) << ",\n"
           << "      \"insert\": "
           << json_escape(support_name(capabilities.insert)) << ",\n"
           << "      \"update\": "
           << json_escape(support_name(capabilities.update)) << ",\n"
           << "      \"erase\": "
           << json_escape(support_name(capabilities.erase)) << ",\n"
           << "      \"scan\": "
           << json_escape(support_name(capabilities.scan)) << ",\n"
           << "      \"range_sum\": "
           << json_escape(support_name(capabilities.range_sum)) << ",\n"
           << "      \"concurrent_find\": "
           << (capabilities.concurrent_find ? "true" : "false") << "\n"
           << "    }\n"
           << "  }";
  }
  output << ",\n"
         << "  \"validation\": {"
         << "\"descriptor_checked\": "
         << (report.validation.descriptor_checked ? "true" : "false")
         << ", \"capability_domain_checked\": "
         << (report.validation.capability_domain_checked ? "true" : "false")
         << ", \"empty_build_checked\": "
         << (report.validation.empty_build_checked ? "true" : "false")
         << ", \"represented_boundaries_checked\": "
         << (report.validation.represented_boundaries_checked ? "true"
                                                               : "false")
         << ", \"absent_key_checked\": "
         << (report.validation.absent_key_checked ? "true" : "false")
         << ", \"finite_domain_full\": "
         << (report.validation.finite_domain_full ? "true" : "false")
         << ", \"rows_checked\": " << report.validation.rows_checked
         << ", \"queries_checked\": " << report.validation.queries_checked
         << ", \"concurrent_find_checked\": "
         << (report.validation.concurrent_find_checked ? "true" : "false")
         << ", \"concurrent_queries_checked\": "
         << report.validation.concurrent_queries_checked
         << "},\n"
         << "  \"memory\": ";
  if (!report.memory) {
    output << "null";
  } else {
    output << "{\"index_owned_bytes\": ";
    write_optional_u64(output, report.memory->index_owned_bytes);
    output << ", \"adapter_owned_bytes\": ";
    write_optional_u64(output, report.memory->adapter_owned_bytes);
    output << ", \"retained_input_bytes\": ";
    write_optional_u64(output, report.memory->retained_input_bytes);
    output << ", \"accounting_notes\": "
           << json_escape(report.memory->accounting_notes) << "}";
  }
  output << ",\n  \"samples\": [";
  for (std::size_t i = 0; i < report.samples.size(); ++i) {
    const auto& sample = report.samples[i];
    if (i != 0) {
      output << ',';
    }
    output << "\n    {\"repeat\": " << sample.repeat
           << ", \"build_ns\": " << sample.build_ns
           << ", \"latency_build_ns\": ";
    write_optional_u64(output, sample.latency_build_ns);
    output << ", \"lookup_ns\": " << sample.lookup_ns
           << ", \"operations\": " << sample.operations
           << ", \"throughput_ops_per_second\": " << std::setprecision(17)
           << sample.throughput_ops_per_second << ", \"workers\": [";
    for (std::size_t worker_index = 0;
         worker_index < sample.workers.size(); ++worker_index) {
      if (worker_index != 0) {
        output << ',';
      }
      const auto& worker = sample.workers[worker_index];
      output << "{\"worker\": " << worker.worker
             << ", \"operations\": " << worker.operations
             << ", \"lookup_ns\": " << worker.lookup_ns
             << ", \"checksum\": " << worker.checksum << '}';
    }
    output << "]}";
  }
  if (!report.samples.empty()) {
    output << '\n';
  }
  output << "  ],\n  \"latency_samples\": [";
  for (std::size_t i = 0; i < report.latency_samples.size(); ++i) {
    const auto& sample = report.latency_samples[i];
    if (i != 0) {
      output << ',';
    }
    output << "\n    {\"repeat\": " << sample.repeat
           << ", \"sample\": " << sample.sample
           << ", \"query_index\": " << sample.query_index
           << ", \"latency_ns\": " << sample.latency_ns << '}';
  }
  if (!report.latency_samples.empty()) {
    output << '\n';
  }
  output << "  ],\n  \"checksum\": " << report.checksum << "\n}\n";
  if (!output) {
    throw std::runtime_error("failed to write result JSON");
  }
}

void write_mixed_result_json(std::ostream& output,
                             const MixedRunReport& report) {
  output << "{\n"
         << "  \"schema\": \"string-index-mixed-run/v1\",\n"
         << "  \"status\": " << json_escape(report.status) << ",\n"
         << "  \"stage\": " << json_escape(report.stage) << ",\n"
         << "  \"reason\": " << json_escape(report.reason) << ",\n"
         << "  \"parameters\": ";
  write_parameters_json(output, report.parameters);
  output << ",\n"
         << "  \"execution\": {\"threads\": " << report.execution.threads
         << ", \"mode\": " << json_escape(report.execution.mode)
         << ", \"partition\": " << json_escape(report.execution.partition)
         << ", \"latency_mode\": "
         << json_escape(report.execution.latency_mode) << "},\n"
         << "  \"descriptor\": ";
  write_descriptor_json(output, report.descriptor);
  output << ",\n"
         << "  \"validation\": {"
         << "\"descriptor_checked\": "
         << (report.validation.descriptor_checked ? "true" : "false")
         << ", \"capability_domain_checked\": "
         << (report.validation.capability_domain_checked ? "true" : "false")
         << ", \"empty_build_checked\": "
         << (report.validation.empty_build_checked ? "true" : "false")
         << ", \"represented_boundaries_checked\": "
         << (report.validation.represented_boundaries_checked ? "true"
                                                               : "false")
         << ", \"trace_replay_checked\": "
         << (report.validation.trace_replay_checked ? "true" : "false")
         << ", \"affected_keys_checked\": "
         << (report.validation.affected_keys_checked ? "true" : "false")
         << ", \"final_state_checked\": "
         << (report.validation.final_state_checked ? "true" : "false")
         << ", \"initial_rows_checked\": "
         << report.validation.initial_rows_checked
         << ", \"operations_checked\": "
         << report.validation.operations_checked
         << ", \"touched_keys_checked\": "
         << report.validation.touched_keys_checked << "},\n"
         << "  \"memory\": ";
  write_memory_json(output, report.memory);
  output << ",\n"
         << "  \"configured_operations\": {\"find\": "
         << report.configured_operations.find << ", \"insert\": "
         << report.configured_operations.insert << ", \"update\": "
         << report.configured_operations.update << ", \"erase\": "
         << report.configured_operations.erase << ", \"scan\": "
         << report.configured_operations.scan << ", \"total\": "
         << report.configured_operations.total << "},\n"
         << "  \"outcomes\": {\"find_hits\": "
         << report.outcomes.find_hits << ", \"find_misses\": "
         << report.outcomes.find_misses << ", \"insert_inserted\": "
         << report.outcomes.insert_inserted
         << ", \"insert_already_exists\": "
         << report.outcomes.insert_already_exists
         << ", \"update_updated\": " << report.outcomes.update_updated
         << ", \"update_not_found\": "
         << report.outcomes.update_not_found << ", \"erase_erased\": "
         << report.outcomes.erase_erased << ", \"erase_not_found\": "
         << report.outcomes.erase_not_found << ", \"scan_calls\": "
         << report.outcomes.scan_calls << ", \"scan_rows\": "
         << report.outcomes.scan_rows << "},\n"
         << "  \"state\": {\"initial_key_count\": "
         << report.initial_key_count << ", \"final_key_count\": ";
  if (report.final_key_count) {
    output << *report.final_key_count;
  } else {
    output << "null";
  }
  output << ", \"touched_keys\": " << report.touched_keys << "},\n"
         << "  \"samples\": ";
  write_throughput_samples_json(output, report.samples);
  output << ",\n  \"latency_samples\": [],\n"
         << "  \"checksum\": " << report.checksum << "\n}\n";
  if (!output) {
    throw std::runtime_error("failed to write mixed result JSON");
  }
}

void write_samples_csv(std::ostream& output, const RunReport& report) {
  output << "sample_kind,repeat,sample,query_index,build_ns,lookup_ns,latency_ns,operations,throughput_ops_per_second\n";
  for (const auto& sample : report.samples) {
    output << "throughput," << sample.repeat << ",,," << sample.build_ns << ','
           << sample.lookup_ns << ",," << sample.operations << ','
           << std::setprecision(17) << sample.throughput_ops_per_second << '\n';
    if (sample.latency_build_ns) {
      output << "latency_build," << sample.repeat << ",,,"
             << *sample.latency_build_ns << ",,,,\n";
    }
  }
  for (const auto& sample : report.latency_samples) {
    output << "latency," << sample.repeat << ',' << sample.sample << ','
           << sample.query_index << ",,," << sample.latency_ns << ",,\n";
  }
  if (!output) {
    throw std::runtime_error("failed to write samples CSV");
  }
}

int run_cli(int argc, char** argv, v1::Factory factory) {
  std::map<std::string, std::string> options;
  v1::Parameters parameters;
  std::set<std::string> parameter_names;
  try {
    for (int i = 1; i < argc; ++i) {
      const std::string option = argv[i];
      if (option != "--rows" && option != "--queries" &&
          option != "--operations" && option != "--result" &&
          option != "--samples" && option != "--repeats" &&
          option != "--latency-samples" && option != "--threads" &&
          option != "--parameter") {
        throw std::invalid_argument("unknown option: " + option);
      }
      if (i + 1 >= argc) {
        throw std::invalid_argument("missing value for " + option);
      }
      const std::string value = argv[++i];
      if (option == "--parameter") {
        auto parameter = parse_parameter_argument(value);
        if (!parameter_names.insert(parameter.first).second) {
          throw std::invalid_argument("duplicate parameter name: " +
                                      parameter.first);
        }
        parameters.push_back(std::move(parameter));
      } else if (!options.emplace(option, value).second) {
        throw std::invalid_argument("duplicate option: " + option);
      }
    }
    const std::array<const char*, 5> required = {
        "--rows", "--result", "--samples", "--repeats",
        "--latency-samples"};
    for (const char* option : required) {
      if (options.find(option) == options.end()) {
        throw std::invalid_argument(std::string("missing required option: ") +
                                    option);
      }
    }
    const bool has_queries = options.find("--queries") != options.end();
    const bool has_operations =
        options.find("--operations") != options.end();
    if (has_queries == has_operations) {
      throw std::invalid_argument(
          "exactly one of --queries and --operations is required");
    }
    const std::string workload_option =
        has_queries ? "--queries" : "--operations";

    const std::size_t repeats =
        parse_count(options.at("--repeats"), "--repeats", false);
    const std::size_t latency_samples = parse_count(
        options.at("--latency-samples"), "--latency-samples", true);
    const std::size_t threads =
        options.find("--threads") == options.end()
            ? 1
            : parse_count(options.at("--threads"), "--threads", false);
    if (threads > 256) {
      throw std::invalid_argument("--threads must be in the range 1..256");
    }
    preflight_cli_paths(options, workload_option);

    if (has_queries) {
      RunReport report;
      report.parameters = parameters;
      report.execution.threads = threads;
      report.execution.mode =
          threads == 1 ? "serial" : "shared-index-read-only";
      try {
        const auto rows = read_rows_file(options.at("--rows"));
        const auto queries = read_queries_file(options.at("--queries"));
        report = run_static_lookup(factory, parameters, rows, queries, repeats,
                                   latency_samples, threads);
      } catch (const std::exception& error) {
        report.status = "invalid_input";
        report.stage = "interchange";
        report.reason = error.what();
      }

      std::ostringstream result_output;
      write_result_json(result_output, report);
      std::ostringstream samples_output;
      write_samples_csv(samples_output, report);
      write_exclusive_file(options.at("--result"), result_output.str(),
                           "result");
      write_exclusive_file(options.at("--samples"), samples_output.str(),
                           "samples");
      return report.status == "passed" || report.status == "skipped" ||
                     report.status == "unsupported"
                 ? 0
                 : 1;
    }

    MixedRunReport report;
    report.parameters = parameters;
    report.execution.threads = threads;
    report.execution.mode =
        threads == 1 ? "serial" : "unsupported-mixed-parallel";
    try {
      const auto rows = read_rows_file(options.at("--rows"));
      report.initial_key_count = rows.size();
      const auto operations =
          read_operations_file(options.at("--operations"));
      report = run_mixed_operations(factory, parameters, rows, operations,
                                    repeats, latency_samples, threads);
    } catch (const std::exception& error) {
      report.status = "invalid_input";
      report.stage = "interchange";
      report.reason = error.what();
    }

    std::ostringstream result_output;
    write_mixed_result_json(result_output, report);
    RunReport samples_report;
    samples_report.samples = report.samples;
    samples_report.latency_samples = report.latency_samples;
    std::ostringstream samples_output;
    write_samples_csv(samples_output, samples_report);
    write_exclusive_file(options.at("--result"), result_output.str(),
                         "result");
    write_exclusive_file(options.at("--samples"), samples_output.str(),
                         "samples");
    return report.status == "passed" || report.status == "skipped" ||
                   report.status == "unsupported"
               ? 0
               : 1;
  } catch (const std::exception& error) {
    // Argument and output-path failures cannot always be represented at the
    // requested result path. Keep diagnostics free of key contents.
    std::cerr << "unified runner: " << error.what() << '\n';
    return 2;
  }
}

}  // namespace string_index_bench::unified

#if defined(SIB_ADAPTER_FACTORY_HEADER)
int main(int argc, char** argv) {
  return string_index_bench::unified::run_cli(
      argc, argv, &SIB_ADAPTER_FACTORY_SYMBOL);
}
#endif
