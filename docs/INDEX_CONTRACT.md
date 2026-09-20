# 实现接口契约：string-index/v1

状态：确定的 v1 接入规范。函数签名以独立 C++17 头文件 [index_contract.h](../src/index_contract.h) 为准，语义以本文为准；修改任一侧必须同步另一侧。现有 TLI runner 尚未迁移，不声称现有索引已经符合本契约。

## 1. 边界与最小接入单元

接入单元是“外部实现 + 薄适配器”，benchmark 核心只调用 `string_index_bench::v1::Index`。外部库无需修改自己的 API，也不需要认识数据来源、文件格式、workload 文件名、随机种子、正确答案或测量 CSV。

适配器提供一个唯一命名的工厂，例如：

```cpp
std::unique_ptr<string_index_bench::v1::Index>
make_my_index(const string_index_bench::v1::Parameters& parameters);
```

统一脚本从用户提供的实现位置找到适配器并加载工厂，不要求修改 benchmark 的索引枚举或把实现源码复制进本仓库。`Parameters` 为名称/字符串值列表，不允许重名，未知参数和非法值必须以 `std::invalid_argument` 拒绝。构造参数影响行为时必须写入结果记录。工厂返回非空的全新实例；配置在整个实例生命周期内不可改变。

v1 固定**单线程、内存索引、唯一键 map、unsigned-byte 字典序、uint64 值**。不定义并发一致性、持久化、多值键、自定义比较器或批量操作。算法可以是学习型或传统索引，不要求相同内部布局。

### 从“实现在哪”找到接口

`--implementation` 指向包含 `benchmark-adapter.json` 的目录，或直接指向该文件。适配包可以位于外部实现仓库内，也可以单独放在旁边引用该仓库，用户无需修改上游算法。

v1 采用源码构建的 C++17 适配包，最小描述文件为：

```json
{
  "contract": "string-index/v1",
  "id": "my-index",
  "source_dir": ".",
  "cmake_target": "my_index_adapter",
  "factory_header": "my_index_adapter.h",
  "factory_symbol": "make_my_index"
}
```

这些字段均为必需项。`source_dir` 相对于描述文件目录解析，其中的 CMake 项目提供一个库 target；其 PUBLIC include 路径可找到 `factory_header`，库导出 `factory_symbol` 指定的、符合 `Factory` 类型的函数。工厂名必须是合法的 C++ 标识符或限定名。适配库通过 harness 提供的 include 路径包含 `index_contract.h`，不自带另一个版本的接口定义。

统一脚本负责为这个适配包生成独立 runner 构建入口、以相同工具链链接 harness 和适配库、校验接口版本、再启动评测。v1 不承诺跨编译器 C++ 二进制 ABI。构建产物保存在结果/缓存目录，不覆盖实现源码；依赖准备方法由适配包说明，缺依赖时明确报错，不自动下载或修改系统环境。

这套发现/构建流程是后续脚本的实现要求，目前尚未实现。任意原始索引仓库首次接入仍需写一次薄适配器；无法只凭一个未知仓库路径推断所有 API。完成适配之后，用户仅指定其位置即可反复评测不同数据，无需为每个数据集改实现。

## 2. 必须实现的管理与数据接口

所有虚函数都必须有实现；不支持的可选操作提供抛出 `UnsupportedOperation` 的桩，不能空执行或返回看似成功的结果。

| 函数 | 必需性 | 精确语义 |
| --- | --- | --- |
| `descriptor() const → Descriptor` | 必须 | 返回实现/适配器版本与能力；同一配置下稳定，无状态修改，无数据集名称分支 |
| `bulk_load(const vector<Row>&) → void` | 必须 | 在新实例上恰好调用一次，接收严格递增、唯一的键值；完整建立该 map；空输入必须成功 |
| `find(string_view key) const → optional<Value>` | 必须 | 存在时返回当前 value，不存在时返回 `nullopt`；不得以某个 value 表示缺失 |
| `memory_usage() const → MemoryUsage` | 必须 | 提供可获得的内存口径；未知值用 `nullopt`，不以 0 代替未知 |
| 析构 | 必须 | 释放该实例持有的资源；允许销毁新实例或失败实例；不得抛异常 |

`Row` 为 `{ std::string key; uint64_t value; }`。`0` 和 `UINT64_MAX` 都是合法载荷；实现若不能存储整个值域，必须通过 `max_value` 限定可接受上界。benchmark 绝不把位置预测、候选区间或近似值当成成功的 `find`。学习型索引需要的纠错搜索必须在适配器调用返回前完成。

所有数据操作只能在一次成功的 `bulk_load` 后调用；`bulk_load` 不用于重置。重新运行/重复测量必须创建新实例。无论 map 因为空 build 还是删除最后一个键而变空，声明支持的操作都须遵守下述空状态行为。

## 3. 可选操作的统一语义

