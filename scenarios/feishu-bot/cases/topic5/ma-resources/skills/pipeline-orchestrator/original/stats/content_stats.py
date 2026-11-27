"""blueai-stats-aggregation-skill · content_stats.py
补充 aggregate.py 没有覆盖的 4 项活动层数据，口径与 aggregate.py 一致（只统计『是否活动相关=是』）：
  ① PGC/UGC 结构（2.N.1③）          → stats_content_type_{活动}.xlsx
  ② 正/负面观点 TOP（2.N.3）          → stats_opinion_{活动}.xlsx
  ③ 热门内容 PGC/UGC Top（2.N.5）     → stats_top_pgc_/stats_top_ugc_{活动}.xlsx
     + 候选榜 stats_top_candidates_ / 剔除记录 stats_top_excluded_ / 概况 stats_top_summary_
  ④ 热帖配方（2.N.4④）的输入 = ③ 的榜单
热门内容的『与品牌/活动是否相关』需要人工判断：先不带 --exclude-file 运行得到候选榜，
人工确认要剔除的帖子写入 JSON（[{"id":..., "reason":...}]），再带 --exclude-file 重跑。
"""
import argparse, json, re, sys
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aggregate import load, pick_campaign, save  # noqa: E402

SENT_OK = ('正面', '中性', '负面')
NO_OPINION = ('', '无', 'N/A', 'None', '标注跳过_无内容')
SPLIT = re.compile(r'[、/|;；,，]')


def inter(r):
    try: return int(float(r.get('总互动量') or 0))
    except Exception: return 0


def clean(s, n=100):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return s[:n] + ('…' if len(s) > n else '')


def text_of(r):
    t = str(r.get('标题', '') or '').strip(); c = str(r.get('内容', '') or '').strip()
    return clean((t + ' ' + c).strip() if t and t not in c else (c or t))


def ctype(r):
    v = str(r.get('内容类型', '') or '').strip()
    return v if v in ('PGC', 'UGC') else '未知'


def site(r): return str(r.get('站点名称', '') or r.get('平台标签', '') or '').strip()


def opinion_col(rows):
    for c in ('观点类别(AI)', '观点(AI)'):
        if rows and c in rows[0]: return c
    return None


def content_type_stats(rows, out, c, scope):
    if scope != '全部':
        print(f"ℹ️ content_scope={scope}：跳过 PGC/UGC 结构（见 KS_Report_Template「内容范围条件呈现」）", file=sys.stderr); return
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        k = ctype(r); agg[k][0] += 1; agg[k][1] += inter(r)
    total = len(rows)
    data = [[k, v[0], f"{v[0]/total*100:.1f}%", v[1], round(v[1]/v[0], 1)] for k, v in sorted(agg.items(), key=lambda x: -x[1][0])]
    save(data, ['内容类型', '帖子数', '占比', '总互动量', '条均互动量（总互动量÷帖子数）'], out, f"stats_content_type_{c}.xlsx")


def opinion_stats(rows, out, c, top_n, min_dir):
    col = opinion_col(rows)
    if not col:
        print("⚠️ 数据中没有『观点类别(AI)』列，跳过观点统计（未完成轮次二观点标注？）", file=sys.stderr); return
    data, notes = [], []
    for sent in ('正面', '负面'):
        pool = [r for r in rows if str(r.get('情感(AI)', '') or '').strip() == sent]
        n = len(pool)
        if n < min_dir:
            data.append([sent, '—', '—', f'{sent}内容仅 {n} 条（<{min_dir}），不做观点提取', '', n, ''])
            continue
        by = defaultdict(list)
        for r in pool:
            raw = str(r.get(col, '') or '').strip()
            if raw in NO_OPINION: continue
            for lab in {x.strip() for x in SPLIT.split(raw) if x.strip() and x.strip() not in NO_OPINION}:
                by[lab].append(r)
        ranked = sorted(((k, v) for k, v in by.items() if k != '其他'), key=lambda x: (-len(x[1]), -sum(inter(r) for r in x[1])))  # 条数并列时按该类帖子总互动量
        for i, (lab, lst) in enumerate(ranked[:top_n], 1):
            seen, quotes = set(), []
            for r in sorted(lst, key=inter, reverse=True):
                t = text_of(r); key = t[:30]
                if not t or key in seen: continue
                seen.add(key); quotes.append(t)
                if len(quotes) == 3: break
            data.append([sent, i, lab, len(lst), f"{len(lst)/n*100:.1f}%", n, ' ‖ '.join(quotes)])
        if '其他' in by:
            data.append([sent, '—', '其他（未单列）', len(by['其他']), f"{len(by['其他'])/n*100:.1f}%", n, ''])
            if len(by['其他']) / n > 0.20:
                print(f"⚠️ {sent}方向『其他』占 {len(by['其他'])/n*100:.0f}% (>20%)：类别可能不完整，报告需提示", file=sys.stderr)
        extra = len(ranked) - top_n
        if extra > 0: notes.append(f"{sent}方向另有 {extra} 个类别未入 TOP{top_n}")
    data.append(['说明', '', '占比=命中该类别的帖子数÷该方向活动相关帖子总数；一条内容可同时计入多个类别，占比之和可能超过 100%；条数并列时按该类帖子总互动量排序；代表性原文为互动量最高的 ≤3 条（用‖分隔）', '', '', '', ''])
    for nt in notes: data.append(['说明', '', nt, '', '', '', ''])
    save(data, ['方向', '排名', '观点类别', '帖子数', '占比', '该方向帖子总数', '代表性原文（≤100字，不含账号名）'], out, f"stats_opinion_{c}.xlsx")


