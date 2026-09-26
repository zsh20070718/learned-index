#include "runner.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <filesystem>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
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
         left.range_sum == right.range_sum;
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

void preflight_cli_paths(const std::map<std::string, std::string>& options) {
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
  const auto queries =
      canonical_cli_path(options.at("--queries"), "queries input");
  const auto result =
      canonical_cli_path(options.at("--result"), "result output");
  const auto samples =
      canonical_cli_path(options.at("--samples"), "samples output");
  if (result == samples) {
    throw std::invalid_argument("result and samples paths collide");
  }
  if (result == rows || result == queries || samples == rows ||
      samples == queries) {
    throw std::invalid_argument("an output path collides with an input path");
  }
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

RunReport run_static_lookup(v1::Factory factory,
                            const v1::Parameters& parameters,
                            const std::vector<v1::Row>& rows,
                            const std::vector<Query>& queries,
                            std::size_t repeats,
                            std::size_t latency_sample_count) {
  RunReport report;
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

      const auto lookup_start = Clock::now();
      for (std::size_t i = 0; i < queries.size(); ++i) {
        consume(adapter_repeated_call(
                    "find", "throughput query", repeat, i,
                    [&] { return throughput_index->find(queries[i].key); }),
                &report.checksum);
      }
      const auto lookup_end = Clock::now();

      ThroughputSample sample;
      sample.repeat = repeat;
      sample.build_ns = elapsed_ns(build_start, build_end);
      sample.lookup_ns = elapsed_ns(lookup_start, lookup_end);
      sample.operations = queries.size();
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

void write_result_json(std::ostream& output, const RunReport& report) {
  output << "{\n"
         << "  \"schema\": \"string-index-static-run/v1\",\n"
         << "  \"status\": " << json_escape(report.status) << ",\n"
         << "  \"stage\": " << json_escape(report.stage) << ",\n"
         << "  \"reason\": " << json_escape(report.reason) << ",\n"
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
           << json_escape(support_name(capabilities.range_sum)) << "\n"
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
           << sample.throughput_ops_per_second << '}';
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
  try {
    for (int i = 1; i < argc; ++i) {
      const std::string option = argv[i];
      if (option != "--rows" && option != "--queries" &&
          option != "--result" && option != "--samples" &&
          option != "--repeats" && option != "--latency-samples") {
        throw std::invalid_argument("unknown option: " + option);
      }
      if (i + 1 >= argc) {
        throw std::invalid_argument("missing value for " + option);
      }
      if (!options.emplace(option, argv[++i]).second) {
        throw std::invalid_argument("duplicate option: " + option);
      }
    }
    const std::array<const char*, 6> required = {
        "--rows",   "--queries", "--result",
        "--samples", "--repeats", "--latency-samples"};
    for (const char* option : required) {
      if (options.find(option) == options.end()) {
        throw std::invalid_argument(std::string("missing required option: ") +
                                    option);
      }
    }

    const std::size_t repeats =
        parse_count(options.at("--repeats"), "--repeats", false);
    const std::size_t latency_samples = parse_count(
        options.at("--latency-samples"), "--latency-samples", true);
    preflight_cli_paths(options);
    RunReport report;
    try {
      const auto rows = read_rows_file(options.at("--rows"));
      const auto queries = read_queries_file(options.at("--queries"));
      report = run_static_lookup(factory, {}, rows, queries, repeats,
                                 latency_samples);
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
