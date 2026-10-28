# Spec 01：broker v2（M1）

## 目标

实现凭证 broker Lambda：用 ADFS 表单登录拿到 SAML assertion，通过 `AssumeRoleWithSAML` 换取目标账号的 ReadOnly 临时凭证，并用 Redis 缓存以减少 ADFS 与 STS 调用。同时实现调用方客户端 `BrokerLambdaProvider`。

## 范围

做：broker 服务逻辑、Lambda 入口、请求/响应契约、调用方客户端。
不做：MFA、Windows 集成认证、Redis 跨 region 复制（方案 B 预留）。

## 文件清单

```
inventory/models.py              # broker 契约（本 spec 定义的部分）
inventory/broker/adfs.py         # AdfsClient
inventory/broker/saml.py         # SamlAssertion、RoleMap
inventory/broker/sts.py          # StsAssumer
inventory/broker/crypto.py       # EnvelopeCipher
inventory/broker/cache.py        # RedisCache（缓存 + 锁）
inventory/broker/service.py      # BrokerService
inventory/handlers/broker_lambda.py
inventory/aws.py                 # BrokerLambdaProvider（调用方）
tests/broker/...                 tests/fixtures/broker/...
```

## 1. 配置（`BrokerSettings`，前缀 `BROKER_`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `adfs_idp_url` | 必填 | 如 `https://adfs.corp.example/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices` |
| `adfs_credentials_secret_arn` | 必填 | Secrets Manager，内容 `{"username": "...", "password": "..."}` |
| `adfs_timeout_seconds` | 15 | |
| `adfs_ca_bundle` | None | 内网自签证书时指定 CA 文件路径 |
| `identity` | `default` | ADFS 登录身份名，作为缓存 key 的一部分 |
| `role_name` | `ReadOnly` | 从 assertion 中选择 role 时匹配 role 名（ARN 最后一段） |
| `redis_url` | 必填 | `rediss://host:6379/0` |
| `redis_auth_secret_arn` | None | |
| `kms_key_id` | 必填 | |
| `session_duration_seconds` | 3600 | 实际取 `min(本值, assertion 的 SessionDuration)` |
| `saml_refresh_margin_seconds` | 300 | assertion 到期前多久视为失效 |
| `default_min_ttl_seconds` | 1200 | |
| `lock_ttl_ms` | 45000 | 必须 ≥ `lock_wait_timeout_ms + 2 × adfs_timeout_seconds × 1000 + 5000`（凭证锁覆盖等 SAML 锁 + ADFS GET/POST + STS），启动时校验 |
| `lock_wait_timeout_ms` | 5000 | |
| `lock_poll_interval_ms` | 100 | |
| `data_key_ttl_seconds` | 3600 | KMS 数据密钥轮换间隔 |
| `adfs_failure_cache_seconds` | 300 | 密码错误（`ADFS_AUTH_FAILED`）后多久内不再尝试登录，防止锁定 AD 账号 |
| `redis_socket_timeout_seconds` | 2 | Redis 连接与读写超时 |

`session_duration_seconds` 必须 ≥ 900（STS 下限），启动时校验。

## 2. 契约（`inventory/models.py`）

请求（以 `action` 区分的联合类型）：

```python
class GetCredentialsRequest(BaseModel):
    action: Literal["get_credentials"]
    account_id: str = Field(pattern=r"^\d{12}$")
    role: str | None = None  # None 表示使用 BrokerSettings.role_name
    min_ttl_seconds: int = Field(1200, ge=0, le=3000)
    correlation_id: str | None = None


class ListAccessibleAccountsRequest(BaseModel):
    action: Literal["list_accessible_accounts"]
    role: str | None = None
    correlation_id: str | None = None


class GetSamlRequest(BaseModel):
    action: Literal["get_saml"]
    correlation_id: str | None = None


BrokerRequest = Annotated[
    GetCredentialsRequest | ListAccessibleAccountsRequest | GetSamlRequest,
    Field(discriminator="action"),
]
```

响应：

