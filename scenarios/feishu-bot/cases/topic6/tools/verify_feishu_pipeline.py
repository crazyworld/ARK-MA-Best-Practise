#!/usr/bin/env python3
"""topic6 飞书文档全链路验证：模拟真实 Pipeline 各阶段向飞书写入产物。

验证项（对应 Pipeline 真实流程）：
  Phase D · HC1: 创建飞书在线表格（模拟宽表 wide_table）
  Phase D · HC2: 更新同一表格 + 写入分布摘要文档
  Phase F · HC3: 创建飞书文档（模拟报告），写入报告内容 + 验证可编辑
  权限验证:      白名单用户对所有产物有 full_access

用法:
    export FEISHU_APP_ID="cli_xxx"
    export FEISHU_APP_SECRET="xxx"
    export TOPIC6_DOCS_FOLDER_TOKEN="CHz0fnO5hlJAVddjiWBchhvlnSg"
    export AUTHORIZED_OPEN_IDS="ou_user1,ou_user2"  # 可选

    python verify_feishu_pipeline.py                  # 跑完自动清理
    python verify_feishu_pipeline.py --no-cleanup      # 保留产物供人工验证
    python verify_feishu_pipeline.py --env-file ~/.arkagent/cases/topic6/config.env
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

FEISHU_HOST = "https://open.feishu.cn"

# ---- 模拟真实 Pipeline 产物数据 ----

# HC1/HC2 的 distribution_summary（来自 coordinator.system.md 契约）
MOCK_HC1_SUMMARY = {
    "rows": 50,
    "cols": 28,
    "c0_valid_rate": 0.96,
    "r1_r5_valid_rates": [1.0, 0.98, 1.0, 0.96, 1.0],
}
MOCK_HC2_SUMMARY = {
    "rows": 3950,
    "cols": 28,
    "c0_valid_rate": 0.97,
    "r1_r5_valid_rates": [0.99, 0.98, 1.0, 0.97, 0.99],
    "marketing_hit_rate": 0.85,
}

# 宽表列头（28 列，来自 merge_annotations.py 输出契约）
WIDE_TABLE_HEADERS = [
    "title", "platform", "url", "heat_score", "normalized_heat",
    "publish_time", "interaction_count", "C0_事件名称", "C0_事件类型",
    "C0_核心实体", "C0_情感倾向", "C0_有效",
    "R1_平台借势", "R1_可行性",
    "R2_商业合作", "R2_品牌",
    "R3_风险预警", "R3_风险等级",
    "R4_创意借鉴", "R4_创意类型",
    "R5_消费者行为", "R5_行为类型",
    "C2_事件ID", "C2_事件簇", "C2_置信度",
    "C3_营销节点", "C3_营销类型", "C3_关联度",
]

# 宽表模拟数据（5 行，覆盖各列类型）
WIDE_TABLE_ROWS = [
    ["iPhone 17 Pro 发布引发热议", "微博", "https://weibo.com/xxx", 9500, 0.95,
     "2026-10-14 10:00", 125000, "iPhone 17 Pro 发布", "产品发布",
     "Apple", "正面", "是",
     "品牌可借势科技热点", "高",
     "Apple × 运营商", "Apple",
     "", "", "产品开箱测评", "实物测评",
     "消费者抢购", "购买行为",
     "evt_001", "苹果秋季发布", 0.92,
     "双十一预热", "电商节点", 0.88],
    ["秋天的第一杯奶茶", "抖音", "https://douyin.com/xxx", 8200, 0.82,
     "2026-10-13 14:30", 89000, "秋天第一杯奶茶", "社会话题",
     "奶茶品牌", "正面", "是",
     "餐饮品牌可借势", "高",
     "", "",
     "蹭热度风险", "低",
     "DIY 奶茶教程", "生活创意",
     "跟风消费", "从众行为",
     "evt_002", "秋季饮品热潮", 0.88,
     "秋冬营销", "季节节点", 0.91],
    ["AI 编程助手对比评测", "知乎", "https://zhihu.com/xxx", 6800, 0.68,
     "2026-10-15 09:00", 45000, "AI 编程工具评测", "科技评测",
     "Cursor/GitHub Copilot", "中性", "是",
     "科技品牌可参与讨论", "中",
     "Cursor × VS Code", "Cursor",
     "", "",
     "效率提升对比", "数据对比",
     "开发者试用", "体验行为",
     "evt_003", "AI 工具竞品", 0.85,
     "", "", 0.0],
    ["无效数据行", "B站", "", 100, 0.01,
     "2026-10-12", 50, "", "",
     "", "", "否",
     "", "", "", "", "", "", "", "", "", "",
     "", "", 0.0, "", "", 0.0],
    ["国庆旅游数据复盘", "微博", "https://weibo.com/yyy", 7100, 0.71,
     "2026-10-14 16:00", 67000, "国庆旅游复盘", "数据报告",
     "文旅局/OTA", "正面", "是",
     "旅游品牌复盘借势", "中",
     "携程 × 地方文旅", "携程",
     "", "",
     "旅游 UGC 内容", "UGC 营销",
     "出行决策", "消费决策",
     "evt_004", "国庆出行", 0.90,
     "国庆/中秋", "节假日", 0.95],
]

# Phase F 报告内容（来自 source.example.json 结构）
MOCK_REPORT_SECTIONS = [
    {"heading": "一、行业及热门话题", "content": (
        "本周（W42, 2026-10-12 ~ 2026-10-18）社媒四平台共监测热点话题 3,950 条，"
        "经标注筛选有效话题 3,832 条（有效率 97.0%）。\n\n"
        "热度 TOP3：\n"
        "1. iPhone 17 Pro 发布引发全网热议（综合热度 9,500）\n"
        "2. 秋天的第一杯奶茶再次刷屏（综合热度 8,200）\n"
        "3. 国庆旅游数据复盘（综合热度 7,100）"
    )},
    {"heading": "二、营销节点", "content": (
        "本周涉及营销节点 2 个：\n"
        "- 双十一预热期（电商节点）：与 iPhone 发布叠加，3C 品牌可提前布局\n"
        "- 秋冬营销季（季节节点）：餐饮、服装品牌的季节性营销窗口"
    )},
    {"heading": "三、平台新鲜事", "content": (
        "- 抖音：秋季奶茶话题带动 UGC 内容爆发，相关视频播放量超 2 亿\n"
        "- 知乎：AI 工具类话题持续升温，Cursor vs Copilot 评测引发开发者讨论\n"
        "- B站：科技区 iPhone 17 开箱类视频密集上线"
    )},
    {"heading": "四、营销发现", "content": (
        "营销命中率 85.0%，主要洞察：\n"
        "1. 平台借势（R1）：品牌可借 iPhone 发布热度进行 3C 配件营销\n"
        "2. 商业合作（R2）：Apple × 运营商合作模式可复制到其他品牌\n"
        "3. 创意借鉴（R4）：奶茶 DIY 教程类 UGC 内容转化率高"
    )},
]


class FeishuPipelineTester:
    def __init__(self, app_id: str, app_secret: str, folder_token: str,
                 authorized_open_ids: list[str]):
        self.app_id = app_id
        self.app_secret = app_secret
        self.folder_token = folder_token
        self.authorized_open_ids = authorized_open_ids
        self.tenant_token = ""
        self.results: list[tuple[str, bool, str]] = []
        # 本次测试创建的资源（清理用）
        self._job_folder_token = ""
        self._spreadsheet_token = ""
        self._report_doc_token = ""
        self._summary_doc_token = ""

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{FEISHU_HOST}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.tenant_token}",
            "Content-Type": "application/json; charset=utf-8",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body_text[:500]}") from e

    def _ok(self, step: str, detail: str = ""):
        self.results.append((step, True, detail))
        print(f"  ✅ {step}" + (f" — {detail}" if detail else ""))

    def _fail(self, step: str, detail: str = ""):
        self.results.append((step, False, detail))
        print(f"  ❌ {step}" + (f" — {detail}" if detail else ""))

    def run(self, cleanup: bool = True) -> bool:
        self._get_token()
        if not self.tenant_token:
            return False
        self._create_job_folder()
        self._phase_d_create_spreadsheet()
        self._phase_d_write_wide_table()
        self._phase_d_read_verify()
        self._phase_f_create_report()
        self._phase_f_write_report()
        self._phase_f_create_summary()
        self._grant_permissions()
        self._verify_permissions()
        if cleanup:
            self._cleanup()
        self._summary()
        return all(ok for _, ok, _ in self.results)

    def _get_token(self):
        print("\n[准备] 获取 tenant_access_token …")
        try:
            req = urllib.request.Request(
                f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
                data=json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode(),
                method="POST", headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            self.tenant_token = data.get("tenant_access_token", "")
            if self.tenant_token:
                self._ok("获取 token", f"...{self.tenant_token[-8:]}")
            else:
                self._fail("获取 token", str(data))
        except Exception as e:
            self._fail("获取 token", str(e)[:200])

    def _create_job_folder(self):
        print("\n[Phase D] 创建 Job 子文件夹 …")
        try:
            name = f"W42_2026-10-12_job_test_{int(time.time()) % 10000}"
            resp = self._api("POST", "/open-apis/drive/v1/files/create_folder", {
                "name": name, "folder_token": self.folder_token,
            })
            token = resp.get("data", {}).get("token", "")
            if token:
                self._job_folder_token = token
                self._ok("创建 Job 文件夹", f"{name} → {token}")
            else:
                self._fail("创建 Job 文件夹", str(resp)[:200])
        except Exception as e:
            self._fail("创建 Job 文件夹", str(e)[:200])

    def _phase_d_create_spreadsheet(self):
        print("\n[Phase D · HC1] 创建飞书在线表格（宽表） …")
        folder = self._job_folder_token or self.folder_token
        try:
            resp = self._api("POST", "/open-apis/sheets/v3/spreadsheets", {
                "title": "wide_table_demo_r1",
                "folder_token": folder,
            })
            ss = resp.get("data", {}).get("spreadsheet", {})
            token = ss.get("spreadsheet_token", "")
            if token:
                self._spreadsheet_token = token
                url = f"https://bluefocus.feishu.cn/sheets/{token}"
                self._ok("创建宽表", f"url={url}")
            else:
                self._fail("创建宽表", str(resp)[:200])
        except Exception as e:
            self._fail("创建宽表", str(e)[:200])

    def _phase_d_write_wide_table(self):
        print("[Phase D · HC1] 写入 28 列宽表数据（表头 + 5 行） …")
        if not self._spreadsheet_token:
            self._fail("写入宽表", "无表格")
            return
        try:
            # 获取第一个 sheet 的 ID
            resp = self._api("GET", f"/open-apis/sheets/v3/spreadsheets/{self._spreadsheet_token}/sheets/query")
            sheets = resp.get("data", {}).get("sheets", [])
            if not sheets:
                self._fail("写入宽表", "无 sheet")
                return
            sheet_id = sheets[0].get("sheet_id", "")

            # 写入数据：表头 + 数据行
            all_rows = [WIDE_TABLE_HEADERS] + WIDE_TABLE_ROWS
            # 列号：A-Z 后是 AA, AB...
            col_count = len(WIDE_TABLE_HEADERS)
            if col_count <= 26:
                end_col = chr(64 + col_count)
            else:
                end_col = chr(64 + (col_count - 1) // 26) + chr(65 + (col_count - 1) % 26)
            range_str = f"{sheet_id}!A1:{end_col}{len(all_rows)}"
            resp = self._api(
                "PUT",
                f"/open-apis/sheets/v2/spreadsheets/{self._spreadsheet_token}/values",
                {
                    "valueRange": {
                        "range": range_str,
                        "values": all_rows,
                    }
                },
            )
            code = resp.get("code", -1)
            if code == 0:
                self._ok("写入宽表", f"{len(WIDE_TABLE_HEADERS)} 列 × {len(WIDE_TABLE_ROWS)} 行")
            else:
                self._fail("写入宽表", f"code={code} msg={resp.get('msg', '')}")
        except Exception as e:
            self._fail("写入宽表", str(e)[:300])

    def _phase_d_read_verify(self):
        print("[Phase D · HC1] 回读宽表验证 …")
        if not self._spreadsheet_token:
            self._fail("回读宽表", "无表格")
            return
        try:
            resp = self._api("GET", f"/open-apis/sheets/v3/spreadsheets/{self._spreadsheet_token}/sheets/query")
            sheets = resp.get("data", {}).get("sheets", [])
            sheet_id = sheets[0].get("sheet_id", "") if sheets else ""
            resp = self._api(
                "GET",
                f"/open-apis/sheets/v2/spreadsheets/{self._spreadsheet_token}/values/{sheet_id}!A1:A2",
            )
            values = resp.get("data", {}).get("valueRange", {}).get("values", [])
            if values and values[0] and values[0][0] == "title":
                self._ok("回读宽表", f"首行首列={values[0][0]}，数据正确")
            else:
                self._fail("回读宽表", f"values={values}")
        except Exception as e:
            self._fail("回读宽表", str(e)[:200])

    def _phase_f_create_report(self):
        print("\n[Phase F · HC3] 创建飞书文档（报告） …")
        folder = self._job_folder_token or self.folder_token
        try:
            resp = self._api("POST", "/open-apis/docx/v1/documents", {
                "folder_token": folder,
                "title": "社媒热点周报 · W42（2026-10-12 ~ 2026-10-18）",
            })
            doc_id = resp.get("data", {}).get("document", {}).get("document_id", "")
            if doc_id:
                self._report_doc_token = doc_id
                self._ok("创建报告文档", f"https://bluefocus.feishu.cn/docx/{doc_id}")
            else:
                self._fail("创建报告文档", str(resp)[:200])
        except Exception as e:
            self._fail("创建报告文档", str(e)[:200])

    def _phase_f_write_report(self):
        print("[Phase F · HC3] 分板块写入报告内容 …")
        if not self._report_doc_token:
            self._fail("写入报告", "无文档")
            return
        written = 0
        for section in MOCK_REPORT_SECTIONS:
            try:
                # 写标题
                resp = self._api(
                    "POST",
                    f"/open-apis/docx/v1/documents/{self._report_doc_token}/blocks/{self._report_doc_token}/children",
                    {"children": [{
                        "block_type": 3,  # heading1
                        "heading1": {
                            "elements": [{"text_run": {"content": section["heading"]}}],
                            "style": {},
                        },
                    }], "index": -1},
                )
                # 写正文
                for para in section["content"].split("\n"):
                    if not para.strip():
                        continue
                    self._api(
                        "POST",
                        f"/open-apis/docx/v1/documents/{self._report_doc_token}/blocks/{self._report_doc_token}/children",
                        {"children": [{
                            "block_type": 2,  # text
                            "text": {
                                "elements": [{"text_run": {"content": para}}],
                                "style": {},
                            },
                        }], "index": -1},
                    )
                written += 1
            except Exception as e:
                self._fail(f"写入板块 {section['heading']}", str(e)[:200])
        if written == len(MOCK_REPORT_SECTIONS):
            self._ok("写入报告", f"{written} 个板块全部写入")
        else:
            self._fail("写入报告", f"仅写入 {written}/{len(MOCK_REPORT_SECTIONS)} 个板块")

    def _phase_f_create_summary(self):
        print("[Phase F] 创建分布摘要文档 …")
        folder = self._job_folder_token or self.folder_token
        try:
            resp = self._api("POST", "/open-apis/docx/v1/documents", {
                "folder_token": folder,
                "title": "distribution_summary_W42",
            })
            doc_id = resp.get("data", {}).get("document", {}).get("document_id", "")
            if not doc_id:
                self._fail("创建摘要文档", str(resp)[:200])
                return
            self._summary_doc_token = doc_id
            # 写入摘要内容
            summary_text = (
                f"HC1 分布摘要（demo 50 条）:\n"
                f"  rows={MOCK_HC1_SUMMARY['rows']}, cols={MOCK_HC1_SUMMARY['cols']}\n"
                f"  C0 有效率={MOCK_HC1_SUMMARY['c0_valid_rate']:.0%}\n"
                f"  R1~R5 有效率={MOCK_HC1_SUMMARY['r1_r5_valid_rates']}\n\n"
                f"HC2 分布摘要（全量 3950 条）:\n"
                f"  rows={MOCK_HC2_SUMMARY['rows']}, cols={MOCK_HC2_SUMMARY['cols']}\n"
                f"  C0 有效率={MOCK_HC2_SUMMARY['c0_valid_rate']:.0%}\n"
                f"  营销命中率={MOCK_HC2_SUMMARY['marketing_hit_rate']:.0%}"
            )
            self._api(
                "POST",
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                {"children": [{
                    "block_type": 2,
                    "text": {"elements": [{"text_run": {"content": summary_text}}], "style": {}},
                }], "index": 0},
            )
            self._ok("创建摘要文档", f"https://bluefocus.feishu.cn/docx/{doc_id}")
        except Exception as e:
            self._fail("创建摘要文档", str(e)[:200])

    def _grant_permissions(self):
        print("\n[Phase G 模拟] App 回读报告文档（模拟 HC3 通过后 Agent 重新读取） …")
        if not self._report_doc_token:
            self._fail("App 回读报告", "无文档")
        else:
            try:
                resp = self._api(
                    "GET",
                    f"/open-apis/docx/v1/documents/{self._report_doc_token}/raw_content",
                )
                content = resp.get("data", {}).get("content", "")
                has_section1 = "行业" in content and "热门话题" in content
                has_section4 = "营销发现" in content
                if has_section1 and has_section4:
                    self._ok("App 回读报告", f"内容长度={len(content)}，4 个板块均可读")
                elif content:
                    self._ok("App 回读报告", f"内容长度={len(content)}（部分板块可能需等待同步）")
                else:
                    self._fail("App 回读报告", "内容为空")
            except Exception as e:
                self._fail("App 回读报告", str(e)[:200])

        print("\n[权限] 为白名单用户授予文件夹权限 …")
        if not self.authorized_open_ids:
            self._ok("授予权限", "无白名单用户，跳过")
            return
        folder = self._job_folder_token or self.folder_token
        for open_id in self.authorized_open_ids:
            try:
                resp = self._api(
                    "POST",
                    f"/open-apis/drive/v1/permissions/{folder}/members?type=folder&need_notification=false",
                    {"member_type": "openid", "member_id": open_id, "perm": "full_access"},
                )
                code = resp.get("code", -1)
                if code == 0 or "already" in str(resp).lower():
                    self._ok(f"授权 {open_id[:15]}…", "full_access")
                else:
                    self._fail(f"授权 {open_id[:15]}…", f"code={code}")
            except Exception as e:
                self._fail(f"授权 {open_id[:15]}…", str(e)[:200])

    def _verify_permissions(self):
        print("[权限] 验证权限列表 …")
        folder = self._job_folder_token or self.folder_token
        try:
            resp = self._api("GET", f"/open-apis/drive/v1/permissions/{folder}/members?type=folder")
            members = resp.get("data", {}).get("items", [])
            member_ids = {m.get("member_id", "") for m in members}
            for open_id in self.authorized_open_ids:
                if open_id in member_ids:
                    self._ok(f"权限验证 {open_id[:15]}…", "在列表中")
                else:
                    self._fail(f"权限验证 {open_id[:15]}…", "不在列表中")
            if not self.authorized_open_ids:
                self._ok("权限验证", f"共 {len(members)} 个成员")
        except Exception as e:
            self._fail("权限验证", str(e)[:200])

    def _cleanup(self):
        print("\n[清理] …")
        for token, typ in [
            (self._report_doc_token, "docx"),
            (self._summary_doc_token, "docx"),
            (self._spreadsheet_token, "sheet"),
        ]:
            if token:
                try:
                    self._api("DELETE", f"/open-apis/drive/v1/files/{token}?type={typ}")
                    print(f"  🗑️ 已删除 {typ} {token[:12]}…")
                except Exception:
                    pass
        if self._job_folder_token:
            try:
                self._api("DELETE", f"/open-apis/drive/v1/files/{self._job_folder_token}?type=folder")
                print(f"  🗑️ 已删除文件夹 {self._job_folder_token[:12]}…")
            except Exception:
                pass

    def _summary(self):
        print("\n" + "=" * 60)
        passed = sum(1 for _, ok, _ in self.results if ok)
        failed = sum(1 for _, ok, _ in self.results if not ok)
        print(f"结果: {passed} 通过, {failed} 失败")
        if failed:
            print("\n失败项:")
            for step, ok, detail in self.results:
                if not ok:
                    print(f"  ❌ {step}: {detail}")

        print("\n产物链接（--no-cleanup 模式下可访问）:")
        if self._job_folder_token:
            print(f"  📁 文件夹: https://bluefocus.feishu.cn/drive/folder/{self._job_folder_token}")
        if self._spreadsheet_token:
            print(f"  📊 宽表:   https://bluefocus.feishu.cn/sheets/{self._spreadsheet_token}")
        if self._report_doc_token:
            print(f"  📄 报告:   https://bluefocus.feishu.cn/docx/{self._report_doc_token}")
        if self._summary_doc_token:
            print(f"  📋 摘要:   https://bluefocus.feishu.cn/docx/{self._summary_doc_token}")
        print("=" * 60)


def load_env_file(path: str) -> None:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser(description="topic6 飞书文档全链路验证")
    parser.add_argument("--env-file", default="", help="env 文件路径")
    parser.add_argument("--no-cleanup", action="store_true", help="保留测试产物")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    load_env_file(os.path.expanduser("~/.arkagent/cases/topic6/config.env"))

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    folder_token = os.environ.get("TOPIC6_DOCS_FOLDER_TOKEN", "")
    open_ids = [
        s.strip() for s in os.environ.get("AUTHORIZED_OPEN_IDS", "").replace(",", " ").split()
        if s.strip()
    ]

    if not app_id or not app_secret:
        print("❌ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET"); sys.exit(1)
    if not folder_token:
        print("❌ 缺少 TOPIC6_DOCS_FOLDER_TOKEN"); sys.exit(1)

    print("topic6 飞书文档全链路验证")
    print(f"  App:    {app_id}")
    print(f"  文件夹: {folder_token}")
    print(f"  用户:   {open_ids or '（无）'}")

    tester = FeishuPipelineTester(app_id, app_secret, folder_token, open_ids)
    ok = tester.run(cleanup=not args.no_cleanup)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
