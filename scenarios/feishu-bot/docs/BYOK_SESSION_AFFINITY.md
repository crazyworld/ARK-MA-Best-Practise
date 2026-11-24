# BYOK 会话亲和(X-Partner-Session-Id)

## 解决什么问题

BYOK Direct 模式下,模型请求由方舟 Managed Agent 的 Gateway 转发到企业自己的模型网关(如 bmc-llm-relay)。
同一个 MA Session 的多次模型调用(首轮、每次工具结果回传后的下一轮、失败重试)如果被负载均衡到不同后端,
上下文缓存无法复用,重试也可能落到没有该会话状态的节点。需要让网关能识别"这些请求属于同一个 Session"并按会话粘滞。

## 做法

在已有的 `X-User-Model-Header-*` 通用透传通道上多带一个 Header:

```
X-User-Model-Header-X-Partner-Session-Id: ma-<session_id>
```

方舟 Runtime 去掉 `X-User-Model-Header-` 前缀后,客户网关收到 `X-Partner-Session-Id: ma-<session_id>`。
BYOK 接入指南的规则是"需要多个 Header 时每个分别按此规则传入",值原样透传。

- 取值 `ma-` + session_id:前缀由使用方(模型网关侧)约定,用于区分来源。
- 同一个 Session 的每次请求取值一致,不同 Session 各自不同。
- 作用点:`ArkClient.send_message`(首条消息)与 `send_custom_tool_result`(工具结果回传)。`run()`、各 case 的
  重试 / 续跑 / 催促最终都走这两个方法,因此**凡是已经带 BYOK 鉴权头的入口**(通用 Gateway、topic6、数字员工、排障 Bot)
改一处即全部生效。**例外**:topic5 目前没有给 `send_message` 传 `extra_headers`(`topic5_runner.py` 的首条消息),
它没有 BYOK 头,所以不会带亲和头;topic5 要走 BYOK relay 时需先接入 BYOK 鉴权头,本 PR 不改。
- 首轮不会漏:`create_session` 先返回 session_id,再发首条消息;Header 随该 `POST /events` 一起提交,Agent Loop 在提交后才启动。

## 开关与配置

| 项 | 说明 |
|---|---|
| `BYOK_SESSION_AFFINITY=1`(环境变量 / config.env) | 开启。默认**关闭**,行为与升级前完全一致 |
| `ArkClient(..., session_affinity=True/False)` | 代码里显式指定,优先于环境变量 |

开启前提:relay 侧识别并按 `X-Partner-Session-Id` 做粘滞;未配置时多带一个 Header 无副作用但也没有收益。

## 两个必须保持的约束

1. **只在已带 BYOK 头的请求上追加**。方舟原生模型的 Session 收到任何 `X-User-Model-Header-*` 都会 400 `InvalidModelHeader`。
   topic6 对"切换模型前创建的旧 Session"会不带 BYOK 头(见 `Topic6Runner._headers_for`),此时亲和头也不追加。
2. **不修改调用方传入的字典**。各 gateway 的 `extra_headers` 是跨 Session 共享的同一个 dict,按 Session 写入会串号。

## 验证

- 单元测试:`tests/test_ark.py -k affinity`(6 项):首条消息与工具结果回传都带头且值相同、按 Session 取值、默认关闭、
  无 BYOK 头不追加、不修改入参、环境变量开关;另有一条走完整 `run()`(自定义工具调用 → 结果回传 → idle)的用例。
  已验证:去掉 `_byok_headers` 调用后其中 3 项失败。
- **未验证(本 PR 不含这些结论)**:
  1. relay 端是否真的按该 Header 粘滞、缓存命中率是否提升——需要在带 relay 的真实环境对比(同一 Session 多轮,观察 `cached_tokens`)。
  2. 方舟是否对自定义 Header 名做白名单——BYOK 指南没有写死,"多个 Header 分别传入"的措辞偏通用透传,但没有实测。
     建议首次开启时先发一条消息,确认请求未被方舟拒绝、relay 日志里能看到 `X-Partner-Session-Id`。

## 与既有配置的关系

不改变 `BYOK_MODEL_AUTH_HEADER_NAME/VALUE` 的含义与校验;亲和头是会话级动态值,不进 config。
