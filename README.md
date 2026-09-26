# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`template`：同意书模板（含版本与正文摘要）；`consent`：同意书（草稿→已签→生效，退回补签后重签）；`sample`：样本；`withdrawal`：撤回申请。

## 同意书版本与快照

- 模板由伦理委员会改版后发布新版本（`publish`，可用`supersedes`指代旧模板）。
- 签署（`sign`）时冻结快照：模板版本、正文摘要、用途`scope`、有效期`expires_at`。
- 模板改版后：未签草稿自动切到新版；已签未生效的退回补签（状态`returned`，附退回原因）；已生效的仍按原快照办理，旧模板自动归档。
- 样本借出（`loan`）前自动核对快照；`GET /api/entities/<id>/loan_check`返回模板版本、摘要、冻结用途/有效期及拦截原因，演示页面可直观查看。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/entities/<id>/loan_check`：借出前快照核对。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
