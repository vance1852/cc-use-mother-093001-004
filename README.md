# 追踪实物样品交接保管协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

在此之上，项目内置了面向组委会实物复核阶段的**样品交接与保管服务**（`custody` 模块）：

- **预约入库**：登记预约（持久序号保证到达顺序）、承运单与预期包裹数；包裹到达扫码登记包裹、作品、组件、封签、重量、照片摘要、保管条件（温湿度、朝向）与责任人；同一包裹码按内容哈希去重，重复扫码不会产生重复库存，同码不同内容直接冲突。
- **双人核验**：同一组件必须由两名不同核验人通过才能生效；只有全部组件双人核验通过、未冻结且无未结异常的作品才能组成可评审样品。
- **独立异常流程**：缺件、错投、破损分别建档、独立结案；核验不通过或封签差异会自动开立破损异常。
- **资源原子占用**：库位容量、特殊设备数量、外借时段在同一 IMMEDIATE 事务内检查并写入；易损作品的温湿度与朝向要求必须被库位环境完整覆盖。
- **连续保管链**：评委借阅、复核转场、返还、退件均由"发起—对方确认"两步完成，确认时逐组件核对封签；封签差异或逾期只冻结相关组件并开立异常，同包裹的完好组件不受影响；交接双方不能是同一人。
- **补录证据**：晚到的承运单、封签与现场说明只能追加，不能覆盖原交接记录。
- **角色视图**：仓管、评审秘书、承运联络人、审计员通过各自的 API 视图读取数据；支持损伤责任区间查询（定位到两次交接之间的保管人）和按历史时点还原组件位置与保管人。
- **重启保持**：预约顺序、资源占用与待确认交接全部保存在 SQLite 中，服务重启后继续有效。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `custody.py`：样品交接与保管领域服务；
  - `custody_acceptance.py`：样品保管端到端离线验收；
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
    PYTHONPATH=src python3 -m creative_program_foundation.custody_acceptance

验收命令会在临时 SQLite 数据库中完成登记链并核对幂等回执与审计链；样品保管验收额外覆盖
双人核验、原子占用、借阅返还、封签差异冻结、补录证据、责任区间与服务重启保持，
成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

样品保管接口统一挂在 `/custody/` 前缀下，全部写接口需要 `request_id` 幂等键：

- `POST/GET /custody/reservations`：预约入库与预约列表；
- `POST/GET /custody/packages`、`GET /custody/packages/detail`：扫码入库与包裹明细；
- `POST /custody/locations`、`POST /custody/equipment`：库位与特殊设备登记；
- `POST /custody/verifications`、`POST /custody/samples`：双人核验与组成可评审样品；
- `POST /custody/allocations`、`POST /custody/allocations/release`：库位/设备/外借时段占用与释放；
- `POST /custody/handovers`、`POST /custody/handovers/confirm`、`GET /custody/handovers/pending`：交接发起、确认与待确认列表；
- `POST /custody/exceptions`、`POST /custody/exceptions/resolve`：异常开立与结案；
- `POST /custody/components/freeze`、`POST /custody/components/unfreeze`：组件冻结与解冻；
- `POST /custody/evidence`：补录证据（只增不改）；
- `POST /custody/overdue/sweep`：逾期巡检并冻结相关组件；
- `GET /custody/components/chain`、`GET /custody/components/responsibility`、`GET /custody/components/location-at`：保管链、损伤责任区间与历史时点位置；
- `GET /custody/views/warehouse|secretary|carrier|auditor`：四种角色视图。
