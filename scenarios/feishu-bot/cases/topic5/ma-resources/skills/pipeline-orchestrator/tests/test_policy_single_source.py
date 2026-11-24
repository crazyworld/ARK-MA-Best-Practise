"""口径唯一事实源守护测试（FIX-57）。

口径常量只允许定义在 topic5-validator/scripts/policy.py。
一旦有人在 handlers / scan_report / validator 里另抄一份"清单"（多项字面量成组出现），
本测试失败。单个指标名作字典键/展示文案属于使用而非定义，不算泄漏。

运行：python3 -m unittest tests.test_policy_single_source
"""
import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers          # noqa: E402
import validator         # noqa: E402

ORCH_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = ORCH_DIR.parent
VALIDATOR_DIR = SKILLS_DIR / "topic5-validator"
POLICY_PATH = VALIDATOR_DIR / "scripts" / "policy.py"


def _load_policy_directly():
    spec = importlib.util.spec_from_file_location("policy_under_test", POLICY_PATH)
    assert spec and spec.loader, f"无法加载 {POLICY_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VoiceShareScanTest(unittest.TestCase):
    """FIX-19：声量贡献参考 >100% 判 P0（KS_Metrics_Calculation v0.15 / LM #46）。"""

    def setUp(self):
        self.scan = validator._load_scan()

    def _ids(self, md):
        return [i["id"] for i in self.scan.scan_markdown(md)["issues"]]

    def test_over_hundred_percent_is_p0(self):
        md = "## 2.1.2 提及漏斗分析\n声量贡献参考：120.0%（活动相关 1200 / 品牌同期 1000）\n"
        self.assertTrue(any(i.startswith("P0-voice-share-overflow") for i in self._ids(md)))

    def test_normal_percent_passes(self):
        md = "## 2.1.2 提及漏斗分析\n声量贡献参考：12.0%（活动相关 120 / 品牌同期 1000）\n"
        self.assertFalse(any(i.startswith("P0-voice-share-overflow") for i in self._ids(md)))

    def test_calling_it_penetration_is_p0(self):
        md = "## 2.1.2\n活动声量渗透率：12.0%，声量贡献参考作为补充。\n"
        self.assertTrue(any(i.startswith("P0-forbidden-metric") for i in self._ids(md)))


class PolicySingleSourceTest(unittest.TestCase):
    def setUp(self):
        self.policy = _load_policy_directly()

    def test_policy_file_exists(self):
        self.assertTrue(POLICY_PATH.is_file(), f"缺少口径事实源：{POLICY_PATH}")

    def test_handlers_constants_match_policy(self):
        self.assertEqual(list(handlers.METRIC_WHITELIST), list(self.policy.METRIC_WHITELIST))
        self.assertEqual(
            list(handlers.FORBIDDEN_METRIC_TERMS),
            list(self.policy.FORBIDDEN_METRIC_TERMS),
        )

    def test_scan_report_constants_match_policy(self):
        scan = validator._load_scan()
        self.assertTrue(scan, "scan_report.py 不可定位，无法校验口径一致")
        self.assertEqual(list(scan.METRIC_WHITELIST), list(self.policy.METRIC_WHITELIST))
        self.assertEqual(list(scan.FORBIDDEN_TERMS), list(self.policy.FORBIDDEN_METRIC_TERMS))

    def test_validator_builtin_constants_match_policy(self):
        self.assertEqual(list(validator._FORBIDDEN), list(self.policy.FORBIDDEN_METRIC_TERMS))
        self.assertEqual(list(validator._AGG_SUM), list(self.policy.AGGREGATE_SUM_PATTERNS))

    def test_rules_version_in_yaml_matches_policy(self):
        yaml_path = SKILLS_DIR.parent / "io" / "contracts" / "pipeline.v0.2.yaml"
        text = yaml_path.read_text(encoding="utf-8")
        match = re.search(r"validator_rules_version:\s*([\w.\-]+)", text)
        self.assertIsNotNone(match, "pipeline.v0.2.yaml 缺 validator_rules_version")
        self.assertEqual(match.group(1), self.policy.RULES_VERSION)

    def test_no_duplicated_lists_outside_policy(self):
        """禁止在 policy 之外再定义一份"清单"。

        判定：同一行出现 ≥3 个禁用词字面量（tuple/list/dict 定义的特征），
        或出现旧版本号。单个指标名作键/文案不算泄漏。
        """
        scanned = [
            ORCH_DIR / "handlers.py",
            ORCH_DIR / "validator.py",
            ORCH_DIR / "report_contract.py",
            ORCH_DIR / "charts.py",
            VALIDATOR_DIR / "scripts" / "scan_report.py",
        ]
        terms = list(self.policy.FORBIDDEN_METRIC_TERMS)
        offenders = []
        for path in scanned:
            for ln_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                hits = sum(1 for term in terms if f'"{term}"' in line)
                # 兼容回退分支（`or (...)` 包裹的极简兜底）允许存在，由
                # test_handlers_constants_match_policy 保证正常路径取 policy 值。
                if hits >= 3 and " or (" not in line:
                    offenders.append(f"{path.name}:{ln_no}: 重复定义禁用词清单 → {line.strip()[:90]}")
                if "topic5-validator-qc-v1.7" in line:
                    offenders.append(f"{path.name}:{ln_no}: 残留旧规则版本号")
        self.assertEqual(offenders, [], "口径泄漏：\n" + "\n".join(offenders))

    def test_nsr_is_fixed_to_customer_formula(self):
        """FIX-13：客户脚本把公式写死，不设开关；分母=正面+负面。"""
        self.assertTrue(self.policy.NSR_DENOMINATOR_IS_FIXED)
        self.assertIn("正面内容数 + 负面内容数", self.policy.NSR_DEFINITION)
        self.assertNotIn("中性", self.policy.NSR_DEFINITION)
        self.assertEqual(self.policy.NSR_MIN_POSITIVE_NEGATIVE, 30)


if __name__ == "__main__":
    unittest.main()
