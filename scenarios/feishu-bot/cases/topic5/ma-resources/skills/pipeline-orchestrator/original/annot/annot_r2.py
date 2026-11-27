"""轮次二：三个独立任务（直接 API）
 ① 正面观点 ② 负面观点 ③ 属性标注（ugc_category + discussion_dimension，覆盖全部活动相关行含中性）
用法：python annot_r2.py <03_数据接收及处理目录> <活动名> <内容类型范围:全部|仅UGC|仅PGC>
输入：annotated_data.xlsx + opinion_candidates_*.json + ugc_category_list.json（均为用户确认后的版本）
输出：回写 annotated_data.xlsx（观点类别(AI) / ugc_category / discussion_dimension）
"""
import sys, json, re, os
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import anthropic

d, campaign, scope = sys.argv[1], sys.argv[2], sys.argv[3]
MODEL, BATCH = "claude-haiku-4-5-20251001", 15
df = pd.read_excel(os.path.join(d, "annotated_data.xlsx"))
client = anthropic.Anthropic()
S = lambda v: str(v) if pd.notna(v) else ""


def load(n):
    p = os.path.join(d, n)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"status": "not_applicable", "categories": []}


def item(i):
    r = df.loc[i]
    t = (S(r["标题"])[:200] + "\n" + S(r["内容"])[:800]).strip()
    extra = (S(r.get("视频封面内容识别", ""))[:200] + " " + S(r.get("音频内容识别", ""))[:200]).strip()
    return t + (f"\n[封面/音频识别] {extra}" if extra else "")


def call(system, ids, key, valid):
    body = "\n\n".join(f"[{k}] 平台:{df.loc[i,'平台标签']}\n{item(i)}" for k, i in enumerate(ids))
    err = ""
    for _ in range(3):
        try:
            m = client.messages.create(model=MODEL, max_tokens=2500, system=system,
                                       messages=[{"role": "user", "content": body}])
            arr = json.loads(re.search(r"\[.*\]", m.content[0].text, re.S).group(0))
            out = {}
            for a in arr:
                v = a if isinstance(a, dict) else {}
                res = {kk: str(v.get(kk, "")).strip() for kk in key}
                if all(valid[kk](res[kk]) for kk in key):
                    out[ids[int(v["i"])]] = res
            return out
        except Exception as e:
            err = repr(e)[:150]
    print("batch failed", ids[:2], err, file=sys.stderr)
    return {}


def run(system, ids, key, valid):
    batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    res = {}
    with ThreadPoolExecutor(6) as ex:
        for o in ex.map(lambda b: call(system, b, key, valid), batches):
            res.update(o)
    return res


def cat_table(cats):
    return "\n".join(f"- {c['name']}：{c['definition']}（例：{'；'.join(e[:40] for e in c.get('examples', [])[:2])}）" for c in cats)


rel = df["是否活动相关(AI)"].astype(str).str.strip() == "是"
sent = df["情感(AI)"].astype(str).str.strip()
df["观点类别(AI)"] = ["N/A" if not rel[i] else "无" for i in df.index]
df["ugc_category"] = "N/A"
df["discussion_dimension"] = "N/A"

# ①② 观点
for senti, f in (("正面", "opinion_candidates_positive.json"), ("负面", "opinion_candidates_negative.json")):
    j = load(f)
    ids = list(df.index[rel & (sent == senti)])
    if j.get("status") != "ok" or not ids:
        print(senti, "观点不适用，填无"); continue
    names = [c["name"] for c in j["categories"]]
    sysmsg = f"""你是专业文本标注员。以下是「{campaign}」活动的{senti}内容。候选观点标签是【讨论话题】，不含情感；该内容的情感已确定为{senti}，只需判断它在讨论哪个话题。
## 候选标签
{cat_table(j['categories'])}
## 规则
从候选标签中选 1 个最匹配的；仅当内容兼有两个不同话题时选 2 个（用英文逗号分隔）。优先篇幅最重的部分；都不匹配输出“其他”。
只输出 JSON 数组：[{{"i":序号,"观点":"标签1,标签2"}}]，不附加解释。"""
    ok = lambda v, names=names: bool(v) and all(x.strip() in names + ["其他"] for x in v.replace("，", ",").split(","))
    res = run(sysmsg, ids, ["观点"], {"观点": ok})
    for i, o in res.items():
        df.at[i, "观点类别(AI)"] = o["观点"].replace("，", ",")
    print(senti, "观点标注", len(res), "/", len(ids))

# ③ 属性标注（全部活动相关行，含中性）
ug = load("ugc_category_list.json")
ids = list(df.index[rel])
ugc_ok = ug.get("status") == "ok" and scope != "仅PGC"
unames = [c["name"] for c in ug.get("categories", [])] + ["其他"]
ugc_part = (f"""## ugc_category（UGC 参与方式）
分类依据是用户做了什么动作、采用什么内容形式，不依据情感/观点倾向——同样的动作和形式，无论夸还是骂，归同一类。选 1 个；都不符合填“其他”。
{cat_table(ug.get('categories', []))}""" if ugc_ok else "## ugc_category\n本项目不适用，一律填 N/A。")
sysmsg = f"""你是专业文本标注员。对「{campaign}」活动的每条内容标注两个字段（与情感无关，中性内容同样标注）。
{ugc_part}

## discussion_dimension（讨论维度，选 1 个主导维度）
- 品牌：主要讨论品牌形象、价值观、合作背景、联名意义
- 产品：主要讨论具体产品功能、使用体验、外观设计、口味、周边实物
- 活动：主要讨论活动机制、参与流程、抢购、打卡、规则、奖励
多维度并存取篇幅最重者。
只输出 JSON 数组：[{{"i":序号,"ugc_category":"…","discussion_dimension":"品牌|产品|活动"}}]，不附加解释。"""
valid = {"ugc_category": (lambda v: v in unames + ["N/A"]) if ugc_ok else (lambda v: True),
         "discussion_dimension": lambda v: v in ("品牌", "产品", "活动")}
res = run(sysmsg, ids, ["ugc_category", "discussion_dimension"], valid)
for i, o in res.items():
    df.at[i, "ugc_category"] = o["ugc_category"] if ugc_ok else "N/A"
    df.at[i, "discussion_dimension"] = o["discussion_dimension"]
print("属性标注", len(res), "/", len(ids))
df.to_excel(os.path.join(d, "annotated_data.xlsx"), index=False)
