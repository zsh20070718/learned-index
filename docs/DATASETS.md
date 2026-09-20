# 真实数据来源与处理

本文仅记录已有样本的来源和处理，不规定用户数据必须满足这些过滤或规模限制。通用入口接受外部原始数据的位置，由 reader 和 benchmark 完成准备，见 [数据接入约定](DATASET_CONTRACT.md)。

## 原项目中的入口

| 本地数据集 | 来源链 | 取键规则 |
| --- | --- | --- |
| `english_words` | TLI 固定版本 `wormhole/README.md:63` → [dwyl/english-words](https://github.com/dwyl/english-words) 的 `words.txt` | 每行一个词条，保留大小写、数字和标点 |
| `memetracker_urls` | 同一 README:481 → [Wormhole 实验记录](https://github.com/wuxb45/wormhole/issues/5) → [MemeTracker 官方数据页](https://snap.stanford.edu/memetracker/data.html) | phrase-cluster 文件中 C 层记录的 URL 字段 |
| `memetracker_phrases` | 同一 MemeTracker 文件 | B 层记录的 QtStr 字段，保留空格 |

Wormhole 记录的是 MemeTracker `m9urls` 实验；这里选择官方可下载的约 220 MiB phrase-cluster 文件，**不声称复现其原始数据快照**。词表锁定 Git 提交 `20f5cc9b3f0ccc8ce45d814c532b7c2031bba31c`；两份完整原文件均固定大小与 SHA-256，详见仓库根目录 `datasets.lock.json`。

TLI 的 `prepare.sh` 等脚本使用 `url_90M_string`，但当前固定版本的下载脚本没有提供该文件的链接。`download.sh` 中的 wiki/books/OSM/Facebook 是整数数据集；GRE 默认入口也以整数键为主。本次没有把这些整数转成十进制字符串充当真实字符串数据。

## 复现和抽样

`scripts/prepare_real_datasets.py` 先校验原文件，再按完整文件扫描，gzip 读到 EOF 并校验尾部。只移除 LF/CRLF，字段内容不做 URL 规范化、大小写转换、Unicode 重编码或裁剪。空键、NUL 和超出当前索引入口 4096 字节限制的键分别计数并排除。

抽样保留 keyed BLAKE2b-128 排名最低的 k 个唯一键，key 为十进制 seed 的 SHA-256，最后按原字节排序。这样同一个键重复出现不会增加被选中概率；结果与原文件行序无关，也避免只读取词典开头。该方法针对唯一键集合，**不保留真实访问频率**。摘要碰撞以完整键字节确定顺序。相同源文件、count 和 seed 可复现相同数据字节。

记录包含完整来源、请求和实际键数、候选记录数（包含重复）、过滤数量、源文件扫描量、数据 SHA-256，以及键长和相邻最长公共前缀的 min/median/p95/max/mean，单位均为字节。统计中的 median 取较低的中位元素，p95 使用 nearest-rank；公共前缀针对采样后排序的相邻键。

默认每类 10,000 键，支持 200–100,000，生成文件不超过 128 MiB。预处理流式读取，解压上限 4 GiB，单行上限 64 KiB；超限或格式错误直接失败，不静默跳过剩余文件。下载有固定大小校验、30 秒网络超时和约 300 秒总时限，无自动重试。每份来源的样本在完整扫描通过后写出，若后续来源失败，已完成样本保留。

## 数据和 pattern 是两个维度

真实数据提供键的形态，查询与插入顺序仍由 benchmark 生成。相同键数和操作数下，比较三类真实键及 random/prefix 合成对照；每类分别运行原有八种负载，以及均匀、热点、Zipf 与未命中组合，共十五种负载。

Zipf 的 rank 绑定字节排序，而非流量排名；热点查询绑定前 10% 的排序键。TLI 的 equality/delta/hotspot 控制插入模式，不能和查询热度混淆。缺失键仍由源键后缀变异生成。当前实验不覆盖真实查询日志、跨时间数据偏移和所有 Unicode 形态。

## 来源署名与保存范围

词表仓库有 Unlicense 文件，同时 README 保留原 Infochimps 来源声明。MemeTracker 数据页署名 Leskovec、Backstrom、Kleinberg，相关论文为 *Meme-tracking and the Dynamics of the News Cycle*, KDD 2009。这里记录来源声明，不推断统一再分发许可。

只提交处理脚本、来源锁定和说明；原数据、派生样本与执行结果留在 Git 忽略的 `data/` 和 `results/`，不随代码发布。