```python
class AwsCredentials(BaseModel):
    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr
    expiration: datetime


class GetCredentialsResponse(BaseModel):
    status: Literal["ok"] = "ok"
    account_id: str
    role_arn: str
    credentials: AwsCredentials
    source: Literal["cache", "sts"]


class ListAccessibleAccountsResponse(BaseModel):
    status: Literal["ok"] = "ok"
    role: str
    account_ids: list[str]  # 升序
    expires_at: datetime  # 对应 assertion 的失效时间


class GetSamlResponse(BaseModel):
    status: Literal["ok"] = "ok"
    saml_response: SecretStr  # base64
    expires_at: datetime


class BrokerErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    code: BrokerErrorCode
    message: str
    retryable: bool
```

- `SecretStr` 字段在 `repr` 与日志中显示为 `**********`；序列化为 JSON 返回给调用方时通过 `field_serializer(when_used="json")` 输出真实值。
- 示例请求/响应放在 `tests/fixtures/broker/*.request.json` / `*.response.json`，也作为文档参考。

错误码：

| code | retryable | 场景 |
|---|---|---|
| `ACCESS_DENIED` | 否 | 账号不在 assertion 的 role 列表中，或 STS 返回 AccessDenied |
| `ADFS_AUTH_FAILED` | 否 | 用户名或密码错误（需要告警） |
| `ADFS_UNAVAILABLE` | 是 | ADFS 超时、5xx、页面无法解析 |
| `THROTTLED` | 是 | STS 限流 |
| `INVALID_REQUEST` | 否 | 请求校验失败 |
| `INTERNAL` | 是 | 其他错误（Redis、KMS 故障等） |

## 3. 组件

### 3.1 `AdfsClient`（标准 ADFS 表单登录）

```python
class AdfsClient:
    def __init__(self, idp_url: str, credentials: Callable[[], tuple[str, SecretStr]], ...): ...
    def login(self) -> str: ...  # 返回 base64 SAMLResponse
```

`credentials` 在每次登录时调用（Lambda 中读取 Secrets Manager，带 300 秒缓存），密码轮换后无需冷启动即可生效。

1. 用 `httpx.Client(follow_redirects=True)` GET `adfs_idp_url`，得到登录页 HTML。
2. 用标准库 `html.parser` 解析登录表单：表单 `action`、所有 input（未勾选的 checkbox/radio 除外），加上 `UserName`、`Password`、`AuthMethod=FormsAuthentication`。
3. POST 表单（同一 client 保持 cookie）。
4. 在响应 HTML 中查找 `<input name="SAMLResponse" value="...">`：
   - 找到 → 返回。
   - 仍是登录页**且**有 `errorText` 错误提示 → `ADFS_AUTH_FAILED`。
   - 其他（包括没有错误提示的登录页）→ `ADFS_UNAVAILABLE`。
5. 网络错误、超时、5xx → `ADFS_UNAVAILABLE`。

接入真实环境时若流程不同，只修改此类。

### 3.2 `SamlAssertion`

```python
@dataclass(frozen=True)
class RolePair:
    role_arn: str
    principal_arn: str


@dataclass(frozen=True)
class SamlAssertion:
    raw_b64: str
    expires_at: datetime  # Conditions/@NotOnOrAfter
    session_duration: int | None  # SessionDuration 属性
    roles: dict[str, list[RolePair]]  # account_id -> roles

    @classmethod
    def parse(cls, saml_b64: str) -> "SamlAssertion": ...
    def find_role(self, account_id: str, role_name: str) -> RolePair | None: ...
```

- 用 `defusedxml.ElementTree` 解析，禁止使用标准库 `xml.etree` 直接解析外部输入。
- Role 属性名 `https://aws.amazon.com/SAML/Attributes/Role`，值形如 `role_arn,principal_arn`，两者顺序可能互换，按 `:role/` 与 `:saml-provider/` 识别。
- broker 不校验签名（由 STS 校验）。

### 3.3 `StsAssumer`

```python
def assume(self, assertion: SamlAssertion, pair: RolePair) -> AwsCredentials: ...
```

- 调用 `sts.assume_role_with_saml`，`DurationSeconds = min(session_duration_seconds, assertion.session_duration or ∞)`。
- 错误映射：`ExpiredTokenException`、`InvalidIdentityToken` → 抛 `SamlExpired`（由 service 清除 SAML 缓存后重试一次）；`AccessDenied` → `ACCESS_DENIED`；限流类 → `THROTTLED`；其他 → `INTERNAL`。

### 3.4 `EnvelopeCipher`

