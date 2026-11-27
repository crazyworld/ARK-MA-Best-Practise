"""blueai-stats-aggregation-skill · aggregate.py"""
import argparse, sys
from pathlib import Path
from collections import defaultdict
import openpyxl
from openpyxl import Workbook

DATE_COLS = ('发表时间', '发布时间', '时间')  # 字段规范为『发表时间』，其余为兼容旧数据

def load(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    hdrs = [str(c.value or '').strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = [dict(zip(hdrs, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]
    wb.close()
    print(f"已加载 {len(rows)} 行", file=sys.stderr)
    return rows

def pick_campaign(rows, cf):
    """按『分析对象』筛选活动。多活动数据必须显式指定 --campaign-filter，不允许混池统计。
    返回 (rows, error_message)。"""
    names = sorted({str(r.get('分析对象', '') or '').strip() for r in rows} - {''})
    if not cf:
        if len(names) > 1:
            return None, (f"数据含多个活动（{names}），请用 --campaign-filter 指定其一，"
                          f"并为每个活动分别运行；不允许混池统计")
        return rows, None
    if cf not in names:
        return None, f"『分析对象』中没有 {cf!r}（现有：{names or '无该列/全空'}），请检查 --campaign-filter 是否写错"
    kept = [r for r in rows if str(r.get('分析对象', '') or '').strip() in ('', cf)]
    blank = sum(1 for r in kept if not str(r.get('分析对象', '') or '').strip())
    if blank:
        print(f"⚠️ {blank} 行『分析对象』为空，已并入 {cf!r} 统计；请确认它们确属该活动", file=sys.stderr)
    return kept, None

def nsr(pos, neg): return f"{(pos-neg)/(pos+neg)*100:+.1f}%" if (pos+neg) else "N/A"

def save(rows, hdrs, out, name):
    wb = Workbook(); ws = wb.active
    ws.append(hdrs)
    for r in rows: ws.append(r)
    wb.save(out / name)
    print(f"  ✅ {name} ({len(rows)} 行)", file=sys.stderr)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--campaign', required=True)
    p.add_argument('--campaign-filter')
    p.add_argument('--brand-total', type=int, default=0)
    p.add_argument('--brand-name', help='主品牌名（仅匹配主品牌名，不用别名集合），用于漏斗 Tier2 文本匹配')
    p.add_argument('--product-keywords', nargs='+')
    p.add_argument('--sentiment-missing-max', type=float, default=0.05, help='活动相关内容中『情感(AI)』缺失/非法比例超过该值时报错退出（默认 5%%）')
    p.add_argument('--exclude-warn', type=float, default=0.10, help='无关内容剔除比例超过该值时告警（默认 10%%）')
    args = p.parse_args()
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    loaded = load(args.input)
    all_rows, err = pick_campaign(loaded, args.campaign_filter)
    if err:
        print(f"❌ {err}", file=sys.stderr); sys.exit(2)
    c = args.campaign

    # 统一口径：全部指标只统计「是否活动相关=是」的内容（用户确认，见 KS_Data_Processing v1.12）
    def rel_of(r): return str(r.get('是否活动相关(AI)', r.get('是否活动相关', '')) or '').strip()
    unlabeled = [r for r in all_rows if rel_of(r) not in ('是', '否')]
    if unlabeled:
        print(f"⚠️ {len(unlabeled)}/{len(all_rows)} 行『是否活动相关』缺失/非法（轮次一未标注？），已排除出统计；"
              f"请先完成轮次一标注", file=sys.stderr)
    rows = [r for r in all_rows if rel_of(r) == '是']
    total = len(rows)
    if not total:
        print("❌ 活动相关内容为 0 条，无法统计（检查『是否活动相关(AI)』列是否已标注）", file=sys.stderr)
        sys.exit(2)
    # 活动层情感只认『情感(AI)』（轮次一 AI 标注），不回退到监测平台的原始『情感』列
    SENT_OK = ('正面', '中性', '负面')
    bad_sent = [r for r in rows if str(r.get('情感(AI)', '') or '').strip() not in SENT_OK]
    if bad_sent:
        ratio = len(bad_sent) / total
        msg = f"活动相关内容中 {len(bad_sent)}/{total} 条（{ratio*100:.1f}%）『情感(AI)』缺失或不是 正面/中性/负面"
        if '情感(AI)' not in rows[0]:
            msg = "数据中没有『情感(AI)』列（活动层情感必须来自轮次一 AI 标注，不使用监测平台原始『情感』）"
            ratio = 1.0
        if ratio > args.sentiment_missing_max:
            print(f"❌ {msg}，超过允许的 {args.sentiment_missing_max*100:.0f}%；请先完成/补跑轮次一情感标注", file=sys.stderr); sys.exit(2)
        print(f"⚠️ {msg}，这部分不计入情感分布与 NSR", file=sys.stderr)
    n_excl = len([r for r in all_rows if rel_of(r) == '否'])
    if all_rows and n_excl / len(all_rows) > args.exclude_warn:
        print(f"⚠️ 无关内容剔除比例 {n_excl/len(all_rows)*100:.1f}%（{n_excl}/{len(all_rows)}）超过 {args.exclude_warn*100:.0f}%："
              f"活动相关由 AI 判定，判定偏严会影响全部指标。请先抽查被判『否』的内容（建议随机 30 条）确认无误后再出报告",
              file=sys.stderr)
    save([['监测检索口径（原始声量）', len(all_rows)], ['活动相关（本报告统计口径）', total],
          ['剔除的无关内容', len([r for r in all_rows if rel_of(r) == '否'])], ['未标注（已排除）', len(unlabeled)]],
         ['范围', '条数'], out, f"stats_scope_{c}.xlsx")

    # 趋势
    by_d = defaultdict(lambda: [0, 0])
    for r in rows:
        d = '未知'
        for col in DATE_COLS:
            if r.get(col):
                d = str(r[col])[:10]; break
        by_d[d][0] += 1; by_d[d][1] += int(r.get('总互动量') or 0)
    n_unknown = by_d['未知'][0] if '未知' in by_d else 0
    if n_unknown == total:
        print(f"❌ 所有行都取不到日期（已尝试列：{'、'.join(DATE_COLS)}），声量趋势无法生成；请检查数据列名",
              file=sys.stderr); sys.exit(2)
    if n_unknown:
        print(f"⚠️ {n_unknown}/{total} 行缺日期，已归入『未知』，声量趋势不含这部分", file=sys.stderr)
    save([[d, v[0], v[1]] for d, v in sorted(by_d.items())],
         ['日期', '声量', '互动量'], out, f"stats_volume_trend_{c}.xlsx")

    # 平台
    plat = defaultdict(lambda: [0, 0])
    for r in rows:
        pl = str(r.get('平台标签', r.get('平台', '未知')) or '未知').strip()
        plat[pl][0] += 1; plat[pl][1] += int(r.get('总互动量') or 0)
    save([[k, v[0], f"{v[0]/total*100:.1f}%", v[1]] for k, v in sorted(plat.items(), key=lambda x: -x[1][0])],
         ['平台', '声量', '占比', '互动量'], out, f"stats_platform_{c}.xlsx")

    # 情感 + NSR（口径：活动相关内容；占比分母=活动相关声量）
    sc = defaultdict(int)
    for r in rows: sc[str(r.get('情感(AI)', '') or '').strip()] += 1
    pos, neg, neu = sc.get('正面', 0), sc.get('负面', 0), sc.get('中性', 0)
    t = pos + neg + neu
    save([['正面', pos, f"{pos/t*100:.1f}%" if t else 'N/A'],
          ['中性', neu, f"{neu/t*100:.1f}%" if t else 'N/A'],
          ['负面', neg, f"{neg/t*100:.1f}%" if t else 'N/A'],
          ['NSR（不含中性）', nsr(pos, neg), '']],
         ['情感', '数量', '占比/值'], out, f"stats_sentiment_{c}.xlsx")

    # KOL层级（头/腰/尾，排除素人/未知；范围=活动相关的 PGC）；条均互动量=总互动量÷帖子数（分母是帖子数，非人数）
    tc, ti = defaultdict(int), defaultdict(int)
    for r in rows:
        if str(r.get('内容类型','') or '').strip() != 'PGC': continue
        kol = str(r.get('kol_level','') or '').strip()
        if kol in ('素人', '未知', ''): continue
        tc[kol] += 1; ti[kol] += int(r.get('总互动量') or 0)
    tk = sum(tc.values())
    save([[tier, tc.get(tier,0), f"{tc.get(tier,0)/tk*100:.1f}%" if tk else 'N/A',
           f"{ti.get(tier,0)/tc.get(tier,1):.0f}" if tc.get(tier) else 'N/A',
           ti.get(tier,0)] for tier in ['头部','腰部','尾部']],
         ['层级','帖子数','声量占比','条均互动量','总互动量'], out, f"stats_kol_tier_{c}.xlsx")

    # UGC参与方式
    elig = [r for r in rows if str(r.get('是否活动相关(AI)','') or '').strip()=='是'
            and str(r.get('内容类型','') or '').strip()=='UGC']
    et = len(elig); cat = defaultdict(int)
    for r in elig: cat[str(r.get('ugc_category','') or '').strip() or 'N/A'] += 1
    na_ugc = [r for r in elig if str(r.get('ugc_category','') or '').strip() in ('', 'N/A')]
    if na_ugc:
        by = defaultdict(int)
        for r in na_ugc: by[str(r.get('情感(AI)','') or '').strip() or '?'] += 1
        print(f"⚠️ {len(na_ugc)}/{et} 条活动相关UGC的 ugc_category 为空/N/A（按情感：{dict(by)}），"
              f"结果中会出现 N/A 类别——请先运行 check_coverage.py 补标，勿直接用于报告", file=sys.stderr)
    save([[k, v, f"{v/et*100:.1f}%" if et else 'N/A'] for k, v in sorted(cat.items(), key=lambda x: -x[1])],
         ['参与方式','数量','占比'], out, f"stats_ugc_category_{c}.xlsx")

    # 分维度NSR
    rel = [r for r in rows if str(r.get('是否活动相关(AI)','') or '').strip()=='是']
    na_dim = [r for r in rel if str(r.get('discussion_dimension','') or '').strip() not in ('品牌','产品','活动')]
    if na_dim:
        print(f"⚠️ {len(na_dim)}/{len(rel)} 条活动相关内容的 discussion_dimension 缺失/非法，"
              f"分维度覆盖声量将偏低——请先运行 check_coverage.py 补标", file=sys.stderr)
    dim_rows = []
    for dim in ['品牌','产品','活动']:
        dr = [r for r in rel if str(r.get('discussion_dimension','') or '').strip()==dim]
        dp = sum(1 for r in dr if str(r.get('情感(AI)','') or '').strip()=='正面')
        dn = sum(1 for r in dr if str(r.get('情感(AI)','') or '').strip()=='负面')
        note = '样本不足' if (dp+dn) < 30 else ''   # 门槛按『正面+负面』合计，与 NSR 分母一致（KS_Metrics_Calculation v0.12）
        dim_rows.append([dim, len(dr), dp, dn, nsr(dp, dn) if not note else note])
    save(dim_rows, ['讨论维度','覆盖声量','正面','负面','NSR'], out, f"stats_dimension_nsr_{c}.xlsx")

    # 提及漏斗（Tier1 活动声量 → Tier2 提及品牌 → Tier3 提及产品；均为帖子数，同一数据集内递进子集）
    # 指标名：品牌提及率=Tier2/Tier1；产品提及占比=Tier3/Tier2（另列 Tier3/Tier1）；
    # 声量贡献参考=活动声量/品牌同期总声量（仅量级参考，非渗透率）
    def text_of(r): return str(r.get('标题','') or '') + str(r.get('内容','') or '')
    def pct(a, b): return f"{a/b*100:.1f}%" if b else 'N/A'
    t1 = total
    fd = [['活动总声量（Tier 1）', t1, '100%', '100%']]
    if args.brand_name:
        tier2 = [r for r in rows if args.brand_name.lower() in text_of(r).lower()]  # 英文品牌名大小写不敏感（manner/Manner/MANNER）
        t2 = len(tier2)
        degenerate = bool(t1) and t2 / t1 >= 0.95
        fd.append(['提及品牌（Tier 2）' + ('【退化：与 Tier 1 基本重叠】' if degenerate else ''), t2, pct(t2, t1), pct(t2, t1)])
        if degenerate:
            print(f"⚠️ Tier2/Tier1 = {t2/t1*100:.1f}% ≥ 95%：活动检索词已包含品牌名，该层无独立分析意义。"
                  f"报告 2.N.2 须按 KS 退化为两层（活动声量 → 提及产品），不输出独立的品牌提及率", file=sys.stderr)
        if args.product_keywords:
            t3 = sum(1 for r in tier2 if any(k.lower() in text_of(r).lower() for k in args.product_keywords))
            fd.append(['提及产品（Tier 3，嵌套于 Tier 2）', t3, pct(t3, t2), pct(t3, t1)])
    else:
        print("⚠️ 未提供 --brand-name：无法按‘文本包含品牌名’计算 Tier2/Tier3，仅输出 Tier1。"
              "（请传入主品牌名，不要用 是否活动相关 代替）", file=sys.stderr)
    if args.brand_total and args.brand_total < t1:
        print(f"⚠️ 品牌同期总声量 {args.brand_total} < 活动相关声量 {t1}：声量贡献参考将 > 100%，"
              f"多半是品牌关键词未覆盖活动关键词。请回查需求确认单『关键词预检』，核实前该指标写为『口径异常』，不得写入报告", file=sys.stderr)
    if args.brand_total:
        contrib = '口径异常（品牌总量<活动声量）' if args.brand_total < t1 else f"{t1/args.brand_total*100:.2f}%"
        fd += [['---','---','---','---'],
               ['品牌同期总声量（参考）', args.brand_total, '', ''],
               ['声量贡献参考（活动声量/品牌同期总声量）', t1, contrib, '']]
    save(fd, ['层级','声量','占比（对上一层）','占活动声量比'], out, f"stats_penetration_{c}.xlsx")

    print(f"\n✅ 全部统计文件已输出到 {args.output}", file=sys.stderr)

if __name__ == '__main__':
    main()