| 函数 | 返回与状态变化 |
| --- | --- |
| `insert(key, value) → InsertResult` | 缺失：写入并返回 `inserted`；已存在：返回 `already_exists`，保留旧值。不是 upsert |
| `update(key, value) → UpdateResult` | 存在：替换值，返回 `updated`，即使新旧值相同；缺失：返回 `not_found`，不插入 |
| `erase(key) → EraseResult` | 存在：移除并返回 `erased`；缺失：返回 `not_found`，不改状态 |
| `scan(lower_bound, limit) → vector<Row>` | 从第一个 `key >= lower_bound` 的键开始，严格递增返回至多 `limit` 条当前键值；到末尾停止 |
| `range_sum(lower_bound, upper_bound) → Value` | 对闭区间 `[lower_bound, upper_bound]` 中当前 value 求和，按模 `2^64` 相加 |

`scan` 的 `limit=0`、空 map 或起点大于最大键均返回空 vector；起点无需命中已有键；禁止回绕到首键、补足条数或无序返回。返回的 `Row` 自持有键字节，随后修改/销毁索引不使结果失效。

`range_sum` 的空 map、无键区间或 `lower_bound > upper_bound` 均返回 0；相等端点包含该键。它与按条数返回记录的 `scan` 是两个独立能力、两种负载和两种结果，不能互相改名充当已支持。返回 0 也不能证明区间为空。

当 `insert` 被声明支持时，重复键分支也必须支持；`update`/`erase` 同理必须支持缺失分支。不能通过“本次负载碰巧没遇到”宣称满足完整操作契约。

### 可直接用作接入验收的例子

在能力覆盖这些键值的实例上，先 `bulk_load({("a", 0), ("ab", 7), ("b", 9)})`，按顺序执行：

| 操作 | 结果与影响 |
| --- | --- |
| `find("a")`、`find("aa")` | `0`、`nullopt` |
| `insert("a", 6)` | `already_exists`；`find("a")` 仍为 0 |
| `update("ab", 11)` | `updated` |
| `update("z", 1)`、`erase("z")` | 均为 `not_found`；不产生 z |
| `scan("aa", 2)` | `[("ab", 11), ("b", 9)]` |
| `scan("a", 0)`、`scan("z", 2)` | 均为空 |
| `range_sum("a", "ab")` | 11 |
| `range_sum("aa", "aa")`、`range_sum("b", "a")` | 均为 0 |
| `erase("ab")`、`find("ab")` | `erased`、`nullopt` |

支持完整值域时，两个键的值为 `UINT64_MAX` 和 1，其区间和必须为 0。空键、NUL、高位字节、前缀键以及空初始状态另按声明能力覆盖测试；例如 `"a" < "a\0" < "aa"`，`0x7f < 0x80`。

## 4. 能力声明与匹配

`Capabilities` 的每个操作取以下三值之一：

- `native`：由原实现提供完整语义，适配器只做参数/结果转换。
- `adapted`：适配器增加搜索、重复键检查、循环插入式 build 或其他补偿；必须在 `adaptation_notes` 逐操作说明方法及额外存储。
- `unsupported`：不提供该操作，不应进入该负载。

`bulk_load` 和 `find` 必须为 `native` 或 `adapted`，否则拒绝注册。原实现没有 bulk-load API 时可用逐次插入实现 build，整体时间和内存都计入构建，声明 `adapted`。原实现不接受空状态时可以用明确的空状态处理适配，但不得悄悄塞入伪键。

`KeyDomain` 描述**该实现自身**的限制，不是数据集接收条件：最短键长 `min_bytes`、可选最长键长 `max_bytes`、可选固定长度 `fixed_bytes`、是否支持 NUL、高位字节和前缀键对。`max_bytes=nullopt` 表示未声明实现专属上限；没有 benchmark 全局字节限制。有限区间必须合法，固定长度须位于区间内；`max_value` 为可存储载荷上界。这些是适配器对外提供的能力，默认字段值不是测试通过的证据。v1 只接入可用这些限制表达键域和值域的实现；其他限制需要先扩充并版本化契约，不能藏在备注中。

运行前 benchmark 必须检查初始键、全部操作涉及的键/边界及新值。前缀关系保守地检查整个负载涉及的不同键的并集；即使两键从不同时存活，也不绕过这个限制。约束不满足记录 `unsupported` 和具体原因。不能只检查数据文件，更不能依赖数据集名称、哈希、路径或操作文件名判定。

例如静态索引可以只声明 build/find；它可以参加点查询比较，遇到写负载记 `unsupported`，不作为零吞吐量或成功样本。不同能力的实现只在相同操作语义、相同数据和负载的交集上比较，并报告覆盖范围。

## 5. 所有权、编码与错误