- 加密：使用缓存的数据密钥（KMS `GenerateDataKey` AES_256，每 `data_key_ttl_seconds` 换一次）做 AES-GCM，输出 `{"v": 1, "ek": <加密的数据密钥 b64>, "iv": ..., "ct": ...}`。
- 解密：按 `ek` 查本地 LRU（容量 8）中的明文数据密钥，未命中调用 KMS `Decrypt`。
- KMS EncryptionContext：`{"app": "inventory-broker", "kind": "cred" | "saml"}`。

### 3.5 `RedisCache`

| Key | 值 | TTL |
|---|---|---|
| `broker:saml:{identity}` | 加密的 `{"v": 1, "saml_b64"}` | `expires_at - now - saml_refresh_margin_seconds` |
| `broker:roles:{identity}` | Hash：`account_id → JSON[[role_arn, principal_arn]]`（明文，不含敏感信息），`list_accessible_accounts` 直接读取 | 同上 |
| `broker:cred:{account_id}:{role}` | 加密的 `{"v": 1, "role_arn", "credentials", "issued_at"}` | `expiration - now` |
| `broker:adfs_failed:{identity}` | 最近一次 `ADFS_AUTH_FAILED` 的消息 | `adfs_failure_cache_seconds` |
| `broker:lock:saml:{identity}` / `broker:lock:cred:{account_id}:{role}` | 随机 token | `lock_ttl_ms` |

- 加锁：`SET key token NX PX lock_ttl_ms`。
- 释放：Lua 脚本，token 一致才删除。
- 等锁：每 `lock_poll_interval_ms` **重试加锁**并重读目标缓存 key——命中即返回；加锁成功则先再读一次缓存再自己获取（持锁方失败时只有一个等待者接手，不会在超时后一起涌入）。超过 `lock_wait_timeout_ms` 仍未拿到，则不加锁自行获取（记录 WARNING）。
- 读取时任何解密或解析失败（KMS 故障、格式版本 `v` 不符等）都记录 WARNING、删除该 key 并视为未命中，不会让一条坏数据阻塞到 TTL 过期。

### 3.6 `BrokerService`

`get_credentials(req)`：

```
1. cred = cache.get_cred(account, role)
   剩余有效期 >= min_ttl → 返回 (source=cache)，指标 CacheHit
2. 获取 cred 锁；没拿到 → 等锁（见 3.5），期间命中则返回
3. 拿到锁后再读一次 cred（可能已被其他请求写入）
4. assertion = get_assertion()          # 内部同样是"缓存 → 锁 → ADFS 登录"
5. pair = assertion.find_role(account, role)；None → ACCESS_DENIED（指标 AccessDenied）
6. creds = sts.assume(assertion, pair)
   SamlExpired → 清除 saml 缓存，回到第 4 步，只重试一次
7. cache.set_cred(...)，释放锁，返回 (source=sts)
```

- "足够新"的判断为：剩余有效期 ≥ `min(min_ttl, 会话总时长 / 2)`。会话时长短于 `min_ttl`（例如 SAML `SessionDuration` 较短）时，不会每次请求都重新 AssumeRole；调用方 `BrokerLambdaProvider` 使用同样的规则。
- `ADFS_AUTH_FAILED` 会写入 `broker:adfs_failed:{identity}`，期间直接返回同一错误、不再登录。
- `list_accessible_accounts`：取 assertion → 列出 roles 中含 `role_name` 的账号。
- `get_saml`：取 assertion → 返回。

### 3.7 Lambda 入口（`inventory/handlers/broker_lambda.py`）

- 模块级初始化（跨调用复用）：settings、Redis 连接池、KMS/STS client、EnvelopeCipher、BrokerService。
- 装饰器：`@logger.inject_lambda_context(correlation_id_path="correlation_id")`、`@tracer.capture_lambda_handler`、`@metrics.log_metrics`。
- 用 `TypeAdapter(BrokerRequest)` 校验 event；把 service 结果或错误转换成响应模型，返回 `model_dump(mode="json")`。
- 业务错误**作为正常返回值**（`status=error`），不抛出；只有未捕获的崩溃才会让 Lambda 返回 FunctionError。
- 每次调用输出一条汇总日志：`action account_id source duration_ms code`。

### 3.8 调用方 `BrokerLambdaProvider`（`inventory/aws.py`）

