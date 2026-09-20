#pragma once

// Normative C++17 adapter surface for string-index/v1.
// Semantics: docs/INDEX_CONTRACT.md; data model: docs/DATASET_CONTRACT.md.
// The current TLI runner does not consume this interface yet.

#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace string_index_bench::v1 {

using Value = std::uint64_t;
struct Row {
  std::string key;
  Value value;
};

enum class Support { unsupported, native, adapted };
enum class InsertResult { inserted, already_exists };
enum class UpdateResult { updated, not_found };
enum class EraseResult { erased, not_found };

struct UnsupportedOperation : std::logic_error {
  using std::logic_error::logic_error;
};

struct KeyDomain {
  // Implementation limits only: these never constrain dataset admission.
  // null max_bytes means no declared implementation-specific upper bound.
  std::uint64_t min_bytes = 0;
  std::optional<std::uint64_t> max_bytes;
  std::optional<std::uint64_t> fixed_bytes;
  bool allows_nul = true;
  bool allows_high_bit = true;  // Bytes 0x80..0xff.
  bool allows_prefix_pairs = true;  // Distinct keys a and a + suffix.
};

struct Capabilities {
  KeyDomain keys;
  Value max_value = std::numeric_limits<Value>::max();
  Support bulk_load = Support::unsupported;
  Support find = Support::unsupported;
  Support insert = Support::unsupported;
  Support update = Support::unsupported;
  Support erase = Support::unsupported;
  Support scan = Support::unsupported;
  Support range_sum = Support::unsupported;
};

struct Descriptor {
  std::string implementation;       // Algorithm/library identity.
  std::string implementation_version;  // Commit or immutable release ID.
  std::string adapter_version;      // Commit or source digest.
  Capabilities capabilities;
  std::string adaptation_notes;     // Required for each adapted operation.
};

struct MemoryUsage {
  // Null means unavailable, never zero. Byte counts must not overlap.
  std::optional<std::uint64_t> index_owned_bytes;
  std::optional<std::uint64_t> adapter_owned_bytes;
  std::optional<std::uint64_t> retained_input_bytes;
  std::string accounting_notes;
};

using Parameters = std::vector<std::pair<std::string, std::string>>;

class Index {
 public:
  virtual ~Index() = default;
  virtual Descriptor descriptor() const = 0;

  // Called exactly once on a fresh instance; sorted unique rows, possibly empty.
  // The input remains immutable and alive until this instance is destroyed.
  virtual void bulk_load(const std::vector<Row>& rows) = 0;
  virtual std::optional<Value> find(std::string_view key) const = 0;

  // Unsupported methods must throw UnsupportedOperation without mutation.
  virtual InsertResult insert(std::string_view key, Value value) = 0;
  virtual UpdateResult update(std::string_view key, Value value) = 0;
  virtual EraseResult erase(std::string_view key) = 0;

  // Owning results, ordered by unsigned-byte lexicographic key order.
  // Return at most limit rows whose keys are >= lower_bound.
  virtual std::vector<Row> scan(std::string_view lower_bound,
                                std::size_t limit) const = 0;
  // Inclusive [lower_bound, upper_bound], sum modulo 2^64.
  virtual Value range_sum(std::string_view lower_bound,
                          std::string_view upper_bound) const = 0;
  virtual MemoryUsage memory_usage() const = 0;
};

// Each adapter exports a distinctly named factory with this signature.
// Parameters must be validated; no dataset/workload names or paths are passed.
using Factory = std::unique_ptr<Index> (*)(const Parameters&);

}  // namespace string_index_bench::v1
