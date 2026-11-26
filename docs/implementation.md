# 联采服务实现说明

本目录是实现部分，对应 `docs/exchange.md` 的交换约定。实现只依赖 Python 3.11 标准库（含 `sqlite3`），没有第三方包。

## 运行

```sh
# 建库、身份、资格凭证、采购公告
python3 -m liancai --db demo.sqlite3 init

# 用样例时间线重放报价，开标并评审（固定时钟，便于复现）
python3 -m liancai --db demo.sqlite3 seed-demo --evaluate

# 启动 JSON 接口（默认 127.0.0.1:8412）
python3 -m liancai --db demo.sqlite3 serve
```

命令行覆盖报价、撤回、开标、评审、送签、确认、候补、异议与查询，均以 `--token` 表明身份；`python3 -m liancai --help` 可看全部子命令。

## 角色与隔离

| 角色 | 能做什么 |
| --- | --- |
| 采购组 `buyer` | 发布公告、导入凭证、处理异议、送签、候补安排、查看完整评审与审计 |
| 评审组 `evaluator` | 开标、评审/重评、送签、查看完整计算依据 |
| 供应商 `supplier` | 在关标前提交/替换/撤回自己的报价、查看自己的回执与授标、确认/拒绝发给自己的授标、就公开判定或自己材料提异议 |

关键隔离不靠“前端隐藏”，而由服务层强制：

- 开标前（`now < open_at`）任何人（包括采购组）都只能看到报价份数，读不到内容；试图读取会返回 `SEALED`（HTTP 423）。
- 供应商只能读取自己的报价、回执、授标和合同；读取他人材料返回 `FORBIDDEN`。
- 供应商的成交解释只覆盖本方材料与“在全部候选中的相对名次”，不含对手的报量、价格或方案编号。

## 时点与效力

- `receipt_at` 一律取服务端时钟；`receipt_at >= close_at` 的报文记为 `LATE` 回执，**不产生报价**。
- 同一供应商在截止前的最高版本是“关标时在轮”的版本；更早版本标记 `SUPERSEDED`，回执保留。
- **撤回不回落**：最高版本被撤回后该供应商本轮无报价，不会让更早版本复活。
- `open_tender` 只是记账事件：它按截止时点重放哪些报价在轮，不改变任何报价在截止时的效力。
- 资格问题（凭证不属于该供应商、时段超出凭证核准范围）在收件时记录，超出范围时标记 `INELIGIBLE`；异议成立则标记 `DISQUALIFIED`。两者都保留可审计记录，不从库中删除。

## 评审与拟授标

`solve()` 是精确的分支限界搜索，硬约束来自公告：

- 每包不可拆分；整包报价（`whole`）只能整体选中；
- 同一 `exclusion` 全场至多一项；
- 时段不超过 `max_kwh`；允许部分满足时，非零成交量不得低于 `min_kwh`；不允许部分满足时只能整段成交或整段不成交；
- 分组预算与整轮预算各自独立约束。跨组包价按“电量比例 + 最大余数”分摊到各组，分摊和恒等于包价，所以整轮预算不会因分摊而漂移。

满足硬约束后按公告顺序比较：最高优先时段进展 → 次级优先时段进展 → 可成交总量（多者优先）→ 总支出（低者优先）→ 选中报价编号的字典序。

每个未选方案给出原因码（`LOT_CAPACITY`、`BELOW_MINIMUM`、`BUDGET_TOTAL`、`BUDGET_GROUP`、`EXCLUSION_CONFLICT`、`WHOLE_BID_NOT_SELECTED`、`INTERNAL_EXCLUSION`、`INELIGIBLE`、`NOT_SELECTED` 等），供复核与对供应商解释使用。

样例数据的结果是 `A1 + C1`：N=330、P=310、S=230，支出 290000 分，且满足两项最低履约量；升禾、海津的方案都会把 N 或 P 推过上限，因此未入选。

## 异议、复核与送签闸门

- 异议只能针对已公开的判定（`AWARD`）、自己的材料（`BID`）或评审记录（`EVALUATION`）；针对竞争方未公开报价会被拒绝。
- 影响判断：被质疑凭证对应的报价，以及与其共享供应商、凭证、`exclusion`、受影响时段、预算分组的拟授标作为“可能受影响”；完全独立的预算岛即使共享采购编号也不受影响。
- 只要存在**可能改变某个拟授标**的未决异议，该拟授标就不能送签（`Conflict`，送签前会转为 `HELD_FOR_REVIEW`）；不受影响的组照常送签、确认。
- 异议“成立”会立即重算：被质疑报价置为 `DISQUALIFIED`，并生成一条 `REEVALUATION` 评审记录。重评以已签合同为前置条件，绝不撤销已生效的独立授标。
- 异议“不成立”不改变任何授标；原先因该异议暂停的授标恢复暂停前状态（若送签窗口仍开放则回到 `OFFERED`，否则回到 `DRAFT`）。

