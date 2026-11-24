# 平台契约（v0.11.6发布范围，映射规则自v0.11.0）

使用数据搜索任务接口。source不是站点名称；同一阵地的站点可逗号多选，站点编号不能替换为数据超市appId或scopeId。

| 平台键（估算/计价/日志） | search.source | 配置区与site | msg_type |
|---|---|---|---|
| weibo | weibo | weibo，无site | 0主帖+回帖/1主帖/2回帖 |
| weixin | weixin | weixin，无site | 仅1公众号文章；不发送weixinMsgType |
| xiaohongshu | xiaohongshu | xiaohongshu，无site | 0笔记+评论/1笔记/2评论 |
| douyin | shortVideo | short_video.site: "1003583" | 0主帖+评论/1主帖/2评论 |
| kuaishou | shortVideo | short_video.site: "1334510" | 同上 |
| shipinhao | shortVideo | short_video.site: "25047925" | 同上 |
| bilibili | video | video.site: "44" | 同上；不提供独立弹幕开关 |
| tencent_video | video | video.site: "35" | 同上 |
| iqiyi | video | video.site: "47" | 同上 |
| zhihu | qa | qa.site: "101944" | 0问题+回答/1问题/2回答 |
| baidu_zhidao | qa | qa.site: "102026" | 同上；不是回答下评论 |
| news | news | news.site可选，原规则不变 | 0主帖+评论/1主帖/2评论 |
| forum | forum | forum.site可选，原规则不变 | 0主帖+回帖/1主帖/2回帖 |

例：source: "weixin,shortVideo,video,qa"，short_video.site: "1003583,25047925"，video.site: "44,35,47"，qa.site: "101944,102026"。各区msg_type须按业务目标明确；同一区多个站点共用该枚举，程序不静默拆成收费任务。

账号字段：weibo_uid、biz（微信公众号）、xhs_acc_id、sv_acc_id（抖音）、kuaishou_uid、shipinhao_uid、bili_acc_id、zhihu_uid。非空账号必须属于已选平台；没有账号接口的站点使用关键词。账号与关键词同时填写共同生效，预检无法证明组合等价则需逐平台上游估算。

内容类型可在short_video/video/qa区指定post_types，单站点可用字符串，多站点建议用按平台键的映射：

```yaml
short_video:
  site: "1003583,1334510,25047925"
  msg_type: 1
  post_types:
    douyin: "video#0,image#0,video#1,image#1"
    kuaishou: "video#0,image#0"
video:
  site: "44,35,47"
  msg_type: 1
  post_types:
    bilibili: "video,dynamic,article"
qa:
  site: "101944,102026"
  msg_type: 0
  post_types:
    zhihu: "questions,articles,pins"
```

未指定类型不新增筛选。抖音/B站/知乎类型映射到两端对应postTypes，快手仍仅任务侧保留、统计侧放宽。类型和msg_type的供应商组合语义尚未实测，新平台估算均为UNCERTAIN，不能宣称声量是精确账单上限。字段适用性由代码检查，微信、百度知道、知乎及普通长视频只接受content/title；B站额外支持cover_ocr_content/src_title/src_content。抖音commodity_title仅主帖时允许，防止评论查询放大。

## 收费与验收边界

每个已选平台必须在价表record_rates中有同名键；v0.11.5按维护者授权内置已确认的13平台映射，管理员可显式覆盖，不推断未确认平台同价。缺单价在首次统计调用前停止。v0.11.0/v0.11.2的两批真实结果共412条，已取得13平台非零字段结构样本；不同账号权限、历史覆盖、全部内容类型仍需按实际条件验收。预计时间不套用既有微博样本。

搜狐视频：官方数据超市appId=143、scopeId=128，不能当videoSite。2026-11-01数据搜索站点清单未列该站，当前账号数据超市可见列表亦未列出；需确认接口权限、创建响应/任务查询链路和对应计费契约。本版对sohu_video显式返回SOHU_MARKET_CONTRACT_REQUIRED，未接入，不自动用搜狐新闻替代。

新预检contract_policy_version=4、估算计划version=2；未提交的旧预检重做。已有付费任务继续原ID/查询，不重建；原六类默认查询载荷保持一致，导出校验继续使用轻量字段字典，未伪称全平台完整字典。
