# 返回数据字典与校验约定

版本：0.2.1-provisional（schema 1.1）。包内 data_dictionary.json 是运行时规则；本表由其生成。覆盖13类已接入平台、65项字段，仍是暂定轻量字典，不是供应商完整官方字典。

## 证据与适用范围

平台注册、字段实际出现、字段业务含义分别登记。当前两批共412条留存数据覆盖13类非零平台，其中新增腾讯视频3条、爱奇艺64条、百度知道18条。历史样本已获用户接受，用于字段字典不设新鲜度阻断。每字段 platform_evidence 仅描述本次观察；未出现保持未知，不能推断不适用、接口故障或永不返回。

官方文档5（计数/下载）和43（统计示例）仅提供响应示例；示例不能替代全量字段定义，尤其不能将统计原文接口的字段存在外推为任务接口全平台支持。旧微博906条结构证据与既有实现映射保留为历史。来源哈希见 evidence_catalog，实际响应正文、作者、关键词和私有费用材料均不打包。

| 平台 | 样本条数 | 样本发布日期范围 | 证据状态 |
|---|---:|---|---|
| weibo | 3 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| weixin | 12 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| xiaohongshu | 40 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| news | 92 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| forum | 5 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| douyin | 121 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| kuaishou | 21 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| shipinhao | 1 | 2026-10-31 至 2026-10-31 | 真实结构观察，语义未认证 |
| bilibili | 30 | 2026-10-25 至 2026-10-31 | 真实结构观察，语义未认证 |
| tencent_video | 3 | 2025-11-10 至 2026-09-09 | 真实结构观察，语义未认证 |
| iqiyi | 64 | 2025-11-02 至 2026-10-25 | 真实结构观察，语义未认证 |
| zhihu | 2 | 2026-10-29 至 2026-10-30 | 真实结构观察，语义未认证 |
| baidu_zhidao | 18 | 2025-11-08 至 2026-02-17 | 真实结构观察，语义未认证 |

## 字段规则

所有字段除id外均可选。出现时校验类型；不强制转换、不改原值。下表观察平台是有限样本证据，既不是支持白名单也不是排除范围。

