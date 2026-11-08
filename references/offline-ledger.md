# 离线账本工具

只用Python 3.9+标准库。不联网、不读取浏览器登录态、不执行购物动作；它校验一份由Agent根据当前证据整理的JSON，生成同源HTML/手机摘要。它不能凭空证明输入信息真实，也不求全网或全局最优。

在安装的Skill文件夹中运行（使用脚本实际路径，不依赖作者目录）：

```sh
python3 scripts/ledger.py validate examples/anonymous-ledger.json
python3 scripts/ledger.py report examples/anonymous-ledger.json --html /tmp/shopping-demo.html --mobile /tmp/shopping-demo.txt
python3 scripts/ledger.py report examples/anonymous-ledger.json --html /tmp/shopping-share.html --mobile /tmp/shopping-share.txt --public
```

示例是虚构订单；图片是自绘几何服装示意，不是实际商品或真实优惠。

真实本地报告默认可能含订单、运单、完整退回信息；不要直接分享。`--public`白名单输出使用显式public_name（否则匿名编号）和明确批准公开的public_spec（否则显示未公开），不输出自由title/notes、私密sku.spec、订单号、联系人、地址、取件码、运单或内部路径。只对明确确认可公开的图片设置 `public_image: true`；图片本身有隐私时不要批准。公开报告仍包含确认公开的商品名、规格、数量、单价、整组金额与进度，并不是隐藏所有消费金额；分享前仍需本人审阅。公开报告不是原始JSON的脱敏副本，原始输入仍必须留在本地。CLI的stdout/stderr是本地诊断信息，不属于public导出文件，不能直接当脱敏内容发送。

## 输入约定 v1.2（0.1.3工具）

0.1.2仍使用v1.1输入约定。0.1.3新增公开规格白名单和明确包裹数量；已有单件样例兼容，多件包裹必须补数量，不能为通过校验伪造数量或回执。

顶层必需 scope、items、quotes、actions、parcels、refunds。金额全部使用整数分，币种用大写3字母码；不接受浮点人民币金额，不直接合并不同币种。数据字段是本包工具输入，并非电商平台真实API返回结构。

- scope：ledger_id、as_of；可选budgets（币种→新增付款额度分）。submitted、unknown和verified的financial动作都占额度，避免超时订单被当成未付款而重复花费。
- items[]：id、title、sku（product_code、spec对象）、quantity、cancelled_quantity、refunded_quantity、unit_paid（currency、amount_cents）；可选public_name、public_spec对象、image相对路径、public_image布尔、order_ref、notes。public_spec只放已确认可公开的颜色/尺码/件数等，不自动复制私密sku.spec。保留量为总数减取消与退款数，部分退货按数量登记；套装的份数和物理件数应在spec中明确，不混为一谈。
- quotes[]：id、item_ids、matches（item_id→same/different/unknown）、stage（candidate/spec_verified/checkout_verified/ordered/paid）、currency、items_total_cents、fees_cents、total_cents、selected、coupons。费用未知用null，总额也必须null。checkout以后必须明确同款，并提供observed_at（带时区ISO-8601时点）及source（证据来源）；这些仍是账本录入，工具不证明真实平台事实。private报告显示来源，public不显示自由来源文本。可选quantities为item_id→正整数，按报价覆盖数量计算基线；仅保留量恰为1时可省略并默认1，多件/部分选购必须显式填写且不超过保留量。selected必须是布尔值，所选组合不允许重复买目标或重复使用single_use券。
- actions[]：id、type、target_id、idempotency_key、status（prepared/submitted/verified/unknown/failed/needs_user）；financial为true仅表示新增出账/付款占用，另有currency与amount_cents；退款入账放refunds，不能标成financial付款动作。相同目标动作在submitted/unknown状态未对账前不能另开新尝试。failed只表示已核实的失败，超时应记unknown；每条为一次尝试，状态更新原记录，不为每次状态变化另开一次尝试。已核实失败后允许记录新尝试，未决尝试之后禁止另开；verified后不得重复提交。幂等标识来自稳定计划，不能为重试每次添加新时间戳。
- parcels[]：id、item_ids、tracking（receipt_verified、carrier、tracking_number、evidence_source、verified_at）；可选quantities（item_id→正整数）、status、pickup_code、pickup_window、return_recipient、return_address。每个item_id须给本包数量；仅已购未取消量恰为1时可省略并默认1。多件必须显式且不超过quantity-cancelled_quantity，不扣refunded_quantity，以允许已退款但需寄回的历史物品。receipt_verified=true要求实际承运商、真实运单、证据来源和带时区ISO-8601核验时间齐全；这些字段仍需Agent核真实来源，工具不会仅凭格式认定运单真伪。HTML和手机摘要逐包显示对应商品、规格和数量；私密版另列订单与关联退款记录引用，退款引用不等于平台售后单号已获独立验证。
- refunds[]：id、item_id、currency、amount_cents、status（requested/approved/platform_refunded/received）、purpose（savings/recovery）、destination（cash/stored_value）；received必须有带时区ISO-8601的received_verified_at。`purpose:savings`只表示换购相关退款，不表示这笔退款金额就是净节省。退款回收、换购相关退款、现金与储值分开。

输出文件不能互相重合，也不能指向输入JSON或任何引用图片；真实路径、软链接和既有硬链接别名在任何写入前检查。共享目录的恶意并发修改不属于本工具支持的隔离保证；各人使用自己的私有工作目录。

parcels是本批、本轮退回的包裹清单，不是同一实物多轮往返的物流流水。包裹内不允许重复item_ids；数量只用quantities表达。跨包裹汇总数量也不能超过已购未取消量；历史多轮寄件按不同批次保留，不把同一实物的不同往返重复放入本批。报价内部也不允许重复item_ids。所有报价阶段只要提供observed_at即要求带时区ISO时间；candidate/spec_verified允许缺省，checkout以后必须填写。公开报告会显示经校验的核价时点，不显示私密来源文本。

图片仅接受输入JSON同目录及其子目录内的相对本地PNG/JPEG/WebP，检查文件签名、大小和真实路径，禁止软链逃逸、路径穿越或远程下载。无图时报告明确显示暂无图片。HTML采用转义，不能把商品标题或备注当作HTML执行。

## 旧账本迁移

0.1.1新增核价时间、来源与数量约束。旧数据缺字段时按校验错误逐项补真实证据，不能为通过校验编造时间或来源；保留旧输入副本，暂时无法补证的记录留待核查，不覆盖原账本。

## 金额边界

工具可以校验已提供的组价、数量、费用、预算和一次性券使用。当前并未建立真实资金流水与最终保留集合的完整自动核账能力，因此不能把“收到退款”自动换算成“已实现净节省”。按 [数据与金额口径](data-and-money.md)补齐证据后，Agent另行核定；未知项不能当0。凑单报价未包含原单退货费时，只能称新购应付差额，不能称最终换购净省。

`new_purchase_price_difference_not_net_savings`只按已选报价覆盖数量与账本基准比较，不扣除凑单品退款、不验证退款闭环，也不代表已扣完凑单成本。Agent须另行核对整组资金与最终保留集合。未知或已提交付款占用预算，是保守额度预留，不等于已经实际扣款；对账解决状态后再更新原动作。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

这些是离线行为测试，不是三平台账号端到端测试。真实平台、本人认证、取件与到账仍需独立用户小范围实测。
