"""blueai-stats-aggregation-skill · check_filterwords.py

Step 4 品牌取数之前运行：核对活动 Excel 是否已按需求确认单的『活动取数过滤词』清洗过。
逐个过滤词在标题、内容、话题三列中搜索（不区分大小写，按字面包含），统计命中行数。

结论：
  全部 0 命中 -> Excel 已按这些词清洗，品牌过滤词应并入同一套词（退出码 0）
  有命中      -> 不动数据，把命中的词和条数告诉用户，由用户决定保留还是排除（退出码 1）
参数/数据错误 -> 退出码 2

用法：
  python check_filterwords.py --input 原始数据/活动数据.xlsx --filterwords "词A|词B|词C" [--output filterword_check.json]
"""
import argparse, json, sys
import openpyxl

COLUMNS = ('标题', '内容', '话题')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--filterwords', required=True, help='活动过滤词，用 | 连接')
    p.add_argument('--output', default='')
    a = p.parse_args()

    words = [w.strip() for w in a.filterwords.split('|') if w.strip()]
    if not words:
        print('❌ 过滤词为空，无需核对', file=sys.stderr)
        sys.exit(2)

    try:
        wb = openpyxl.load_workbook(a.input, read_only=True)
    except Exception as e:
        print(f'❌ 无法读取 {a.input}：{e}', file=sys.stderr)
        sys.exit(2)
    ws = wb.active
    hdrs = [str(c.value or '').strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = [hdrs.index(c) for c in COLUMNS if c in hdrs]
    if not idx:
        print(f'❌ 找不到 {COLUMNS} 中的任何列，实际列名：{hdrs}', file=sys.stderr)
        sys.exit(2)
    missing = [c for c in COLUMNS if c not in hdrs]
    if missing:
        print(f'⚠️ 缺少列 {missing}，仅在其余列中核对', file=sys.stderr)

    texts = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        texts.append(' '.join(str(r[i]) for i in idx if i < len(r) and r[i] is not None).lower())
    wb.close()

    total = len(texts)
    per_word = {w: sum(1 for t in texts if w.lower() in t) for w in words}
    hit_rows = sum(1 for t in texts if any(w.lower() in t for w in words))
    hit_words = {w: n for w, n in per_word.items() if n}

    result = {
        'total_rows': total,
        'filterwords': words,
        'hit_rows': hit_rows,
        'hit_words': hit_words,
        'conclusion': 'cleaned' if hit_rows == 0 else 'not_cleaned',
    }
    if a.output:
        with open(a.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    if hit_rows == 0:
        print(f'✅ {len(words)} 个过滤词在 {total} 行中均命中 0 条：Excel 已按这些词清洗。'
              f'品牌过滤词应并入同一套词。', file=sys.stderr)
        sys.exit(0)
    print(f'⚠️ {hit_rows}/{total} 行命中过滤词，Excel 未完全清洗：', file=sys.stderr)
    for w, n in sorted(hit_words.items(), key=lambda x: -x[1]):
        print(f'   {w}: {n} 条', file=sys.stderr)
    print('请先询问用户这些内容保留还是排除，再继续品牌取数。', file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()
