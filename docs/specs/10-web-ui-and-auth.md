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
| `viewer` | `resources:read`、`scans:read` |
| `operator` | `viewer` 的全部权限，加上 `scans:write`（触发扫描、刷新资源） |
| `admin` | `operator` 的全部权限，加上 `api_keys:manage` |

| 接口 | 需要的权限 |
|---|---|
| `GET /v1/resources*`、`POST /v1/resources/search`、`GET /v1/resource-types`、`GET /v1/accounts/{id}/coverage` | `resources:read` |
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

| 路由 | 内容 | 权限 |
|---|---|---|
| `#/` 看板 | 资源总数、账号数、类型数、近期 job 成功率；按类型、按 region 的数量条形图；最近的扫描 job | `resources:read`、`scans:read` |
| `#/resources` | 过滤（账号、region、类型、名称前缀、tag、含已删除）、表格、"加载更多"、详情抽屉（字段、tags、`data` JSON），"从 AWS 刷新" | 读：`resources:read`；刷新：`scans:write` |
| `#/scans` | 扫描 job 列表（状态、触发方式、账号过滤），"新建扫描"弹窗（账号 / 账号 + region / 账号 + 类型 / ARN） | 读：`scans:read`；新建：`scans:write` |
| `#/scans/:jobId` | job 进度、统计、task 表（含错误）、按相同 scope 重新扫描；非终态时每 2 秒刷新 | 同上 |
| `#/accounts/:accountId` | 账号覆盖情况：region × 类型的状态、数量、最近成功时间、错误；每行"扫描" | 读：`resources:read`；扫描：`scans:write` |
| `#/api-keys` | 列表、创建（名称、角色、过期时间；明文只显示一次）、撤销 | `api_keys:manage` |

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
