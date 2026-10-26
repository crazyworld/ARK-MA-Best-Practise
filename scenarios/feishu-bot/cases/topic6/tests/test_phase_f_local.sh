#!/usr/bin/env bash
# Phase F + HC3 本地端到端验证脚本
# 在线上机器（212）执行，模拟沙箱中 lark-cli 和 publish_report_to_feishu.py 的完整调用链路
#
# 用法：
#   source ~/.arkagent/cases/topic6/config.env
#   bash tests/test_phase_f_local.sh
#
# 测试内容：
#   1. 飞书凭证获取（tenant_access_token）
#   2. 创建文件夹（create_folder）
#   3. 上传 xlsx 并转为在线表格（import_tasks）
#   4. lark-cli bot 身份测试（模拟 LARKSUITE_CLI_* 环境变量）
#   5. 清理测试文件
set -euo pipefail

PYTHON="${PYTHON:-python3}"
TOPIC6_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FEISHU_BOT_DIR="$(cd "$TOPIC6_DIR/../.." && pwd)"

# fallback 变量名
export LARK_APP_ID="${LARK_APP_ID:-${FEISHU_APP_ID:-}}"
export LARK_APP_SECRET="${LARK_APP_SECRET:-${FEISHU_APP_SECRET:-}}"
FOLDER_TOKEN="${TOPIC6_DOCS_FOLDER_TOKEN:-CHz0fnO5hlJAVddjiWBchhvlnSg}"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; exit 1; }

echo "===== Phase F + HC3 本地验证 ====="
echo "LARK_APP_ID: ${LARK_APP_ID:0:8}..."
echo "FOLDER_TOKEN: $FOLDER_TOKEN"
echo ""

# 1. tenant_access_token
echo "--- 1/5 获取 tenant_access_token ---"
TOKEN=$($PYTHON -c "
import os, requests
app_id = os.environ.get('LARK_APP_ID') or os.environ.get('FEISHU_APP_ID', '')
app_secret = os.environ.get('LARK_APP_SECRET') or os.environ.get('FEISHU_APP_SECRET', '')
resp = requests.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
    json={'app_id': app_id, 'app_secret': app_secret}, timeout=10)
d = resp.json()
t = d.get('tenant_access_token', '')
if not t:
    raise SystemExit(f'FAILED: {d}')
print(t)
")
[ -n "$TOKEN" ] && pass "tenant_access_token OK (len=${#TOKEN})" || fail "tenant_access_token 获取失败"