| 原始字段 | 说明 | 校验类型 | 观察平台 | 缺失处理 |
|---|---|---|---|---|
| site_name | 平台 | text | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| data_type | 内容类型 | text | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| id | 内容ID | identifier | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 阻断 |
| url | 原文链接 | url | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| title | 标题 | text | weixin, xiaohongshu, news, forum, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| content | 正文 | text | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| publish_time | 发布时间 | timestamp | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| crawl_time | 抓取时间 | timestamp | 未观察到 | 被选中时告警 |
| sentiment | 情感 | sentiment | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| reposts_count | 转发数 | count | 未观察到 | 被选中时告警 |
| comments_count | 评论数 | count | 未观察到 | 被选中时告警 |
| attitudes_count | 点赞数 | count | 未观察到 | 被选中时告警 |
| reads_count | 阅读数 | count | 未观察到 | 仅统计 |
| interaction | 互动量 | count | 未观察到 | 被选中时告警 |
| author | 作者昵称 | text | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, iqiyi, baidu_zhidao | 被选中时告警 |
| author_url | 作者主页 | url | weibo, xiaohongshu, news, douyin, kuaishou, bilibili, zhihu | 被选中时告警 |
| uid | 作者ID | identifier | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu | 被选中时告警 |
| fans_cnt | 粉丝数 | count | weibo, xiaohongshu, news, douyin, kuaishou, bilibili, zhihu | 被选中时告警 |
| gender | 性别 | opaque_enum | weibo, xiaohongshu, news, douyin, kuaishou, shipinhao, bilibili, zhihu | 被选中时告警 |
| province | 省份 | text | weibo, douyin, shipinhao | 被选中时告警 |
| city | 城市 | text | weibo, douyin, kuaishou, shipinhao | 被选中时告警 |
| verified_type | 认证类型 | opaque_enum | weibo | 被选中时告警 |
| verified_reason | 认证说明 | text | weibo, news, douyin, shipinhao, bilibili | 被选中时告警 |
| vtype | 用户类型 | opaque_enum | weibo, xiaohongshu, news, douyin, kuaishou, shipinhao, bilibili | 被选中时告警 |
| profilers_name | 分析对象 | text_list | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| profilers_id | 分析对象ID | identifier_list | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| update_time | 数据更新时间 | timestamp | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 被选中时告警 |
| reposts_cnt | 转发数 | count | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili | 仅统计 |
| review_cnt | 评论数 | count | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| like_cnt | 点赞数 | count | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi | 仅统计 |
| interaction_cnt | 互动量 | count | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| video_view_cnt | 待核实：视频观看相关字段 | count | 未观察到 | 仅统计 |
| site_id | 站点ID | identifier | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| item_id | 站内原始内容ID | identifier | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| cat_id | 原始分类编码（值域待核实） | opaque_enum | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| post_type | 原始帖子类型 | opaque_enum | weibo, news, douyin, kuaishou, bilibili, zhihu, tencent_video, iqiyi | 仅统计 |
| is_main_post | 主帖标记（保留原枚举） | opaque_enum | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| content_mode | 内容生产类型（保留原枚举） | opaque_enum | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| is_ad | 广告标记（保留原枚举） | opaque_enum | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| coin_cnt | coin_cnt（口径待核实） | count | weibo, xiaohongshu, news, douyin, kuaishou, zhihu | 仅统计 |
| barrage_cnt | barrage_cnt（口径待核实） | count | bilibili | 仅统计 |
| fav_cnt | fav_cnt（口径待核实） | count | weibo, xiaohongshu, news, forum, douyin, kuaishou, bilibili, zhihu | 仅统计 |
| favorite_cnt | favorite_cnt（口径待核实） | count | xiaohongshu, zhihu | 仅统计 |
| collection_cnt | collection_cnt（口径待核实） | count | xiaohongshu | 仅统计 |
| collection_video_cnt | collection_video_cnt（口径待核实） | count | douyin, kuaishou, bilibili | 仅统计 |
| view_cnt | view_cnt（口径待核实） | count | weixin, news, douyin, kuaishou, bilibili, tencent_video | 仅统计 |
| like_share_cnt | like_share_cnt（口径待核实） | count | weixin | 仅统计 |
| display_cnt | display_cnt（口径待核实） | count | news | 仅统计 |
| duration_seconds | duration_seconds（单位/口径待核实） | nonnegative_number | xiaohongshu, douyin, kuaishou, shipinhao, bilibili, tencent_video | 仅统计 |
| pic_urls | pic_urls（原始媒体地址） | text_or_text_list | weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi | 仅统计 |
| video_urls | video_urls（原始媒体地址） | text_or_text_list | weixin, xiaohongshu, news, douyin, kuaishou | 仅统计 |
| audio_urls | audio_urls（原始媒体地址） | text_or_text_list | douyin, kuaishou | 仅统计 |
| topics | 原始话题列表 | text_list | weibo, xiaohongshu, forum, douyin, kuaishou, bilibili | 仅统计 |
| keywords | 原始关键词列表 | text_list | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi, baidu_zhidao | 仅统计 |
| cover_ocr_content | cover_ocr_content（原始文本） | text | xiaohongshu, news, douyin, kuaishou, shipinhao, bilibili | 仅统计 |
| audio_asr_content | audio_asr_content（原始文本） | text | shipinhao | 仅统计 |
| video_highlight_content | video_highlight_content（原始文本） | text | shipinhao | 仅统计 |
| video_content | video_content（原始文本） | text | douyin, shipinhao | 仅统计 |
| other_data | 供应商扩展信息（保留原JSON文本/对象） | json_text_or_object | xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, zhihu, tencent_video, iqiyi | 仅统计 |
| fingerprint | 原始指纹（生成口径待核实） | text | weibo, xiaohongshu, news, forum, bilibili, tencent_video, iqiyi, zhihu, baidu_zhidao | 仅统计 |
| is_robot | 原始机器账号标记（枚举待核实） | opaque_enum | xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, tencent_video, iqiyi, zhihu, baidu_zhidao | 仅统计 |
| parent_id | 上级内容ID（关联口径待核实） | identifier | baidu_zhidao | 仅统计 |
| primary_site_id | 主站点ID（归属口径待核实） | identifier | news, forum, zhihu, baidu_zhidao | 仅统计 |
| search_keyword | 返回查询词列表 | text_list | weibo, weixin, xiaohongshu, news, forum, douyin, kuaishou, shipinhao, bilibili, tencent_video, iqiyi, zhihu, baidu_zhidao | 仅统计 |
| task_ids | 返回任务ID列表 | identifier_list | news, forum, zhihu, baidu_zhidao | 仅统计 |

