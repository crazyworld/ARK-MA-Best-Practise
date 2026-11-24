"""Public action preconditions. Internal stage updates remain owned by workflow."""
from errors import SkillError

ACTIVE = {'CREATE_OUTCOME_UNKNOWN', 'SUPPLIER_RUNNING', 'SUPPLIER_COMPLETED',
          'READY_FOR_DOWNLOAD', 'VERIFYING_DATA_READY', 'WAIT_TIMEOUT_RESUMABLE',
          'DOWNLOADING', 'DOWNLOAD_FAILED_RESUMABLE', 'VALIDATING',
          'EXPORTING', 'EXPORT_FAILED_RESUMABLE'}
STATES = ACTIVE | {'NOT_STARTED', 'PREFLIGHT_RUNNING', 'PRECHECK_RUNNING', 'PRECHECK_FAILED',
    'AWAITING_QUANTITY_CONFIRMATION', 'APPROVED_NOT_SUBMITTED', 'BUDGET_BLOCKED',
    'COMPLETED', 'DATA_VALIDATION_FAILED', 'SUPPLIER_FAILED', 'TASK_COUNT_EXCEEDS_LIMIT','QUANTITY_REVIEW_REQUIRED'}
ALLOWED = {
    'precheck': {'NOT_STARTED', 'PRECHECK_FAILED', 'AWAITING_QUANTITY_CONFIRMATION',
                 'APPROVED_NOT_SUBMITTED', 'BUDGET_BLOCKED'},
    'approve': {'AWAITING_QUANTITY_CONFIRMATION', 'APPROVED_NOT_SUBMITTED'},
    'create': {'APPROVED_NOT_SUBMITTED'},
    'download': ACTIVE - {'CREATE_OUTCOME_UNKNOWN'},
    'resume': ACTIVE - {'CREATE_OUTCOME_UNKNOWN'},
    'export': {'COMPLETED'},
    'review-quantity': {'QUANTITY_REVIEW_REQUIRED','COMPLETED','READY_FOR_DOWNLOAD','VERIFYING_DATA_READY','SUPPLIER_COMPLETED'},
}

def guard(action, state):
    stage = state.get('state', 'NOT_STARTED')
    if stage not in STATES or action in ALLOWED and stage not in ALLOWED[action]:
        raise SkillError('ACTION_NOT_ALLOWED_IN_CURRENT_STATE', 'state', operation=action,
                         next_action='保留原任务，核对当前状态及允许动作；不要重新创建。')