# 2. 创建文件夹
echo "--- 2/5 创建测试文件夹 ---"
CREATED=$($PYTHON -c "
import requests, json
resp = requests.post('https://open.feishu.cn/open-apis/drive/v1/files/create_folder',
    headers={'Authorization': 'Bearer $TOKEN', 'Content-Type': 'application/json; charset=utf-8'},
    json={'name': '_phase_f_e2e_test', 'folder_token': '$FOLDER_TOKEN'}, timeout=30)
d = resp.json()
if d.get('code') != 0:
    raise SystemExit(f'FAILED: {d}')
t = (d.get('data') or {}).get('token') or (d.get('data') or {}).get('folder_token', '')
print(t)
")
[ -n "$CREATED" ] && pass "create_folder OK token=$CREATED" || fail "create_folder 失败"

# 3. 上传 xlsx + import_tasks 转在线表格
echo "--- 3/5 上传 xlsx 并转为在线表格 ---"
SHEET_TOKEN=$($PYTHON -c "
import requests, json, time, tempfile, os
import openpyxl

# 创建测试 xlsx
wb = openpyxl.Workbook()
ws = wb.active
ws.append(['row_id', 'platform', 'title', 'score'])
ws.append([1, '微博', '测试热搜', 95])
ws.append([2, '抖音', '测试话题', 88])
tf = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
wb.save(tf.name)

headers = {'Authorization': 'Bearer $TOKEN'}

# upload_all
size = os.path.getsize(tf.name)
resp = requests.post('https://open.feishu.cn/open-apis/drive/v1/files/upload_all',
    headers=headers,
    files={
        'file_name': (None, 'phase_f_test.xlsx'),
        'parent_type': (None, 'explorer'),
        'parent_node': (None, '$CREATED'),
        'size': (None, str(size)),
        'file': ('phase_f_test.xlsx', open(tf.name, 'rb'), 'application/octet-stream'),
    }, timeout=60)
os.unlink(tf.name)
d = resp.json()
if d.get('code') != 0:
    raise SystemExit(f'upload FAILED: {d}')
file_token = (d.get('data') or {}).get('file_token', '')

# import_tasks
resp = requests.post('https://open.feishu.cn/open-apis/drive/v1/import_tasks',
    headers={**headers, 'Content-Type': 'application/json; charset=utf-8'},
    json={
        'file_extension': 'xlsx',
        'file_token': file_token,
        'type': 'sheet',
        'file_name': 'phase_f_test_sheet',
        'point': {'mount_type': 1, 'mount_key': '$CREATED'},
    }, timeout=30)
d = resp.json()
if d.get('code') != 0:
    raise SystemExit(f'import FAILED: {d}')
ticket = (d.get('data') or {}).get('ticket', '')

# poll
for i in range(20):
    time.sleep(1)
    resp = requests.get(f'https://open.feishu.cn/open-apis/drive/v1/import_tasks/{ticket}',
        headers=headers, timeout=10)
    r = resp.json()
    status = (r.get('data') or {}).get('result', {}).get('job_status', -1)
    if status == 0:
        st = (r.get('data') or {}).get('result', {}).get('token', '')
        # 清理临时文件
        requests.delete(f'https://open.feishu.cn/open-apis/drive/v1/files/{file_token}?type=file',
            headers=headers, timeout=10)
        print(st)
        break
    elif status != 1:
        raise SystemExit(f'import job failed: {r}')
else:
    raise SystemExit('import timeout')
")
[ -n "$SHEET_TOKEN" ] && pass "import_tasks → 在线表格 OK token=$SHEET_TOKEN" || fail "import_tasks 失败"
echo "  在线表格 URL: https://bluefocus.feishu.cn/sheets/$SHEET_TOKEN"

# 4. 模拟 LARKSUITE_CLI_* 环境变量（沙箱中 lark-cli 依赖这些）
echo "--- 4/5 验证 LARKSUITE_CLI_* 环境变量完整性 ---"
$PYTHON -c "
import json
env_file = '$TOPIC6_DIR/ma-resources/environment.json'
data = json.load(open(env_file))
env = data['config']['env']
required = ['LARK_APP_ID', 'LARK_APP_SECRET', 'LARKSUITE_CLI_APP_ID']
forbidden = ['LARKSUITE_CLI_APP_SECRET', 'LARKSUITE_CLI_STRICT_MODE']  # STRICT_MODE=bot 会拦 Phase H --as user
missing = [k for k in required if k not in env] + [f'禁止:{k}' for k in forbidden if k in env]
if missing:
    raise SystemExit(f'environment.json 缺少: {missing}')
print('all required env vars present')
" && pass "environment.json 环境变量完整" || fail "environment.json 缺少 LARKSUITE_CLI 变量"

# 5. 清理
echo "--- 5/5 清理测试文件 ---"
$PYTHON -c "
import requests
headers = {'Authorization': 'Bearer $TOKEN'}
# 删除在线表格
requests.delete('https://open.feishu.cn/open-apis/drive/v1/files/$SHEET_TOKEN?type=sheet',
    headers=headers, timeout=10)
# 删除文件夹
requests.delete('https://open.feishu.cn/open-apis/drive/v1/files/$CREATED?type=folder',
    headers=headers, timeout=10)
print('cleaned')
" && pass "清理完成" || echo "清理失败（不影响验证结果）"

echo ""
echo "===== 全部通过 ====="
