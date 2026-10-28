# Spec 10：消费者 UI（baseui）与 API 认证 / RBAC

## 目标

1. 面向消费者的 Web UI：数据展示看板 + 扫描操作管理，基于 [baseui](https://baseweb.design)（Uber Base Web），替换 spec 07 §9 的原生 JS 页面。
2. API 预留认证与基于角色的访问控制：
   - 用户：内部 SSO 签发的 JWT（`Authorization: Bearer`），用户名为 8 位数字。
   - 服务：API key（`X-API-Key`），供其他服务调用。
3. 默认关闭认证（`API_AUTH_MODE=disabled`），接入 SSO 后改为 `enforced`，代码不需要改动。

不做：SSO 登录流程本身（跳转、回调、刷新 token）。UI 只预留获取 token 的接口和一个"粘贴 token"的开发入口。

## 1. 角色与权限

| 角色 | 权限 |
|---|---|
| `viewer` | `resources:read` |
| `operator` | `viewer` 的全部权限，加上 `scans:read`（扫描记录）、`scans:write`（触发扫描、刷新资源） |
| `admin` | `operator` 的全部权限，加上 `api_keys:manage` |

| 接口 | 需要的权限 |
|---|---|
| `GET /v1/resources*`、`POST /v1/resources/search`、`GET /v1/resource-types`、`GET /v1/accounts`、`GET /v1/accounts/{id}/coverage`、`/v1/history/*`（预留） | `resources:read` |
| `GET /v1/scans*` | `scans:read` |
| `POST /v1/scans`、`POST /v1/resources/refresh` | `scans:write` |
| `GET/POST/DELETE /v1/api-keys*` | `api_keys:manage` |
| `GET /v1/me` | 已认证即可 |
| `/healthz`、`/readyz`、`/ui/*`、`/docs`、`/openapi.json` | 不需要认证 |

## 2. 认证（`inventory/api/auth.py`）

```python
Permission = Literal["resources:read", "scans:read", "scans:write", "api_keys:manage"]
Role = Literal["viewer", "operator", "admin"]

@dataclass(frozen=True, slots=True)
class Principal:
    subject: str            # 用户：8 位数字用户名；API key：apikey:<key_id>；关闭认证时：anonymous
    kind: Literal["user", "service", "anonymous"]
    roles: frozenset[Role]
    @property
    def permissions(self) -> frozenset[Permission]: ...

def require(permission: Permission) -> Callable[..., Awaitable[Principal]]: ...   # FastAPI 依赖
```

- 每个请求最多带一种凭证：同时带 `Authorization` 和 `X-API-Key` 返回 401。
- `auth_mode=disabled`：不校验凭证，principal 为 `anonymous`，拥有全部角色（本地开发、迁移期）。但 API key 只能在 `enforced` 时管理（403）：否则那时创建的 key 在开启认证后仍是有效凭证。UI 在 `disabled` 时不显示 API key 页面。
- `auth_mode=enforced`：没有凭证或凭证无效返回 401 `UNAUTHENTICATED`；权限不足返回 403 `FORBIDDEN`。
- principal 写入日志上下文（字段 `principal`），访问日志与业务日志都带上。

### 2.1 JWT（SSO 用户）

- 用 JWKS 校验签名（`jwt_jwks_url`，PyJWT 的 `PyJWKClient`：key 集合缓存 300 秒、不按 `kid` 永久缓存，因此 SSO 移除的 key 最多 5 分钟后失效；获取超时 3 秒）；同时校验 `exp`、`iss`（`jwt_issuer`）、`aud`（`jwt_audience`），允许 30 秒时钟偏差，允许的算法为 `jwt_algorithms`（默认 `["RS256"]`）。
- 用户名取自 `jwt_username_claim`（默认 `preferred_username`），必须正好是 8 个 ASCII 数字（`[0-9]{8}` 全匹配），否则 401。
- 角色取自 `jwt_roles_claim`（默认 `roles`，字符串数组）：只认 `viewer` / `operator` / `admin`，其他值忽略；一个都没有时使用 `jwt_default_role`（默认 `viewer`）。
- 认证在独立的线程限制器中执行（PyJWT 与数据库调用是同步的）：JWKS 变慢时不会占满其他路由使用的线程。

### 2.2 API key（服务）

- 格式：`inv_<key_id>_<secret>`；`key_id` 为 ULID，`secret` 为 32 字节随机数的 base64url。
- 库中只存 `sha256(secret)`；校验时按 `key_id` 取记录，`hmac.compare_digest` 比较哈希；已撤销或已过期返回 401。
- `last_used_at` 最多每 5 分钟更新一次（先在内存中比较，避免每个请求都写库）。
- key 的角色在创建时指定，创建者必须是 admin。明文只在创建响应中返回一次。

### 2.3 配置（`ApiSettings`，前缀 `API_`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `auth_mode` | `disabled` | `disabled` / `enforced` |
| `jwt_jwks_url` | 无 | `enforced` 时必填 |
| `jwt_issuer` / `jwt_audience` | 无 | `enforced` 时必填 |
| `jwt_algorithms` | `["RS256"]` | |
| `jwt_username_claim` | `preferred_username` | |
| `jwt_roles_claim` | `roles` | |
| `jwt_default_role` | `viewer` | |
| `ui_dir` | 无 | 构建好的 UI 目录；未设置时使用包内的 `inventory/api/ui`（镜像中由构建阶段放入），目录不存在时不挂载 `/ui` 并记录 WARNING |

## 3. 数据（迁移 `0005`）

```sql
CREATE TABLE api_key (
  key_id       text        PRIMARY KEY,
  name         text        NOT NULL,
  key_hash     bytea       NOT NULL,
  roles        text[]      NOT NULL,
  created_by   text        NOT NULL,
  created_at   timestamptz NOT NULL,
  expires_at   timestamptz,
  revoked_at   timestamptz,
  last_used_at timestamptz
);
ALTER TABLE scan_job ADD COLUMN requested_by text;   -- 触发者（principal.subject）；定时任务为 NULL
```

只做加法（spec 08 R8）。`ApiKeyRepository` 放在 `db.py`（同步），API 中经线程调用。

## 4. 接口变化（同步更新 api.md 与 OpenAPI 快照）

- `GET /v1/me` → `{"subject", "kind", "roles", "permissions", "auth_mode"}`。
- `GET /v1/api-keys` → `{"items": [ApiKey]}`（不含明文与哈希）。
- `POST /v1/api-keys` `{"name", "roles", "expires_at"?}` → 201 `{"key": ApiKey, "secret": "inv_..."}`。
- `DELETE /v1/api-keys/{key_id}` → 204（撤销，记录保留）。
- `ScanJob` 增加 `requested_by`。
- 错误码：`UNAUTHENTICATED`（401）、`FORBIDDEN`（403）。401 响应带 `WWW-Authenticate: Bearer`。

## 5. Web UI（`web/`）

| 项目 | 选型 |
|---|---|
| 构建 | Vite + TypeScript，`npm`（`web/package-lock.json` 锁定） |
| 组件 | `baseui` 18 + Styletron（`styletron-engine-monolithic`、`styletron-react`），React 18 |
| 路由 | `react-router-dom` 6 的 `HashRouter`（静态托管在 `/ui/`，不需要服务端改写） |
| 主题 | baseui `LightTheme` / `DarkTheme`，跟随系统并可切换 |

页面：

面向使用者，而不是开发者：普通用户（viewer）只关心资源的现状；扫描相关的内容只给能发起扫描的人（`scans:write`，即 operator / admin）。

| 路由 | 内容 | 谁能看到 |
|---|---|---|
| `#/` 概览 | EIM 数、账号数、资源类型数、资源总数、数据更新时间（最近一次完成的定时全量扫描）；"资源分布"图：一个横向条形图，用按钮切换维度（EIM / 账号 / 账号类型 / 资源类型 / 区域），显示前 10 项，其余合并为"其他"；每根条按账号类型（dev / preprod / prod / 不在目录中）分段着色，颜色固定跟随账号类型，图例带各类型合计，悬停显示数量与占比，点击条或分段即筛选资源。账号显示为"名称（ID）"；最近变化的资源。扫描管理者另有"扫描"区：需要关注的扫描数、进行中的数量、最近的扫描 | 所有人；"扫描"区与"发起扫描"仅 `scans:write` |
| `#/resources` | EIM、账号、账号类型、资源类型、区域筛选（可勾选多个值，URL 中重复参数）、名称搜索、标签、含已删除，全部在一组里即时生效；表格列与筛选顺序一致：EIM、账号、账号类型、资源类型、区域、名称与 ARN、标签、最近变化。标签：`Name` 不重复显示，`aws:` 开头的系统标签排在最后，显示前 3 个并可点击筛选，其余为"+N"（悬停查看，点击打开详情）；"标签列"选择器可把常用的 key 设为独立的列（key 取自当前页的数据，按出现次数排序；选择记在 `localStorage`），此时原标签列改为"其他标签"；详情抽屉中标签超过 10 个时出现搜索框；分页为上一页 / 下一页（keyset cursor，没有跳页），每页 20 / 50 / 100 条（记在 `localStorage`），显示"第 x–y 条，共 n 条"；表格（手机上为卡片）；详情抽屉：按类型的关键属性、标签、记录（首次发现 / 最近变化 / 最近确认），ARN 在标题下方（可复制），ID 与原始数据收在"技术详情"；`?arn=` 直接打开详情 | 所有人；"从 AWS 重新获取""扫描此类资源"仅 `scans:write` |
| `#/scans`、`#/scans/:jobId` | 扫描列表（扫描 ID、范围层级标签：全部账号 / 账号 / 区域 / 类型 / 资源、状态、结果"新增 / 更新 / 删除"、发起人），按状态 / 来源 / 账号筛选，`?status=` 可直接筛选；详情页：扫描 ID，"范围"面板列出层级与具体的账号、区域、类型或资源 ARN；进度、计数、各区域与类型的结果，错误码翻译成说明，request_id、原始错误收在"技术详情" | 仅 `scans:write`（导航中也不显示） |
| `#/accounts/:accountId` | 账号概况与"类型 × 区域"矩阵，格子里是资源数，点击查看资源。扫描管理者另有：按扫描状态着色、"需要关注"计数、点击格子弹出状态 / 最近成功时间 / 问题与"立即扫描"，以及"扫描整个账号" | 所有人；扫描状态与操作仅 `scans:write` |
| `#/api-keys` | 列表、创建（名称、角色、过期时间；明文只显示一次）、撤销 | `api_keys:manage`，且认证为 `enforced` |

显示规则：

- **中英双语**：用户菜单与登录页可切换，默认跟随浏览器语言，保存在 `localStorage`。文案写在使用处（`t('English', '中文')`），不建 key 目录。
- 资源类型显示为常用名称并带服务色标（如"EC2 实例""S3 存储桶"）；区域保留代码（`ap-east-1`）。
- 状态与错误码显示为说明文字（`src/i18n/labels.ts`），原始值在"技术详情"中。
- 时间显示为本地时区的 `YYYY-MM-DD HH:mm`，不用"昨天""3 小时前"这类相对时间；悬停显示原始 UTC 值。
- 资源的 ARN 直接显示（列表中在名称下方，详情中在标题下方），可一键复制；列表中的账号、区域点击即按其筛选。
- viewer 没有 `scans:read`，概览不显示"数据更新时间"（它来自定时扫描记录）。

- 认证：`AuthProvider` 提供 `getToken()`。v1 的 token 来自 `sessionStorage`，由"粘贴 token"的开发入口写入；接入 SSO 时只替换 token 的获取方式。每个请求带 `Authorization: Bearer`；收到 401 时转到登录页。
- 启动时调用 `/v1/me`，按 `permissions` 隐藏或禁用没有权限的操作（服务端仍然校验）。
- 只调用已文档化的 `/v1` 接口；不使用 `dangerouslySetInnerHTML`，API 数据一律作为文本渲染。
- CSP：Styletron 在运行时注入 `<style>`，因此 `style-src` 需要 `'unsafe-inline'`；`script-src 'self'` 保持严格。

## 6. 构建与部署

- `deploy/Containerfile` 增加 node 构建阶段（`npm ci && npm run build`），输出复制到 api 镜像的 `inventory/api/ui/`；构建产物不进 git。
- 本地：`make web`（构建到 `web/dist`）。API 设置 `API_UI_DIR=web/dist` 后即可在 `/ui/` 访问；开发时用 `npm run dev`（Vite 代理 `/v1` 到 `localhost:8080`）。
- 删除 `inventory/api/ui/` 中的原生 JS 页面。

## 测试要求

| 用例 | 工具 / fixture |
|---|---|
| `auth_mode=disabled` 时匿名拥有全部权限；`/v1/me` | — |
| JWT：有效 token；过期、缺少 `exp`、错误 issuer / audience、错误签名、HS256（算法混淆）、用户名不是 8 个 ASCII 数字 → 401；角色映射、非列表的 `roles` 与默认角色 | 测试用 RSA 密钥与 JWKS：`api/auth.jwks.json`、`api/auth.sso_private_key.pem`、`api/auth.other_private_key.pem`；用例：`api/auth.tokens.cases.json`、`api/auth.principals.expected.json` |
| 权限矩阵：每个受保护接口在缺少权限时 403、有权限时通过；每个 `/v1` 路由都声明了权限（遍历路由检查） | `api/auth.permissions.cases.json` |
| API key：创建（只返回一次明文）、使用、撤销后 401、过期后 401、伪造 401、非 admin 不能管理、`disabled` 时不能管理 | PG |
| principal 出现在日志上下文中 | caplog |
| 同时带两种凭证 → 401 | — |
| `scan_job.requested_by` 记录触发者 | PG |
| 迁移 0005 可往返 | PG |
| UI 静态路由与安全头；未构建时不挂载；源码中没有 `dangerouslySetInnerHTML` / `innerHTML`；源码引用的 `/v1` 路径都在 OpenAPI 中 | `api/ui.*.json`、`tests/fixtures/api/ui/` |
| 前端类型检查与构建 | `npm run typecheck`、`npm run build` |

## 验收标准

- 以上测试通过；`make check` 与集成测试通过；`npm run build` 成功。
- 在浏览器中走通：看板、资源浏览与刷新、新建扫描并跟踪进度、账号覆盖、API key 的创建与撤销；`auth_mode=enforced` 时 viewer 看不到写操作，操作被服务端拒绝。
