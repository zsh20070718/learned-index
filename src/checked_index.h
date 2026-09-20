#pragma once

#include <map>
#include <stdexcept>
#include "competitors/base.h"

// Used only by --verify. Timed runs use ObservedIndex without this oracle.
template <class Index>
class CheckedIndex : public Index {
  std::map<std::string, uint64_t> expected_;

  void Check(const std::string& key, uint64_t expected) const {
    if (Index::EqualityLookup(key, 0) != expected)
      throw std::runtime_error(Index::name() + ": point lookup/read-back mismatch");
  }

 public:
  explicit CheckedIndex(const std::vector<int>& params) : Index(params) {}
  uint64_t Build(const std::vector<KeyValue<std::string>>& data, size_t threads) {
    expected_.clear();
    for (const auto& row : data) expected_.emplace(row.key, row.value);
    return Index::Build(data, threads);
  }
  size_t EqualityLookup(const std::string& key, uint32_t thread) const {
    const auto it = expected_.find(key);
    Check(key, it == expected_.end() ? util::NOT_FOUND : it->second);
    return Index::EqualityLookup(key, thread);
  }
  uint64_t RangeQuery(const std::string& lo, const std::string& hi, uint32_t thread) const {
    uint64_t expected = 0;
    for (auto it = expected_.lower_bound(lo); it != expected_.end() && it->first <= hi; ++it)
      expected += it->second;
    const auto actual = Index::RangeQuery(lo, hi, thread);
    if (actual != expected) throw std::runtime_error(Index::name() + ": range sum mismatch");
    return actual;
  }
  void Insert(const KeyValue<std::string>& row, uint32_t thread) {
    Index::Insert(row, thread);
    const auto result = expected_.emplace(row.key, row.value);
    Check(row.key, result.first->second);
  }
  size_t size() const {
    // TLI calls size() when producing its result, after the last operation.
    // This catches inserts that damaged a previously present key as well.
    for (const auto& row : expected_) Check(row.first, row.second);
    return Index::size();
  }
};
