# 统一评测入口：静态点查询、访问分布与并行读取

本入口把“数据在哪里、实现在哪里”接成一条可运行的流程：读取用户数据，准备相同的键和查询，构建适配器，检查正确性，重复测量，再生成报告。

当前范围是**内存、唯一键 map 的静态点查询，支持单线程与共享实例的并发只读查询**。它用于落实项目的评测胶水定位；完整的数据和操作目标仍分别以 [数据接入约定](DATASET_CONTRACT.md) 和 [实现接口契约](INDEX_CONTRACT.md) 为准。

## 从一个例子开始

在仓库根目录运行；只需要 Python 3.9+、CMake 和支持 C++17 的编译器，不需要先下载 TLI、GRE 或真实数据：

```bash
python3 -B scripts/benchmark.py \
  --dataset examples/keys.txt \
  --implementation adapters/std_map \
  --implementation adapters/sorted_vector \
  --output results/my-first-evaluation
```

输出目录必须尚不存在，已有结果不会被覆盖。把 `--dataset` 换成自己的文件路径，把 `--implementation` 换成适配包目录或 `benchmark-adapter.json` 路径即可使用外部数据和实现。路径可以位于仓库之外。

两个自带适配器分别使用标准库 map 和排序数组二分查找，主要用于演示接入和检查评测链。它们不是新设计的学习型索引，也不能代表所有真实索引的性能。

## 数据由入口准备

第一版接收单个文件：

| `--reader` | 自动识别扩展名 | 如何提取键 |
| --- | --- | --- |
| `raw-lines` | `.txt`、`.keys`、`.lines` | 每行原始字节，仅去掉 LF/CRLF 行尾 |
| `jsonl` | `.jsonl`、`.ndjson` | 每行 JSON 对象的 `--key` 顶层字符串字段，按 UTF-8 编码 |
| `legacy-tli` | `.tli` | 旧 TLI 的计数与长度前缀二进制格式 |

默认 `--reader auto` 按上述扩展名选择。未知扩展名应明确指定 reader；旧的无扩展名 `_string` 文件可用 `--reader legacy-tli`。JSONL 必须指定字段，不能默认把整行 JSON 当作键。例如：

```bash
python3 -B scripts/benchmark.py \
  --dataset /path/to/records.jsonl --reader jsonl --key url \
  --implementation adapters/std_map --output results/jsonl-evaluation
```

`--key` 只用于 JSONL；字段缺失、不是字符串、重复 JSON 字段或编码损坏均报错。完整参数见 `python3 -B scripts/benchmark.py --help`。

程序完整读取输入，再按 unsigned byte 字典序排序、合并相同键。报告同时保留原始记录数、唯一键数和重复数；原始数据保持不变。空键、NUL、高位字节、前缀关系和长键不会因为旧 TLI 开发工具的限制而被过滤。

空文件没有可供默认命中查询使用的键，报告为 `skipped`，不产生虚构的性能数值。坏记录、错误字段类型、损坏的二进制格式明确报错，不能静默跳过。文件夹分片、数据清单和外部 reader 插件属于后续接入工作。

物理文件哈希标识本次读到的文件，逻辑哈希标识排序去重后的键集合。不同容器或行序可以拥有相同逻辑哈希，但重复数等原始数据性质仍分别报告。

## 实现通过适配包接入

