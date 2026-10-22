#!/usr/bin/env bash
# topic6 本地测试脚本
# 用法:
#   ./run_tests.sh              # 全量 topic6 测试（含回归）
#   ./run_tests.sh fix          # 只跑本次修复的回归测试（39 条，0.1s）
#   ./run_tests.sh quick        # 同 fix
#   ./run_tests.sh full         # 全量 + 详细输出
#   ./run_tests.sh <pytest参数>  # 自定义，如 ./run_tests.sh -k "url" -v
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
TEST_DIR="$REPO_ROOT/scenarios/feishu-bot"
CONDA_ENV="topic6"

# ---- 定位 python ----
# 优先用 conda env 的 python，找不到就用 PATH 上的
PYTHON=""
for candidate in \
    "$HOME/anaconda3/envs/$CONDA_ENV/bin/python" \
    "$HOME/miniconda3/envs/$CONDA_ENV/bin/python" \
    "$(conda run -n "$CONDA_ENV" which python 2>/dev/null || true)"; do
    if [ -x "$candidate" ]; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    # fallback: 尝试 conda activate（交互 shell）
    eval "$(conda shell.bash hook 2>/dev/null)" || true
    conda activate "$CONDA_ENV" 2>/dev/null && PYTHON="$(which python)" || true
fi
if [ -z "$PYTHON" ]; then
    echo "❌ 找不到 conda env '$CONDA_ENV' 的 python" >&2
    echo "   请先创建: conda create -n $CONDA_ENV python=3.11 pytest pandas httpx" >&2
    exit 1
fi

echo "🐍 Python: $PYTHON ($($PYTHON --version 2>&1))"
cd "$TEST_DIR"

# ---- 解析参数 ----
MODE="${1:-all}"
shift 2>/dev/null || true

case "$MODE" in
    fix|quick)
        echo "🔧 运行本次修复回归测试（test_topic6_fix_regression.py）"
        "$PYTHON" -m pytest tests/test_topic6_fix_regression.py -v --tb=short "$@"
        ;;
    full)
        echo "🧪 运行全量 topic6 测试（详细输出）"
        "$PYTHON" -m pytest tests/test_topic6_*.py -v --tb=long "$@"
        ;;
    all|"")
        echo "🧪 运行全量 topic6 测试"
        "$PYTHON" -m pytest tests/test_topic6_*.py --tb=short -q "$@"
        ;;
    -*)
        echo "🧪 自定义 pytest 参数: $MODE $*"
        "$PYTHON" -m pytest tests/test_topic6_*.py "$MODE" "$@"
        ;;
    *)
        echo "用法: $0 [fix|quick|full|all|<pytest参数>]"
        echo "  fix/quick  只跑本次修复的回归测试（0.1s）"
        echo "  all        全量 topic6 测试（默认，~2s）"
        echo "  full       全量 + 详细输出"
        echo "  -k xxx     自定义 pytest 参数"
        exit 1
        ;;
esac
