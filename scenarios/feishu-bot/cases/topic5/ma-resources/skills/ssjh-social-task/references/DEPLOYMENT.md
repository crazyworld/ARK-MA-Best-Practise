# v0.11.6 内部实测部署

这是2026-11-06用户授权的内测配置：官方HTTP统计/计数/下载，HTTPS登录/创建/状态，内置内部价表。不是生产传输验收结论。

## 首次部署

1. 将完整Skill目录放到宿主认可的位置；保留旧版及正在运行任务，不覆盖旧run。用独立Python 3.10+环境安装requirements.txt。不需要飞书CLI或另装Stat Skill。
2. 管理员将SSJH_USERNAME、SSJH_PASSWORD、SSJH_FIXED_TOKEN安全注入实际执行进程；环境变量值不写入命令日志、聊天或共享任务配置。默认包不含凭证。
3. 将任务config放在使用者工作区，设定实际日期、关键词、平台、request_id和输出位置；output.template指向包内template.yaml实际路径。不要直接运行随包示例作为真实业务任务。
4. 使用同一解释器运行preflight，配置/依赖/预算加载和主机连接均通过后，再execute预检。确认数量后创建；持续运行仍由调用方推进。

```text
python SKILL_DIR/ssjh_social_task.py --action preflight --config TASK_CONFIG --api-config SKILL_DIR/api_config.yaml
```

preflight的HTTPS部分验证证书，HTTP部分仅检查TCP80；不发送账号、token、任务或数据请求。PASS不能证明固定token有效、账号权限或业务取数通过。doctor只检查本地，不联网，仍可用来诊断。

## 费用默认值与覆盖

价表由internal/pricing.json提供，只包含预算脚本需要的字段；pricing_integrity.py保存其SHA256，发现意外修改即停止。模型不能读取费用内容；脚本输出不返回单价或费用。用户明确授权本版内置，不要求分析师另外搜集价格。

默认台账目录为~/.ssjh_private/budget，第一次实际预算操作创建；doctor/preflight不创建。默认registry仍为~/.ssjh-social-task/requests.sqlite3，两者和run均需持久保存。同一业务请求不能通过删除台账、换目录或换请求ID重新获得额度。

管理员已设置SSJH_PRICING_FILE或SSJH_BUDGET_DIR时分别优先使用；显式空值/不存在的指定目录/非法价表仍停止，不以默认值掩盖部署错误。手动价表须在Skill和业务run外；指定目录先由管理员创建。旧付费任务必须保留其原价表/账本，不因新默认值另建账本。

覆盖价表格式：schema_version整数1；currency为CNY；stat_success为正的十进制字符串；record_rates为平台键至正十进制字符串的映射。平台键与PLATFORMS一致。该格式供维护者核对，不让Agent自行填价；合成验收的假费率不能用于真实请求。

SHA校验防意外修改，不是防篡改授权或加密。能读取完整包的同一OS身份可以读到或推断费用信息；SKILL拒绝费用披露、错误脱敏及输出白名单只能约束正常Agent/程序通道，不保证恶意Agent文件访问下保密。严格保密和跨请求费控交给中台，AUTH-CENTRAL-01保持待实现。

## 传输范围

api.base_url=https://dc.datastory.com.cn。api.count_base_url=http://api.dc.datastory.com.cn/api，且runtime.transport_profile=official-http-internal时，传输层才接受这一精确官方HTTP地址。其他远程HTTP拒绝；字面回环IP继续仅供本地合成测试/可信网关。

这是明确配置的官方HTTP内测，不是HTTPS失败后的自动回退；不关闭证书校验、不改hosts/Host/SNI。固定取数token和原文在HTTP段明文传输，风险已由维护者确认。要改用安全网关须先核实接口兼容和全链路保护，不仅看网关入口。不要把该内测配置直接作为生产上线标准。

新旧端点变化会改变预检/缓存绑定。旧完成任务本地导出使用原API配置；原HTTP已付费任务若需要联网恢复，保持同一端点，仅由维护者补入本版transport_profile后继续原job。不能手改state/manifest或换job。旧HTTPS配置可继续使用，但证书当前仍失败，不自动更改。

## 离线与宿主验收

```text
python SKILL_DIR/scripts/check_runtime.py --workspace EXISTING_WORKSPACE --runtime ada
python SKILL_DIR/scripts/offline_acceptance.py --output NEW_EXTERNAL_DIRECTORY
```

check_runtime验证本地卷/依赖；offline_acceptance使用假凭证/费率、127.0.0.1服务和125条合成数据，约2—4分钟，结束关闭服务；输出目录必须不存在，不指向客户run。结果不证明宿主实际加载、权限隔离或真实供应商采集。

Codex/Claude Code/OpenClaw/Hermes保持基础CLI兼容范围；无环境不称运行验收。ADA后续验证加载、身份、持久卷恢复、真实取数、通知及中台注入监测插件。本版不新增监测平台或后台任务。