## 平台和类型校验

- 平台归属只使用登记site_id与明确data_type，二者冲突归unknown并告警；不猜站点名称、URL或取数顺序。新闻、论坛继续按类别归属，不改变原查询范围。
- validation.json.by_platform保存每个平台的字段缺失、null、空值、零、类型与非空率，分母为本次该平台记录数。无记录非空率为null；not_observed与no_records不等同不适用。缺少平台标识时仍执行通用字段、ID和数量校验。
- count为非负整数（布尔值不算数字）；nonnegative_number接受非负有限整数/小数，单位不靠类型推断。identifier接受非空字符串或整数。opaque_enum保留编码，不猜枚举含义。
- text_or_text_list接受原字符串或字符串列表：实测B站pic_urls为字符串，其他已测平台可为列表；不解析拼接、不改写。json_text_or_object只核对JSON对象/对象文本结构，不摊平扩展字段。
- timestamp接受常见Unix秒/毫秒或带时区ISO8601；不猜无时区字符串，不转换原值。URL只核对结构，不联网验证。
- 数量上限、唯一ID、分页哈希、原始JSON和XLSX逐单元格/异常文本分片校验仍是硬门禁。业务字段类型不合规为告警。

## 指标与模板取舍

- 保留原27列与顺序，默认追加site_id、coin_cnt、barrage_cnt、fav_cnt、favorite_cnt、collection_cnt、collection_video_cnt、view_cnt、video_view_cnt、like_share_cnt、display_cnt、duration_seconds，共39列。新增指标明确保留原字段名或待核实标记，不冒充已确认中文指标。
- coin_cnt在本次多个非B站平台出现，B站30条没有该字段；不能把它限定为B站投币。view_cnt同时见于微信、视频和新闻，不自动映射为reads_count。fav_cnt/favorite_cnt/collection_cnt分别保留，不合并、不求和。字段存在或为零都不能证明业务含义。
- 既有reposts_cnt/review_cnt/like_cnt/interaction_cnt映射仍直接取供应商值，不能凭新字段重算总互动。旧错误别名仅作历史定义，不自动填入默认列。
- 供应商分层采集与更新频率导致互动字段缺失是常态。互动字段的缺键/null/空值只计入覆盖统计，不视作采集异常、不补零；存在但类型无效仍告警。阅读量、播放量、点赞等跨平台可比性未获官方确认。
- 新字段当前只覆盖原始指标、内容结构、媒体地址和追溯标识；完整供应商字典与个人画像等其他属性留开发参考。未登记字段仍保留在records.json，并按平台计入undocumented_fields，不在默认表格擅自扩列。

## 导出、缓存与恢复

XLSX不承载缺键/null的完整区分，须结合records.json与validation.json。特殊字符和超过32767 UTF-16单位的文本按v0.11.1规则写入text_overflow，拼接JSON并校验哈希后可恢复原值，禁止静默截断。

已有完成缓存返回原字典版本/质量报告；这不代表已执行新版字段校验。需要新版默认列或报告时使用新输出路径，仅本地重新导出原有页面。旧派生报告不得被新版覆盖。旧失败导出若绑定旧模板先保持原模板恢复；不能改manifest绕过契约，不创建新的计费任务。

COMPLETED只表示当前本地产物通过对应校验；不证明官方字段语义、全平台非零实测、目标Runtime或用户业务分析验收已完成。完整证据与待核实项留研发环境。

## v0.11.3 证据补充

三平台新样本仅更新字典与说明，默认39列及字段映射保持不变。补入fingerprint、is_robot、parent_id、primary_site_id、search_keyword、task_ids的结构定义；这些字段可按需在自定义模板中选用。非零样本覆盖完成不等于所有内容类型或业务指标语义已认证。