## 公布、送签与供应商可见内容

- 评审一生成拟授标即为“已公开发布”（`published_at`），供应商可以查到自己相关的拟授标（`status=DRAFT`，阶段 `PROPOSED`），但**只有采购组正式送签后**（`OFFERED`）才能确认。
- 因重评不再入选的已发布拟授标标记为 `SUPERSEDED`，供应商查询中可见“已不再可签”，而不是消失。
- 异议期间，可能受影响的拟授标转 `HELD_FOR_REVIEW`；供应商查询显示 `UNDER_REVIEW`，确认请求得到 `HELD_FOR_REVIEW` 而不是成功或含糊的失败。

## 授签、有效期与候补

- 授标送签时的截止取 `bid.expires_at` 与公告 `valid_until` 中较早者；送签时窗口已过则直接置 `EXPIRED`，不产生可签机会。
- 供应商只能确认**发给自己的、仍在有效期内的**授标；`accept` 生成合同，`reject` 置 `DECLINED`，超时置 `EXPIRED`——三者在状态与查询结果中可区分。
- 同一授标的所有确认都带 `idempotency_key`；同键重放返回第一次的结果并标 `idempotent_replay=true`，不同键也不会重复签约（只返回既有合同）；同键不同动作返回 `Conflict`。并发确认由服务互斥锁串行化，实测 8 路并发只产生 1 份合同。
- 候补返回 `reserved`（已签与在有效期内的送签占用的真实范围）与 `projected`（加上候补候选后的完整计划），两者分开，便于采购组核对剩余预算与不可拆约束。
- 候补只接**尚未签走**的范围：已签合同与仍在有效期内的送签占用预算、电量与 exclusion；候选仍须满足剩余预算、不可拆包和供应商互斥，且报价未过期。已生效的独立授标不会被候补或重评换掉。

## 查询与审计

- 供应商：`status`（阶段：等待开标/待签/复核中/已签/已拒绝/不再可签/未中选）、`my-case`（本方成交原因与相对名次）、`receipts`、自己的 `awards`/`contracts`。
- 采购/评审：`history` 汇总全部报价、回执、各轮评审、授标、合同、异议与审计日志；`impact` 查看某异议牵连的排除组与“受影响/不受影响”的授标。
- 所有变更都写 `audit` 表；回执与报价版本不可改写。

## 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /tenders` / `GET /tenders/{id}` | 发布公告 / 查看（按角色与开标状态裁剪） |
| `POST /tenders/{id}/bids` | 供应商提交或替换报价，返回回执 |
| `POST /tenders/{id}/bids/{bid}/withdraw` | 关标前撤回 |
| `GET /tenders/{id}/receipts` | 本方回执 |
| `POST /tenders/{id}/open` | 开标（记账，返回开标回执） |
| `POST /tenders/{id}/evaluations` / `GET /evaluations/{id}` | 评审 / 完整计算依据 |
| `POST /tenders/{id}/reevaluate` | 重评 |
| `GET /tenders/{id}/awards` | 拟授标（按角色裁剪） |
| `POST /awards/{id}/offer` | 送签（受异议闸门约束） |
| `POST /awards/{id}/accept` `POST /awards/{id}/reject` | 供应商确认，需 `idempotency_key` |
| `GET/POST /tenders/{id}/standby` | 候补安排 |
| `POST /tenders/{id}/disputes` / `GET /disputes/{id}` / `POST /disputes/{id}/resolve` | 异议、影响查询、处理 |
| `GET /tenders/{id}/my-case`、`/status`、`/history`、`/contracts` | 查询 |

身份用 `Authorization: Bearer <token>`；令牌只在 `register-user`/`init` 时返回，库存的是 SHA-256 摘要，不回显。

## 已知边界

- 精确求解是 NP 难问题的分支限界，节点数有显式上限；超限返回 `EVALUATION_TOO_COMPLEX` 而不是悄悄给出次优解。
- 跨组包价分摊规则是本实现的约定（公告未规定时）；若公告另有拆分方式，应改为读取公告字段。
- 本实现面向单实例 SQLite；多实例部署需换成共享数据库并保留同样的服务层校验。