适配包包含 `benchmark-adapter.json` 和可由 CMake 构建的库 target，工厂函数返回 [index_contract.h](../src/index_contract.h) 中的 `Index`。详见 [实现发现约定](INDEX_CONTRACT.md#从实现在哪找到接口)。首次接入未知算法仍需写一个薄适配器；不能仅凭任意源码目录自动猜测其 API。

构建产物位于本次结果目录，程序不替用户下载依赖或修改系统安装。构建适配包会执行其 CMake 和本地代码，应使用自己信任的实现。依赖不足、构建失败和运行超时都会留下相应状态与阶段。

查询线程数由 `--threads 1 2 4` 指定，默认只测 1；范围 1–256，拒绝重复，按升序依次运行，非空负载不允许线程数超过查询数。仅声明 `concurrent_find=true` 的适配器进入多线程测量。每轮总查询数保持不变，每个适配器只构建一次可执行程序；每轮索引仍重新加载。详见 [并行评测说明](PARALLEL_BENCHMARK.md)。

默认构建并行度为 2，可用 `--jobs` 设置为 1–4。`--build-timeout` 默认允许配置和编译命令各运行 120 秒，`--run-timeout` 默认允许每个实现、线程数组合的评测进程运行 120 秒；超时会记录失败，不自动重试。

运行前检查实现声明的键域、值域以及本轮所有初始键和查询键。实现自身不支持某类键时，本组合记为 `unsupported`；不会为它偷偷删掉数据。首版报告限定为 `bulk_load` / `find` 校验覆盖，不代表可选写入、扫描或区间操作已完成接入验收。

## 同一份查询，分开校验和计时

每个实现复用相同的规范键值和查询序列。默认使用固定种子的均匀点查询，混合命中与未命中；负载参数和实际数量展开记录在报告中。

默认配置如下，可以保存为 JSON 文件并通过 `--workload /path/to/workload.json` 使用。省略的字段沿用默认值，未知字段会被拒绝。旧版 `static-uniform-point/v1` 的字段和查询生成顺序保持不变；省略 `--workload` 仍使用这一版：

```json
{
  "schema": "static-uniform-point/v1",
  "seed": 42,
  "operations": 10000,
  "miss_ratio": "0.1",
  "repeats": 3,
  "latency_samples": 1000
}
```

`operations` 是每轮的查询数量，不是从数据里抽取的键数；所有唯一键都会参与构建。未命中数量为 `floor(operations × miss_ratio)`，缺失键会检查确实不在完整键集合中。`latency_samples` 是每轮单独测量的查询样本数，实际不超过查询总数；它不改变吞吐量轮次的查询数量。设为 0 可关闭延迟测量，此时相关指标标为未测量，不填成零耗时。

### 用同一个入口测试热点与偏斜访问

`static-point/v2` 在相同静态查询流程中增加 `distribution`。三份示例可直接比较：

```bash
for distribution in uniform hotspot zipf; do
  python3 -B scripts/benchmark.py \
    --dataset examples/keys.txt \
    --implementation adapters/std_map \
    --implementation adapters/sorted_vector \
    --workload "examples/workloads/${distribution}.json" \
    --output "results/access-${distribution}" || break
done
```

每份输出目录都必须尚不存在。换成自己的同一份数据与适配器路径，即可保持两侧不变，只改变访问场景。三份配置均为每轮 10,000 次查询、10% 未命中、3 轮重复、每轮 1,000 个延迟样本。

| `distribution` | 访问方式 | 额外参数 |
| --- | --- | --- |
| `uniform` | 从所有源键中等概率有放回抽样 | 无 |
| `hotspot` | 先选择热区或冷区，再在该区均匀抽样 | `hotset_fraction` 默认 `"0.1"`；`hot_probability` 默认 `"0.9"` |
| `zipf` | 第 r 个源键的权重为 r^(-指数)，在有限键集合内归一化 | `zipf_exponent` 默认 `"1.1"`，范围 0–3 |

热点配置的完整例子：

```json
{
  "schema": "static-point/v2",
  "distribution": "hotspot",
  "hotset_fraction": "0.1",
  "hot_probability": "0.9",
  "seed": 42,
  "operations": 10000,
  "miss_ratio": "0.1",
  "repeats": 3,
  "latency_samples": 1000
}
```

热区是字节排序后前 `max(1, min(N-1, floor(N × hotset_fraction)))` 个唯一键。`hotset_fraction` 必须严格在 0–1 之间，`hot_probability` 可取 0 或 1。热点场景要求至少两个唯一键，以保留冷热两区；单键数据明确报负载错误，空数据沿用跳过规则。热点比例按概率采样，实际访问次数不强行凑成 90%。

Zipf 的 r 是字节排序后的名次，从 1 开始；它不是从流量日志学习的排名。指数为 0 时复用均匀抽样；更大的指数会更集中地访问前面的键。生成器使用浮点累计权重表和二分抽样，额外空间 O(N)、时间 O(N + operations × log N)，创建累计表前先计入准备预算。该表只影响负载准备，不计入索引性能。

`v2` 省略 `distribution` 时使用 `uniform`，公共参数的默认值与 v1 一致。不同分布的专属参数不能混用，比如 uniform 带 `zipf_exponent` 会报错，避免参数被悄悄忽略。v1 不能带 v2 字段。十进制参数保留精确文本，支持字符串或 JSON 数字；最多 32 位有效数字、十进制指数绝对值最多 32，避免异常精度造成过大运算。

源键抽样与未命中位置使用独立随机流：只改未命中率时，源键访问顺序保持不变。未命中键仍由选中的源键加后缀生成，并检查不属于完整数据集。报告中的热区比例只统计**命中查询**，不把缺失键算成热区成员；全部未命中时该比例标为未测量。

v2 的 `report.json` 保留分布参数、实际命中/未命中数、命中访问覆盖的唯一键数，以及热点命中比例或第 1 名键的命中比例。`report.md` 展示这些实际数值并说明合成分布的含义；查询文件哈希确认本次使用的完整序列。文件身份与固定种子便于复查同一实验，跨 Python 版本时仍应核对查询哈希。

`--memory-budget-mib` 默认 1024，用于检查 Python 数据/负载准备阶段的内存估算。这是可调整的本次运行预算，不是数据集接收规范中的固定大小上限，也不是操作系统对整个评测进程的硬内存限制。预算不足应调整预算或明确准备较小实验数据，程序不自动抽样。

流程依次是：

1. 检查适用范围，用独立参照验证查询结果、空构建和边界行为。
2. 使用全新实例测量构建和吞吐量，每一轮重新构建。
3. 使用独立的新实例测量延迟，避免把校验开销作为查询耗时。
4. 汇总重复轮次，同时保留原始样本。

构建耗时包括工厂构造和 `bulk_load`。数据读取、排序去重、查询生成和正确答案计算在计时之外。查询返回值会被消费，防止实际查找被编译器消除。

计时使用 `std::chrono::steady_clock`，不另设预热轮。延迟始终在独立单线程实例上测量，不代表并发负载下的延迟。延迟在查询序列上确定性地分散取样；报告中的 P50/P99 按所有轮次的延迟样本合并后计算，包含计时器开销。吞吐量保留各轮数据，汇总中位数、最小值、最大值和标准差（按总体公式，以轮数为分母）。

## 怎么看报告

| 文件 | 用途 |
| --- | --- |
| `report.md` | 首先打开它：查看输入规模、各实现结果、可比较范围和限制 |
| `report.json` | 完整配置、数据身份、能力与版本、环境、各阶段状态和原因 |
| `summary.csv` | 每个实现、线程数的汇总指标、加速比与并行效率 |
| `samples.csv` | 逐轮测量样本，带线程数，便于检查波动和重新计算汇总 |
| `scaling.csv` | 线程扩展性对照：固定总工作量、加速比与并行效率 |
| `workers.csv` | 逐线程操作数、查询耗时和校验和 |

新生成的报告使用 `string-index-benchmark-report/v2`；runner 使用 `string-index-static-run/v2`，增加执行模式、并发校验与工作线程证据。Python 解析器仍可校验历史 v1 runner 结果，但只限单线程。v1/v2 查询文件和负载配置保持不变。

构建身份记录实际编译器、构建配置、适配描述、评测源码、接口头文件和生成程序的哈希。不递归扫描外部源码仓库；适配器自报身份与实际 Git 提交分开保留，并尽力检查 `source_dir` 下已跟踪文件的修改状态。Git 不可用或超时则留空，不把未知写成干净；未跟踪文件和外部依赖不在这项检查内。

`passed` 表示本组合的独立校验和要求的测量完成；`invalid_input`、`unsupported`、`skipped`、`failed` 各有不同原因。失败或跳过的行保留状态，不填成零吞吐量，也不混进成功样本统计。

终端退出码也区分用途：0 表示组合均为通过、不支持或跳过；1 表示至少有一个实现失败或适配描述非法；2 表示命令/全局输入错误，或输出目录已存在。**退出码 0 不等于所有实现都已测量**，仍需查看每行状态。命令和输出位置合法时，坏数据/坏负载配置也会生成六份诊断报告；输出目录已存在时仅拒绝执行，保留旧结果。

内存分项只填写能够说明口径的值，未知项标为不可用。不能把不同实现自报的部分内存混排成“谁更省内存”。延迟含计时器开销，小数据运行、机器负载和未绑核因素仍会影响结果。

## 开发检查

只构建新入口的测试，无需准备上游依赖：

```bash
cmake -S . -B build/unified -DBUILD_LEGACY_BENCHMARK=OFF
cmake --build build/unified -j2
ctest --test-dir build/unified --output-on-failure
```

默认 `BUILD_LEGACY_BENCHMARK=ON` 保留旧入口的构建和测试；需要先按 README 完成旧 setup。

## 当前边界

- 静态点查询已形成端到端流程；插入、更新、删除、scan、range_sum 和混合负载尚未接入此入口。
- 数据准备使用内存排序去重并检查显式预算，超大数据仍需要后续的外部排序等方案，不能自动缩小输入。
- 并发仅覆盖静态只读查询；不提供并发写入、持久化或多值键实验，也不自动认证外部适配器的全部能力。
- 适配器工厂目前接收空参数列表，自定义构造参数的命令行入口仍待接入。
- 小样本运行用于验证接入与复现流程，不能据此给出论文级性能排名。
- 原有 [TLI 开发入口](../README.md#旧-tli-开发入口) 继续保留，其结果和限制按原文档解释。
