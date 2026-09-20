#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

// TLI discards query results when verification is disabled. Consume each result
// at a compiler barrier so inlined lookup/range computations survive -O3.
// GCC/Clang emit no extra instruction for this barrier. The memory clobber also
// prevents moving index memory accesses across it. No hardware fence is needed.
template <class Index>
class ObservedIndex : public Index {
  template <class Value>
  static void Consume(Value value) {
#if defined(__GNUC__) || defined(__clang__)
    asm volatile("" : : "g"(value) : "memory");
#else
    volatile Value sink = value;
    (void)sink;
#endif
  }

 public:
  explicit ObservedIndex(const std::vector<int>& params) : Index(params) {}
  size_t EqualityLookup(const std::string& key, uint32_t thread) const {
    const auto result = Index::EqualityLookup(key, thread);
    Consume(result);
    return result;
  }
  uint64_t RangeQuery(const std::string& lo, const std::string& hi, uint32_t thread) const {
    const auto result = Index::RangeQuery(lo, hi, thread);
    Consume(result);
    return result;
  }
};
