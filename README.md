# 生物质减排声明的边界与可分配证据

此目录保存用于产品开发的合成业务资料、字段契约和资料包自检工具。资料说明见 docs/资料口径.md，文件目录及完整性摘要见 data/catalog.json。`sourcepack` 只核对文件、字段、类型、记录键和数量；`claimapp` 在其上提供份额账本、边界口径计算与更正追踪。全部结果都是本地合同口径，不宣称符合任何真实认证或核证规则。

## 环境与资料检查

使用 Python 3.11.2，依赖在 requirements.txt 中锁定。可在虚拟环境安装：

    python3 -m pip install -r requirements.txt
    python3 -m sourcepack
    python3 -m claimapp check

资料检查逐文件回报行数、键唯一性、完整性与关系引用是否符合资料目录；资料行中的拒绝、停试、缺测和不确定范围是业务记录，不是读入错误。目录摘要按规范化换行计算，因此 CRLF 与 LF 检出结果一致。

## 分析应用

所有命令默认读取本目录的资料包，可加 `--root` 指向其他副本。输出为 UTF-8 JSON。

    python3 -m claimapp report                  # 全量核对：占用、并发请求、说明、证据影响
    python3 -m claimapp summary                 # 共享假设分组、可承诺量、更正影响
    python3 -m claimapp lot L1                  # 同一来源在 B1/B2/B3 及每个证据版本下的全链核对
    python3 -m claimapp statement C2            # 从一份声明追到原料、产出、边界、共享假设与更正
    python3 -m claimapp correction X1           # 从一次来源更正查到所有受影响客户与差额
    python3 -m claimapp requests                # 并发请求判定

### 份额

可用量按产出（`output_id`）和产出单位计算，`reserved` 与 `issued` 同时占用。同一产出的占用合计不得超过 `outputs.quantity`；不同产出或不同单位（MWh 与 t）不能相互抵补。

同一时刻到达的请求构成一个并发批次，按“最大最小公平”均分该产出的剩余量：每个请求先拿到不超过其余请求可得的均等份额，不预设任何请求优先。批次总授出量不超过当时剩余量，两个请求不会同时获得全额。

客户缩减用量只释放尚未签发的 `reserved`。`issued` 说明不能释放、改数或删除；证据变化时只能追加撤回、替代或补充说明。

### 减排计算

- 原料干质量 = 湿吨 × (1 − 水分)；运输按湿吨公里，加工按千瓦时，生物源按干吨。
- 原用途基线是带上下界的份额识别问题：在 `share_min`/`share_max` 与份额和为 1 的约束下，分别用线性规划求最小、最大排放系数。结果给出可识别区间，任何单一原用途都不被当作全量假设。
- 同一 `assumption_group` 的网格、运输和生物源因子是共同参数，客户不能各自取端；联合总量见 `summary` 的 `portfolio.joint_feasible_kg`。
- `energy` 与 `value` 按产出权重分配；`system` 按各产出的真实数量解释替代效应，不能把全链总量当作每份客户凭证。
- 不同 `allocation`、`horizon_years`、生物源处理或证据版本的数字不相加、不排序；`lot` 报告中的 `comparisons` 会逐对给出不可比原因。

### 证据版本与更正

每份说明固定它可知时刻的证据版本（`initial` 或某次更正可知时刻）。更正不会改写任何已签发的数字：

- 未签发预留按当前证据重评（`reassess`）。
- 已签发说明若新区间不再与原区间相交，标记 `withdraw_or_supersede` 并列出 `delta_kg`；部分重叠则标记 `supplement`。
- `correction` 报告列出全部受影响客户、每条声明更正前后的区间和差额，以及需要重评的预留。

### 持久化

账本为 JSON 文件，记录动作和事件；基础资料不被修改。三个写操作：

    python3 -m claimapp reserve --ledger claims.json --claim K1 --customer U9 \
        --output O1-1 --boundary B2 --quantity 5 --at 2025-02-10T08:00:00+08:00
    python3 -m claimapp issue --ledger claims.json --claim K1 --at 2025-02-11T08:00:00+08:00
    python3 -m claimapp release --ledger claims.json --claim K1 --quantity 2 --at 2025-02-12T08:00:00+08:00

`reserve` 在写入前校验份额守恒，超额时拒绝；`issue` 只接受预留；`release` 只接受未签发预留。

## 测试

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q claimapp sourcepack tests

测试覆盖资料检查器诊断、份额守恒、并发请求、边界不可比、共享假设的联合区间、更正对预留与已签发说明的不同处理，以及账本读写。运行无需访问外部业务系统。

## 已知边界

- 计算模型是确定性的本地合同口径，不处理真实认证方法学、核证或第三方复核。
- 只支持资料包内的三种边界与三类因子；新增边界约定需要同时补充 `boundaries` 与对应计算说明。
- 账本是单文件 JSON，适合单进程演示；并发写入未做锁。
