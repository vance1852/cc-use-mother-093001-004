# 追踪实物样品交接保管协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

在此之上已实现**实物复核阶段的样品交接与保管服务**（`custody.py`），覆盖预约入库、双人核验、独立异常流程、原子资源占用、连续保管链、组件级冻结、只增不改的补录证据、历史时点还原与分角色视图。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、样品保管领域服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、样品保管规则与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记项目机构、操作者、业务节点和参考资料，并跑通预约入库到双人核验的最小保管链，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态（含预约顺序、库位与设备占用、待确认交接）和审计历史继续保留。

## 样品交接与保管服务

### 角色

在基础角色（admin、operator、reviewer、auditor）之上扩展：warehouse（仓管）、secretary（评审秘书）、carrier（承运联络人）。评委即 reviewer，审计员只读。

### 业务流程

1. **预约入库**：`POST /custody/reservations` 按场所生成持久预约顺序；承运单可晚到，用 `POST /custody/reservations/waybill` 补登（已登记的不同单号会被拒绝而不是覆盖）。
2. **扫码收货**：`POST /custody/packages/scan` 登记包裹的封签、重量、照片摘要、保管条件与收货责任人；重复扫码只记录事件并返回原包裹，不会产生重复库存；运单与预约不符直接判为错投并进入异常流程。
3. **登记作品与组件**：`POST /custody/works`、`POST /custody/components`，组件数不能超过申报数，易损组件只能放入恒温恒湿设备或专用库位。
4. **双人核验**：`POST /custody/components/verify`，两名不同操作者各自核验后组件生效；作品的全部申报组件都通过核验才成为可评审样品。
5. **原子占用**：`POST /custody/components/assign-location`、`assign-equipment` 在事务内条件更新占用量；`POST /custody/loans` 在事务内拒绝时段重叠的外借。
6. **连续保管链**：`POST /custody/handovers` 发起评委借阅（loan_out）、复核转场（transfer）、归还（loan_return）与退件（return_out），交出方由服务端从最近已确认交接推导；`POST /custody/handovers/confirm` 由接收方另一名操作者确认后生效。
7. **冻结与异常**：确认时发现封签或重量差异、破损，或借阅逾期（`POST /custody/loans/sweep`），只冻结相关组件并生成异常单，同包裹其他作品不受影响；被冻结组件仍允许归还交接。异常由 `POST /custody/exceptions`、`POST /custody/exceptions/resolve` 独立闭环。
8. **补录证据**：`POST /custody/handovers/evidence` 只追加证据行，原交接记录确认后不可变。

### 查询与视图

- `GET /custody/views/warehouse?site_id=`：包裹、库位与设备占用、待确认交接、未决异常、活跃冻结；
- `GET /custody/views/secretary?site_id=`：预约顺序与到货进度、可评审作品、借阅日程、待核验组件；
- `GET /custody/views/carrier?site_id=&carrier_org=`：仅本承运方的预约与包裹（不含作品内容）；
- `GET /custody/views/auditor?site_id=`：异常、冻结、近期交接与 custody.* 审计事件；
- `GET /custody/components/chain?component_id=`：组件完整保管链与证据；
- `GET /custody/components/position?component_id=&at=`：按历史时点还原保管人与库位；
- `GET /custody/components/damage-interval?component_id=`：损伤责任区间（最后一次完好观测到损伤检出之间的保管人序列）。

所有写接口都支持 request_id 幂等：同一请求重放返回首次存储的结果，同一 request_id 携带不同内容会被拒绝。