```python
class BrokerTransport(Protocol):
    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]: ...

class LambdaBrokerTransport:      # boto3 lambda.invoke(RequestResponse)，endpoint 经 EndpointResolver
class BrokerLambdaProvider:       # 实现 CredentialProvider 接口
    def credentials(self, account_id: str, min_ttl_seconds: int = 1200) -> AwsCredentials: ...
    def session(self, account_id: str) -> boto3.Session: ...   # botocore RefreshableCredentials
    def accessible_accounts(self) -> set[str]: ...
```

- 进程内缓存每个账号的凭证（每个账号一把锁，避免同一进程重复请求）。
- `session()` 用 `RefreshableCredentials.create_from_metadata`，`refresh_using` 调用 `credentials(account, 1200)`。
- 请求中自动带上当前上下文的 `request_id`（没有则用 `job_id`）作为 `correlation_id`。
- 错误映射（retryable 只由错误码决定，不信任响应里的 `retryable` 字段）：`ACCESS_DENIED` → `AccessDeniedError`；`THROTTLED` → `ThrottledError`；`ADFS_UNAVAILABLE`、`INTERNAL` → `UpstreamError(retryable=True)`；`ADFS_AUTH_FAILED`、`INVALID_REQUEST` → `UpstreamError(retryable=False)`。
- 响应无法解析（JSON 错误、字段校验失败）→ `UpstreamError(retryable=True)`，消息固定且不链接原异常（校验错误文本可能包含凭证片段）。
- `LambdaBrokerTransport` 使用独立的 client 配置：`read_timeout=70`（大于 broker 函数超时 60 秒）、最多 2 次尝试，避免一次慢调用被 botocore 重试成多次 ADFS/STS 调用。

## 4. 日志与指标

- 指标（维度 `Service=inventory-broker`）：`CacheHit`、`CacheMiss`、`AdfsLogin`、`AdfsLoginFailed`、`StsAssume`、`AccessDenied`、`LockWaitMs`。
- **绝不**记录 assertion、凭证、密码；依赖 `RedactionFilter` 作为兜底，但代码中也不得把这些值传给 logger。

## 测试要求

| 用例 | 工具 / fixture |
|---|---|
| ADFS 登录成功、密码错误、页面异常、超时 | respx；`broker/adfs/login_page.html`、`saml_post.html`、`login_failed.html` |
| SAML 解析：多账号 role、顺序互换、缺少 SessionDuration | `broker/saml/assertion_multi_role.xml`、`assertion_multi_role.expected.json` |
| STS 成功、AccessDenied、过期重试一次 | moto（`assume_role_with_saml`） |
| 加解密往返、数据密钥轮换、错误 EncryptionContext 解密失败 | moto KMS |
| 缓存命中、锁竞争（两个线程同时请求只调用一次 STS）、等锁超时 | fakeredis[lua] |
| handler：每种 action 的请求与响应 | `broker/*.request.json`、`*.response.json`（Lambda context 用测试替身） |
| 日志中不出现 `secret_access_key`、`session_token`、`saml_response` 的真实值 | caplog |
| `BrokerLambdaProvider`：错误码映射、进程内缓存、自动刷新 | 假的 `BrokerTransport`，响应来自 fixture |

## 验收标准

- 以上测试全部通过，`inventory/broker/` 覆盖率 ≥ 90%。
- 同一账号 100 次并发 `get_credentials`（fakeredis）只产生 1 次 STS 调用。
- 集成测试（`-m integration`）使用 compose 中的真实 Redis，加上 moto 与 respx，跑通 handler 的 `get_credentials` 全流程。

## 部署修订（收尾审查后）

- `sts_region` 在 AWS 上配置为默认启用的 region（Terraform 默认 us-east-1），通过该 region 的 endpoint-specific `sts` endpoint 经 peering 访问；ap-east-1 是 opt-in region，未启用它的目标账号在那里 `AssumeRoleWithSAML` 会失败。regional STS 签发的凭证在所有 region 有效。
- broker 的 Secrets Manager 与 KMS client 使用 `INVENTORY_HOME_REGION`，与 `sts_region` 无关。

### 发布与配置修订（spec 08 R4）

- 删除 `sts_region`：`AssumeRoleWithSAML` 固定在 us-east-1 调用（所有账号默认启用；primary region ap-east-1 是 opt-in），跨 region 地址来自 `INVENTORY_ENDPOINTS`。Secrets Manager 与 KMS 使用 `INVENTORY_PRIMARY_REGION`。
- Lambda 函数名 `inventory-<env>-adfs-broker`。
