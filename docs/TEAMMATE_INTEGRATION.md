# 同学实现怎样接入本 benchmark

2026-10-17 的源码核对发现，两个模型仓库已经提供 `string-index/v1` 适配包。这周的工作重点是复用这些接口，控制统一负载、校验与测量条件，并为后续并发实现准备扩展性实验。

源码/API 审查与固定提交链接见 [同学进展与兼容性核对](TEAMMATE_COMPATIBILITY_2026-10-17.md)；本文说明实际接入方式。这里的“可接入”指具体版本的接口与本次查询测试，不代替对模型所有功能的验收。

## 现有适配包可以直接使用

| 同学 / 实现 | 核对提交 | `--implementation` 指向外部 checkout 内的位置 | 本轮选择 |
| --- | --- | --- | --- |
| 赵瀚文 / BlockLearn | `60837a886b7040828e6f0bb5382d7950d8eed4fa` | `src/experiments/block-residual-delta/` | `make_block_residual_delta`，保留该包默认设置 |
| 成曾嵘 / Context16 | `564df24461e70cc0a4270351a42d807a8f9902a9` | `07_Eval_Integration/adapters/our_index/` | `make_our_index_index`，保持默认 Context16 |

两份适配包的 CMake 都接受 `STRING_INDEX_CONTRACT_INCLUDE_DIR`。统一入口会在添加它们的源码目录之前，把该变量设置为本 benchmark 当前的接口目录。因此适配器与 runner 都使用同一份 `index_contract.h`，可以安全读取本次新增的能力字段，无需修改同学的源码或维护第二层包装。

以下命令使用已经准备好的外部 checkout；把两个占位路径换成实际位置，输出目录必须尚不存在：

```bash
python3 -B scripts/benchmark.py \
  --dataset examples/keys.txt \
  --implementation /path/to/Learned-Index-Model/src/experiments/block-residual-delta \
  --implementation /path/to/string-learned-index/07_Eval_Integration/adapters/our_index \
  --workload examples/workloads/parallel-read.json \
  --threads 1 2 4 \
  --output results/team-models
```

赵的原仓库链接会跳转到 `Zephyr761/Learned-Index-Model`。成的默认适配包使用 C++17、`-O3` 和 `x86-64-v3`，并保留其断言设置；机器需要支持相应指令集。benchmark 不替这些包下载依赖，也不把不同编译设置隐藏成统一优化条件。源码核对时两个仓库都没有仓库级许可文件，本项目只从外部 checkout 构建，不复制其实现到公开仓库。

## 并行能力要由实现明确提供

这两个固定版本的适配器都未声明 `concurrent_find`。它们可以参加单线程比较；请求 2、4 线程时应得到 `unsupported`，性能列留空。退出码 0 可以同时包含单线程通过和并行不支持，不能只看退出码判断所有组合已测量。

模型维护者确认同一实例的 `find` 在多线程下安全后，可在自己的描述中设置 `capabilities.concurrent_find = true`，再用原命令复测。需要检查的不只是数据结构，还包括查询时的可变缓存、统计字段、懒初始化、后台合并及资源生命周期。benchmark 不自动给查询加全局锁，也不复制多个模型实例来代替共享实例实验。

本周先完成这条交接边界：**模型侧提供可信的操作与并发能力声明，评测侧提供相同查询、独立并发校验、正确计时和线程扩展报告。** 混合读写、scan 与动态维护开销需要另加 workload 和对应正确性规则，现有模型支持写入并不等于统一入口已经测过写入。

## 版本记录不能只依赖描述文字

这次核对发现，赵的适配包仍声明 `source-uncommitted`，成的清单、描述字符串和当前 `index.hpp` 摘要也有不一致。benchmark 保留原声明，同时新增 `build_identity.external_source_git` 记录实际 checkout 提交与 `source_dir` 下已跟踪文件的修改状态；无法获取时标为不可用。

这不是完整源码认证：未跟踪文件、`source_dir` 外的算法/依赖及构建时生成内容不在 Git 修改检查内。正式比较仍应固定 checkout、核对相关文件和依赖，结合接口头文件、runner 与二进制哈希复查。本次对固定外部 checkout 的运行另外确认了整个 checkout 无修改，未替同学修改或“修正”它们的版本声明。

## 潘梓睿 / XIndex 的接入边界

目前提供的材料是 XIndex 论文，没有潘自己的实现仓库，因此不能把上游代码的状态当作他的实现进度。原始 XIndex 接口显式接收 worker ID，并有后台维护线程；需要先明确线程数量、稳定的 worker ID 分配、线程注册/退出以及后台线程停止与回收协议。

上游示例主要使用数值键；不能把任意变长字符串哈希成整数后宣称保留本 benchmark 的精确字符串语义。应另核对字符串方向的 SIndex 分支，或明确证明受限键域的编码无碰撞且保序。

上游还有进程全局的 RCU/配置状态，析构只发出停止信号，未完成后台线程 join。本 runner 在同一进程中会为校验、吞吐和延迟反复创建实例，因此仅让整次 CLI 在独立进程运行还不够；需要先解决重复实例的生命周期协议及回收问题。

目前的静态只读布尔能力适用于无需外部线程上下文的实现。XIndex 类实现的下一步是设计显式的线程上下文/生命周期扩展，先完成单线程和并发只读接入，再定义混合读写的历史校验。不能忽略 worker ID 参数，也不能用“`find` 是 const”或“给每个线程复制一个实例”替代这些协议。本周未安装 MKL/jemalloc，也未运行或宣称复现 XIndex。

## 实跑证据

例子数据上的端到端命令已通过：两个单线程组合完成校验与测量，四个多线程组合明确报告不支持。随后在相同三类真实键、三种分布、每轮 200,000 次查询、5 轮重复的固定负载上复查这两个模型；详细状态见 [接入结果 CSV](experiments/peer-integration-2026-10-17.csv)，本周汇总见 [改进记录](WEEKLY_UPDATE_2026-10-17.md)。

这些是统一接口与静态查询的接入证据。它们不验证同学各自报告中的 ART/HOT 百分比，也不把不同构建设置和未绑核的小规模实验作为算法排名。
