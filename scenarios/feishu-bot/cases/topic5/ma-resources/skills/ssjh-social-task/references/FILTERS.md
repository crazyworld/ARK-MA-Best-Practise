# 广告与互动参数契约

v0.9.9。上游不传时：不限广告、不限互动；这是明确默认值，不由Agent临时选择。字段位于任务配置的`search.filters`。

```yaml
search:
  filters:
    ad: all                 # all / exclude / only
    interaction_min: null   # 非负整数或null
    interaction_max: null   # 非负整数或null
```

例如`{ad: exclude, interaction_min: 100, interaction_max: null}`表示仅非广告且互动量不少于100。`{interaction_max: 100}`表示只取有互动数值且不超过100的内容。`{interaction_min: 0, interaction_max: 0}`表示互动量恰为0，不能包括缺失值。null表示无边界，两端null才不发送互动筛选；不发送`"-"`或`[null,null]`。

| 配置 | 任务searchCondition | 声量customConditions（本次source） |
|---|---|---|
| ad=all | 省略isAdMap | 不发送is_ad |
| ad=exclude | isAdMap[source]=0 | tConditions[0].is_ad=["0"] |
| ad=only | isAdMap[source]=1 | tConditions[0].is_ad=["1"] |
| min=100,max=null | interactionRangeMap[source]="100-" | tConditions[0].interaction_cnt=[100,null] |
| min=null,max=100 | interactionRangeMap[source]="-100" | tConditions[0].interaction_cnt=[null,100] |
| min=0,max=100 | interactionRangeMap[source]="0-100" | tConditions[0].interaction_cnt=[0,100] |

当前只接受一组全局条件，作用于所有选择的平台。任务按实际source写map，不使用优先覆盖其他项的all键；快手/视频号共用shortVideo并保留原站点筛选。声量每个请求仅一个source，customConditions.sources明确为该source；两个筛选同时启用时写入同一tConditions对象。仍保留主回帖、时间、关键词、排除词等原条件。

参数先通过本地类型/枚举/范围检查，再允许进入预检收费步骤。不支持逐平台覆盖；错误字段必须拒绝，不忽略。变更筛选改变请求指纹，原确认及缓存不能用于新条件；同请求ID不会静默变为新付费任务。原任务继续用原配置恢复，新业务请求按现有中台授权规则执行。

依据：官方[创建文档3](https://dc.datastory.com.cn/api/doc?id=3)“数据搜索”searchCondition.isAdMap / interactionRangeMap、[声量文档14](https://dc.datastory.com.cn/api/doc?id=14)customConditions、[通用示例43](https://dc.datastory.com.cn/api/doc?id=43)，2026-10-31快照。任务文档明确gte/lte为包含边界；声量文档说明互动区间排除空值。成对真实计数/边界/组合关系仍待验证，离线测试不能替代供应商验证。

供应链互动缺失是常态。默认保留，显式互动过滤将排除；这是请求范围改变，不是修补数据质量。统计只估算采集规模，费用门禁沿用既有规则，不能靠下载后过滤降低已发生费用。
