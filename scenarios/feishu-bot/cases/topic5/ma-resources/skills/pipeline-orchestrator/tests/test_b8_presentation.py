"""B8 呈现层：FIX-51 格式/配色、FIX-52 平台分布并排规则、FIX-53 图表规范、FIX-54 活动词云综合平台。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import charts  # noqa: E402
import feishu  # noqa: E402
import handlers  # noqa: E402
import report_contract as RC  # noqa: E402

RENDER = Path(__file__).resolve().parents[2] / "blueai-yuqing-echarts-skill-new/scripts/render_from_spec.js"


class FormatTest(unittest.TestCase):
    def test_nsr_percent_signed(self):
        self.assertEqual(RC.fmt_pct(0.2537), "+25.4%")

    def test_warn_callout_light_yellow(self):
        block = feishu.md_to_blocks("> [!warn] 演示抽样。")[0]
        self.assertEqual(block["callout"], feishu.CALLOUT_STYLE["warn"])


class DistPairTest(unittest.TestCase):
    def _img(self, name):
        return {"block_type": 27, "_local_path": f"/x/{name}.png", "_alt": name}

    def test_platform_and_content_pair(self):
        blocks = feishu.layout_blocks([self._img("A_platform"), self._img("A_content")])
        self.assertEqual([b["block_type"] for b in blocks], [24])

    def test_other_adjacent_images_not_paired(self):
        blocks = feishu.layout_blocks([self._img("A_trend"), self._img("A_content")])
        self.assertEqual([b["block_type"] for b in blocks], [27, 27])


class ChartSpecTest(unittest.TestCase):
    STATS = {"campaigns": {"A": {
        "sentiment_dist": {"positive": 3, "neutral": 2, "negative": 1},
        "platform_dist": {"xiaohongshu": 3, "douyin": 2},
        "trend": {f"202610{d:02d}": d for d in range(1, 31)}}}}

    def test_palette_and_sizes(self):
        specs = {s["chart_id"]: s for s in charts.build_chart_specs(self.STATS, {})}
        self.assertEqual(specs["A_sentiment"]["colors"], {"正面": "#5DBB7D", "中性": "#AAAAAA", "负面": "#F06060"})
        self.assertEqual((specs["A_platform"]["width"], specs["A_platform"]["height"]), (600, 450))
        self.assertEqual(specs["A_trend"]["x_axis_policy"]["step"], 5)

    def test_long_span_uses_week_or_more(self):
        self.assertEqual(charts.x_axis_policy(60)["step"], 5)
        self.assertGreaterEqual(charts.x_axis_policy(120)["step"], 14)

    @unittest.skipUnless(shutil.which("node") and RENDER.exists(), "需要 node")
    def test_renderer_uses_spec_colors_size_and_axis(self):
        spec = {s["chart_id"]: s for s in charts.build_chart_specs(self.STATS, {})}["A_trend"]
        work = Path(tempfile.mkdtemp())
        (work / "s.json").write_text(json.dumps(spec, ensure_ascii=False))
        subprocess.run(["node", str(RENDER), "--spec", str(work / "s.json"), "--js-out", str(work / "o.js")],
                       check=True, capture_output=True)
        probe = ("const fs=require('fs');const echarts={graphic:{LinearGradient:function(){}}};"
                 f"eval(fs.readFileSync({json.dumps(str(work / 'o.js'))},'utf8')+';globalThis.o=option;globalThis.w=W;');"
                 "const o=globalThis.o;const iv=o.xAxis.axisLabel.interval;"
                 "console.log(JSON.stringify([globalThis.w,o.series[0].lineStyle.color,"
                 "o.xAxis.data.filter((_,i)=>iv(i)).length,iv(0),iv(o.xAxis.data.length-1)]));")
        out = json.loads(subprocess.run(["node", "-e", probe], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out, [1400, "#4A7AE3", 7, True, True])
        shutil.rmtree(work, ignore_errors=True)


class WordcloudMergeTest(unittest.TestCase):
    def test_sum_by_term_across_platforms(self):
        merged, method = handlers._merge_cloud_words({"xiaohongshu": {"联名": 10, "周边": 3},
                                                      "douyin": {"联名": 5}})
        self.assertEqual(merged, {"联名": 15, "周边": 3})
        self.assertEqual(method, "sum_by_term_across_platforms")

    def test_ssjh_total_preferred(self):
        merged, method = handlers._merge_cloud_words({"_total": {"联名": 99}, "douyin": {"联名": 5}})
        self.assertEqual((merged, method), ({"联名": 99}, "ssjh_total"))

    def test_one_cloud_per_campaign_dimension(self):
        wc = {"campaigns": {"A": {d: {"platforms": {"all": {"联名": 5, "周边": 2}}}
                                  for d in ("overall", "brand", "product")}}}
        specs = [s for s in charts.build_chart_specs({}, wc, {"platforms": ["xiaohongshu", "douyin"]})
                 if s["chart_type"] == "wordcloud"]
        self.assertEqual(len(specs), 3)
        self.assertTrue(all(s["chart_id"].endswith("_all") for s in specs))
        self.assertIn("综合已选平台（小红书、抖音）", specs[0]["wordcloud_input"]["context"]["platform"])

    def test_heading_by_object(self):
        self.assertEqual(RC._wordcloud_heading("2.1.4", "overall", "all"), "活动整体核心议题")
        self.assertEqual(RC._wordcloud_heading("1.3", None, "xiaohongshu"), "小红书核心议题解读")


if __name__ == "__main__":
    unittest.main()
