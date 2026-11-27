"""轮次一：情感(AI) + 是否活动相关(AI) 直接 API 标注（行数 < 1000 路径）。
用法：python annot_r1.py <03_数据接收及处理目录> <活动名> <背景说明>
输出：annotated_data.xlsx、annot_r1_checkpoint.json（断点续跑）
"""
import sys, json, re, os
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import anthropic

d, campaign, ctx = sys.argv[1], sys.argv[2], sys.argv[3]
MODEL = "claude-haiku-4-5-20251001"
BATCH = 15
ck_path = os.path.join(d, "annot_r1_checkpoint.json")
df = pd.read_excel(os.path.join(d, "cleaned_data.xlsx"))
ck = json.load(open(ck_path, encoding="utf-8")) if os.path.exists(ck_path) else {}
client = anthropic.Anthropic()

SYS = f"""你是专业的社媒文本标注员。对每条内容独立标注两个字段。

## 活动背景
本次复盘活动：「{campaign}」。{ctx}

## 1. sentiment（情感倾向）
- 正面：明确表达积极评价、推荐、喜爱、支持
- 中性：纯信息传递、无明显情感、内容展示/介绍
- 负面：抱怨、批评、质疑、失望、负面情绪
正负并存时以主导情感为准。

## 2. campaign_relevant（是否活动相关）
- 是：内容明确提及、参与、响应或评价本次活动
- 否：品牌一般内容、竞品讨论、行业泛讨论等与本次活动无关

只输出 JSON 数组，每个元素 {{"i":序号,"sentiment":"正面|中性|负面","campaign_relevant":"是|否"}}，不附加解释。"""


def text(r):
    t = str(r["标题"] if pd.notna(r["标题"]) else "")[:200]
    c = str(r["内容"] if pd.notna(r["内容"]) else "")[:800]
    return (t + "\n" + c).strip()


def run_batch(ids):
    body = "\n\n".join(f"[{k}] 平台:{df.loc[i,'平台标签']}\n{text(df.loc[i])}" for k, i in enumerate(ids))
    for _ in range(3):
        try:
            m = client.messages.create(model=MODEL, max_tokens=2000, system=SYS,
                                       messages=[{"role": "user", "content": body}])
            raw = m.content[0].text
            arr = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
            out = {}
            for a in arr:
                if a["sentiment"] in ("正面", "中性", "负面") and a["campaign_relevant"] in ("是", "否"):
                    out[str(ids[int(a["i"])])] = [a["sentiment"], a["campaign_relevant"]]
            return out
        except Exception as e:
            err = repr(e)[:150]
    print("batch failed", ids[:2], err, file=sys.stderr)
    return {}


todo = [i for i in df.index if str(i) not in ck and str(df.loc[i, "内容"] if pd.notna(df.loc[i, "内容"]) else "").strip()]
batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
with ThreadPoolExecutor(6) as ex:
    for res in ex.map(run_batch, batches):
        ck.update(res)
json.dump(ck, open(ck_path, "w", encoding="utf-8"), ensure_ascii=False)

df["情感(AI)"] = [ck.get(str(i), ["标注跳过_无内容" if not str(df.loc[i, "内容"] if pd.notna(df.loc[i, "内容"]) else "").strip() else "标注失败"] * 2)[0] for i in df.index]
df["是否活动相关(AI)"] = [ck.get(str(i), ["", "标注跳过_无内容" if not str(df.loc[i, "内容"] if pd.notna(df.loc[i, "内容"]) else "").strip() else "标注失败"])[1] for i in df.index]
df.to_excel(os.path.join(d, "annotated_data.xlsx"), index=False)
print(campaign, len(df), df["情感(AI)"].value_counts().to_dict(), df["是否活动相关(AI)"].value_counts().to_dict())