- `bulk_load` 的 vector、行和键由 harness 持有，在整个实例生命周期内地址稳定且不可变；实现可借用它们，所保留输入的内存须报告。实现析构后 harness 才释放输入。
- 其他调用的 `string_view` 仅在本次调用内有效。实现需保留插入键时必须复制或取得自己的所有权，不能缓存调用方的 view。返回的 value 和 scan 结果不借用索引内存。
- 原实现需要编码时必须保持精确相等；提供有序操作还必须保序。不允许截断、碰撞合并或用固定整数哈希替代字符串比较。编码、纠错和补查的时间/内存纳入适配器；具体方式记录在 `adaptation_notes`，受影响的操作声明 `adapted`。
- 禁止从外部读取完整未来操作或正确答案，禁止缓存预计算查询结果来替代被测索引；适配器不能借助 benchmark 的 oracle 完成操作。实现所需的底层有序数组可由 build 输入提供，并计入依赖内存。
- 正常缺失、重复键、空扫描使用规定返回值；无效参数、分配失败和实现故障使用异常。异常终止本轮并记录 `failed`，不要求继续运行或回滚实例；harness 必须销毁实例。进程崩溃/超时也记失败，不自动重试。
- 在合法生命周期内误调用未支持操作时必须抛 `UnsupportedOperation` 且不改状态；正常能力预检本应避免此路径。声明支持却抛此异常仍是失败，不能事后降为跳过。

## 6. 计时和内存归属

计时由 harness 管理，接口不返回自报耗时。文件 I/O、解码、规范排序、负载生成、正确答案计算和能力预检在计时外完成。每轮使用相同初始数据的新实例，先以独立实例校验，再使用全新实例测量；校验数据结构不得进入计时路径。

构建计时覆盖实际被测实例的工厂调用和 `bulk_load`，包括分配、复制、编码、训练、排序及辅助结构建立。能力预检可用另一个无数据的实例读取 descriptor 后销毁。预处理若依赖具体实现，就不能移到计时外。`bulk_load` 返回意味着所有构建/训练工作完成；不允许后台工作影响后续阶段。v1 不启动后台工作线程。

操作计时覆盖接口调用及适配器全部工作。`scan` 的结果分配、复制、消费和释放计入 scan 负载，不能与不物化结果的 native iterator 性能混报。返回值必须被消费以防优化消除；正确性断言在独立校验轮执行。实例销毁和 `memory_usage` 在操作计时之外。具体计时器、预热和延迟采样策略属于后续 harness 规定，同组实验必须一致并记录。

`MemoryUsage` 分成互不重叠的三项：

| 字段 | 口径 |
| --- | --- |
| `index_owned_bytes` | 原实现拥有的存活分配，包括其节点、模型及其自有键/值副本 |
| `adapter_owned_bytes` | 适配器拥有的额外分配，包括编码副本、纠错结构；不重复计算上一项 |
| `retained_input_bytes` | 实现借用、必须保持存活的 harness 输入分配；共享分配按整块计一次 |

`accounting_notes` 必须说明 capacity、分配器开销、共享输入和未知项如何处理。三项均已知且口径一致时才汇总；不统一口径时保留分项，不生成内存排名。harness 的 workload、oracle 和临时 scan 输出不属于索引常驻内存。进程 RSS、峰值内存是另行记录的指标，不能用 `size()` 冒充。

## 7. 接入通过条件与结果

接入流程必须先验证 descriptor/参数及能力匹配，再对声明支持的操作执行独立参照校验，包括上面的边界、写后读回与最终状态。删除后须确认键不存在；支持 scan 时核对完整有序键值及无多余键。只有声明支持的语义全部通过，才可以在覆盖范围内计时。

结果至少关联数据逻辑哈希、工作负载哈希、初始状态/值生成规则、实现与适配器版本、参数、能力与适配说明、编译/机器配置、重复轮次、校验状态和测量口径。数据集名称只是标签。

| 终态 | 含义 |
| --- | --- |
| `invalid_input` | 数据/负载/配置违反契约；能力声明自身非法也归此类 |
| `unsupported` | 合法组合超出实现明确声明的能力，未执行 |
| `skipped` | 资源预算或负载构造前提不满足，记录原因，未取得有效测量 |
| `failed` | 支持范围内的校验错误、异常、崩溃或超时 |
| `passed` | 本组合独立校验和要求的测量均完成；仅有校验通过不生成测量成功记录 |

不把 unsupported/skipped/failed 混入成功样本统计。通过某一数据集也不能替代能力边界的接入校验。

## 8. 与现有代码的明确差距

| 现有 TLI 接口/行为 | v1 接线要求 |
| --- | --- |
| `Build` 返回计时；`size()` 自报口径 | 改由 harness 统一计时；内存分项或报告未知 |
| `EqualityLookup` 用 `NOT_FOUND` sentinel | 转成 optional，证明值域不冲突；有保留值时声明 `max_value` |
| `Insert` 无返回值 | 补足重复键不覆盖及返回值；若需额外查找，声明 adapted 并计时 |
| `RangeQuery` 闭区间求和 | 对应独立 range_sum，不能算作 scan；必须覆盖空区间 |
| `applicable(..., filename)` | 替换为显式能力与键/负载性质检查 |
| 当前没有 update/erase/scan 接口 | 在适配完成前均 unsupported |

当前上游 ART 的空区间 range query 有已知错误，须先用可追溯适配修正并验证，或将 range_sum 声明 unsupported；不能靠当前负载避开空区间而宣称符合契约。现有 StdMap/TLI ART、CheckedIndex、ObservedIndex 仍走旧接口；新增头文件不改变它们，也不意味着自动完成迁移。