def top_content(rows, out, c, scope, top_n, cand_n, exclude_file):
    excl = {}
    if exclude_file:
        for it in json.loads(Path(exclude_file).read_text(encoding='utf-8')):
            excl[str(it['id'])] = it.get('reason', '')
    kinds = [k for k in ('PGC', 'UGC') if (scope == '全部' or scope == '仅' + k)]
    summary, excluded_rows, cand_rows = [], [], []
    for k in kinds:
        pool = sorted([r for r in rows if ctype(r) == k], key=inter, reverse=True)
        for rk, r in enumerate(pool[:cand_n], 1):
            cand_rows.append([k, rk, str(r.get('id', '')), text_of(r), site(r), inter(r), str(r.get('url', '') or ''),
                              '待判断' if str(r.get('id')) not in excl else '已标记剔除'])
        kept = [r for r in pool if str(r.get('id')) not in excl]
        top = kept[:top_n]
        if k == 'PGC':
            save([[i, clean(r.get('标题'), 60), text_of(r), 'PGC', site(r), str(r.get('作者', '') or ''), str(r.get('url', '') or ''), inter(r)]
                  for i, r in enumerate(top, 1)], ['排名', '标题', '内容', '内容类型', '站点名称', '作者', '链接', '总互动量'], out, f"stats_top_pgc_{c}.xlsx")
        else:
            save([[i, text_of(r), site(r), str(r.get('url', '') or ''), inter(r)] for i, r in enumerate(top, 1)],
                 ['排名', '内容', '站点名称', '链接', '总互动量'], out, f"stats_top_ugc_{c}.xlsx")   # UGC 表不设作者列
        floor = inter(top[-1]) if top else 0
        ex_in = [r for r in pool if str(r.get('id')) in excl and inter(r) >= floor]
        for r in ex_in:
            excluded_rows.append([k, str(r.get('id', '')), text_of(r), inter(r), excl[str(r.get('id'))]])
        summary.append([k, len(pool), len(top), floor, len(ex_in)])
        if len(top) < top_n:
            print(f"⚠️ {k} 榜单不足 {top_n} 条（可用 {len(top)} 条）", file=sys.stderr)
    save(cand_rows, ['榜单', '候选排名', 'id', '内容', '站点名称', '总互动量', '链接', '状态'], out, f"stats_top_candidates_{c}.xlsx")
    if exclude_file:
        save(excluded_rows, ['榜单', 'id', '内容', '总互动量', '剔除原因'], out, f"stats_top_excluded_{c}.xlsx")
    save(summary, ['榜单', '活动相关帖子数', '入选条数', '最低入选互动量', '入选区间内剔除条数'], out, f"stats_top_summary_{c}.xlsx")
    if not exclude_file:
        print(f"ℹ️ 已输出候选榜（前 {cand_n}）。请人工判断哪些内容与品牌/活动无关，写入 JSON 后用 --exclude-file 重跑；"
              f"榜单表下须注明『剔除条数（入选区间内）与判定规则』", file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True); p.add_argument('--output', required=True)
    p.add_argument('--campaign', required=True); p.add_argument('--campaign-filter')
    p.add_argument('--content-scope', default='全部', choices=['全部', '仅UGC', '仅PGC'])
    p.add_argument('--top-n', type=int, default=10); p.add_argument('--candidates', type=int, default=30)
    p.add_argument('--top-opinions', type=int, default=5); p.add_argument('--min-direction', type=int, default=10)
    p.add_argument('--exclude-file')
    a = p.parse_args()
    out = Path(a.output); out.mkdir(parents=True, exist_ok=True)
    rows_all, err = pick_campaign(load(a.input), a.campaign_filter)
    if err: print(f"❌ {err}", file=sys.stderr); sys.exit(2)
    rel = lambda r: str(r.get('是否活动相关(AI)', r.get('是否活动相关', '')) or '').strip()
    rows = [r for r in rows_all if rel(r) == '是']
    if not rows: print("❌ 活动相关内容为 0 条", file=sys.stderr); sys.exit(2)
    print(f"活动相关 {len(rows)} 条（口径与 aggregate.py 一致）", file=sys.stderr)
    content_type_stats(rows, out, a.campaign, a.content_scope)
    opinion_stats(rows, out, a.campaign, a.top_opinions, a.min_direction)
    top_content(rows, out, a.campaign, a.content_scope, a.top_n, a.candidates, a.exclude_file)
    print(f"✅ 已输出到 {a.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
