# Benchmark 范围与实验语义

## 当前 setup 的边界

本阶段建立可复现的字符串 benchmark 开发入口，不代表完整研究任务已经完成。默认构建直接引用固定版本 TLI 的 `benchmark.h`、`generate.cc`、`competitors/art.h`；`StdMap` 实现相同接口。上游工作区保持原样，便于对照和后续选择性接入。GRE 默认 `microbench.cpp` 实例化整数键，目前仅下载参考源码，未构建其完整依赖和子模块。

TLI 原始完整构建涉及 MKL、TBB、jemalloc 及大量整数索引实例化。当前入口只需 Boost 头文件和标准线程库；SIndex、ARTOLC、Wormhole 等没有被伪装成已运行的比较项。

## 工作负载

键按字节字典序排列，每个唯一键的 value 为其在完整排序数据中的位置。点查找返回该位置；区间查询返回闭区间 `[lo, hi]` 内 value 的总和；插入保留相同 value。当前接口和生成器不支持 update/delete。

Smoke 的 6 个基本负载对应课程字符串部分给出的读写比例，另加 TLI 两种插入模式。混合负载中，区间操作占“读操作”的 5%，传给 TLI 时需要换算为全部操作的比例：

| 名称 | 点查找 | 区间查询 | 插入 | 插入模式 |
| --- | ---: | ---: | ---: | --- |
| lookup | 100% | 0% | 0% | — |
| scan | 0% | 100% | 0% | — |
| read50_insert50 | 47.5% | 2.5% | 50% | equality |
| read80_insert20 | 76% | 4% | 20% | equality |
| read20_insert80 | 19% | 1% | 80% | equality |
| insert | 0% | 0% | 100% | equality |
| delta_insert | 47.5% | 2.5% | 50% | delta |
| hotspot_insert | 47.5% | 2.5% | 50% | hotspot，范围比例 0.5 |

区间端点由上游生成器选择，目标约 100 个元素，并非严格固定返回 100 条的 scan API。初始键数为完整数据量减去本负载插入数，因此这些 smoke 组合的初始加载比例并不相同。每种索引在相同组合中复用同一份 workload。

两类合成键为随机变长 ASCII 字符串、带分组共享前缀的类 URL 字符串；都添加唯一后缀并固定 seed=42。它们是键形态样例，不等同于已实现 Zipf/热点访问分布。TLI 的 hotspot 参数控制插入键所在区间，不是热点查询分布。

## 指标与校验

- 上游原始吞吐量 CSV 无表头：`index, build_us, reported_index_bytes, throughput_mops_per_s`（重复测试会增加构建时间和吞吐量列）。吞吐量数值单位是百万操作/秒。
- 上游延迟模式输出 `index, build_us, reported_index_bytes, avg_ns, p50_ns, p99_ns, p999_ns, max_ns, stddev_ns`，仅单次运行。Smoke 的 verify 日志包含该输出，但校验有额外开销，不作为正式延迟测量结果。
- ART 的 `size()` 主要统计树节点；StdMap 统计分配器所分配的 map 节点。二者都不是包含全部字符串分配、输入和 workload 缓冲区的进程内存。正式内存比较需统一口径，当前数值不可直接用于内存排名。
- 入口将上游 `run_failed` 转为非零退出码；Smoke 同时检查返回码和 `RESULT`，防止“索引不适用、被跳过”被当成通过。
- `--through` 在上游关闭逐操作校验。Smoke 先单独 `--verify` 再测吞吐量，且每次重建索引。纯插入的上游 verify 没有读回断言，插入正确性还需增加最终状态比对。
- 默认 smoke 仅用于开发环境验收，规模小、未绑核、未隔离系统负载，不能作为论文实验数据。

## 后续 benchmark 工作

1. 定义统一字符串索引接口：bulk load、find、insert、update、erase、scan 的重复键与未命中语义；显式声明能力，跳过结果与成功结果分开记录。
2. 接入字符串学习型索引（如 SIndex、LITS）及传统字符串基线（如 ARTOLC、HOT、Wormhole）。逐一核对原始论文代码、许可、key 限制及所需依赖；不把整数索引直接包装为字符串算法。
3. 增加未命中查询、uniform/Zipf/热点访问分布、数据偏移、键长度/共享前缀控制和更新删除负载。TLI 当前字符串生成分支将 negative lookup 比例固定为 0，需要单独扩展。
4. 准备 DBLP、IMDb、Reddit、URL、Wiki 等真实数据的来源清单和处理脚本，记录许可、版本、校验和、排序/去重/编码规则。真实数据不放进 Git。
5. 在适配器层增加独立正确性参照，包括插入后读回、缺失键、空区间、前缀关系和边界键。增删改完成后检查最终状态；并发实验需另外验证并发语义。
6. 正式测量分离构建耗时、吞吐量、无校验延迟分位数和统一内存指标；记录 CPU、编译器/参数、数据哈希、随机种子、提交号和重复轮次。再逐步加入 perf、线程扩展和长时间实验。

## 添加一个初始适配器

可以从 `src/std_map.h` 的 TLI 接口开始，在 `src/string_benchmark.cpp` 加入显式索引选项及 `bench.Run<YourIndex>()`。当前入口固定单线程；只提供 lookup、inclusive range sum、insert。新适配器必须先在相同小数据上通过独立正确性校验，再加入计时流程。
