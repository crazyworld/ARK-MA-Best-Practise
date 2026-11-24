"""Sanitized, machine-readable failures. Never serialize arbitrary exception text."""
import json
import sqlite3


class SkillError(ValueError):
    def __init__(self, code, category='validation', *, operation=None, retryable=False,
                 http_status=None, provider_code=None, retry_after_seconds=None,
                 outcome_uncertain=False, next_action=None):
        super().__init__(code)
        if code == 'SOHU_MARKET_CONTRACT_REQUIRED':
            next_action = '搜狐视频需单独接入数据超市契约。交上游核对账号权限、创建返回/状态链路与内部计价；不得用搜狐新闻或appId替代视频站点。'
        if category=='validation' and 'LOCK' in code:
            category='lock'
            next_action=next_action or '核实锁所属进程已结束后再人工处理；不要按文件年龄自动清锁。'
        if 'OUTCOME_UNKNOWN' in code or 'DO_NOT_REPLAY' in code:
            outcome_uncertain=True
            next_action=next_action or '保留原请求ID和登记库，先核对供应商结果，再 recover/reconcile；禁止重建。'
        self.detail = dict(code=code, category=category, operation=operation,
            retryable=retryable, http_status=http_status, provider_code=provider_code,
            retry_after_seconds=retry_after_seconds, outcome_uncertain=outcome_uncertain,
            next_action=next_action or '核对条件和本地记录，修复后继续原请求。')


def describe(exc, operation=None):
    if isinstance(exc, SkillError):
        return dict(exc.detail)
    category='internal'; code='UNEXPECTED_LOCAL_ERROR'; action='保留运行目录，检查本地实现与状态；不要重建任务。'
    if isinstance(exc, ImportError):
        category='dependency'; code='DEPENDENCY_MISSING'; action='运行 doctor，按 requirements.txt 在目标环境补齐依赖。'
    elif isinstance(exc, json.JSONDecodeError):
        category='state'; code='JSON_FILE_INVALID'; action='保留损坏文件，核对有效备份和登记记录；不要重建任务。'
    elif isinstance(exc, (OSError, sqlite3.Error)):
        category='filesystem'; code='LOCAL_IO_ERROR'; action='检查文件路径、权限、磁盘空间与占用，保留运行目录后恢复。'
    elif isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        category='validation'; code='CONFIG_OR_STATE_INVALID'; action='运行 doctor，核对配置、状态结构和恢复记录。'
    return SkillError(code,category,operation=operation,next_action=action).detail
