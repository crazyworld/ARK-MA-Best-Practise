"""Fixed request ceiling; bundled or admin rates never enter public outputs.

This enforces a conservative pre-call estimate, not a supplier-side billing cap.
The host must protect ledger/overrides; bundled rates are not an OS secrecy boundary.
"""
import contextlib
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from errors import SkillError
from request_registry import valid_id

LIMIT_MICRO = 1000 * 1_000_000
MESSAGE = '您的取数任务涉及数据费用成本过高，超出数据费费控阈值1000元限制，请联系智蓝数据部负责人袁杰松。感谢理解和配合！~'


def fail(code):
    error = SkillError(code, 'budget', operation='budget', retryable=False,
        next_action=MESSAGE if code == 'BUDGET_LIMIT_EXCEEDED' else
        '停止收费调用，交由上游Agent核对内部计价配置或费用记录；不得绕过门禁或重新创建未知任务。')
    error.detail.update(upstream_action='STOP_AND_ESCALATE', route_to='upstream_agent')
    if code == 'BUDGET_LIMIT_EXCEEDED':
        error.detail['message'] = MESSAGE
    raise error


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def amount(value):
    # Decimal strings avoid binary rounding; no silent rounding down at the boundary.
    try:
        if not isinstance(value, str) or len(value) > 40:
            fail('BUDGET_PRICING_INVALID')
        number = Decimal(value)
        if not number.is_finite() or number <= 0 or number > Decimal('1000000000'):
            fail('BUDGET_PRICING_INVALID')
        micro = number * 1_000_000
        if micro != micro.to_integral_value():
            fail('BUDGET_PRICING_INVALID')
        return int(micro)
    except (InvalidOperation, ValueError, OverflowError):
        fail('BUDGET_PRICING_INVALID')


def private_settings(run_path):
    """Return private settings without returning values/paths to public diagnostics."""
    try:
        bundled = 'SSJH_PRICING_FILE' not in os.environ
        default_directory = 'SSJH_BUDGET_DIR' not in os.environ
        price = os.environ.get('SSJH_PRICING_FILE', str(Path(__file__).parent / 'internal' / 'pricing.json'))
        directory = os.environ.get('SSJH_BUDGET_DIR', str(Path.home() / '.ssjh_private' / 'budget'))
        if not price or not directory:
            fail('BUDGET_CONFIGURATION_REQUIRED')
        price, directory = Path(price).expanduser().resolve(), Path(directory).expanduser().resolve()
        public_roots = [Path(__file__).resolve().parent, Path(run_path).resolve()]
        if not price.is_file() or (directory.exists() and not directory.is_dir()) or (not directory.exists() and not default_directory):
            fail('BUDGET_CONFIGURATION_REQUIRED')
        if any((not bundled and price.is_relative_to(p)) or directory.is_relative_to(p) for p in public_roots):
            fail('BUDGET_PRIVATE_STORAGE_REQUIRED')
        raw = price.read_bytes()
        if bundled:
            from pricing_integrity import BUNDLED_SHA256
            if hashlib.sha256(raw).hexdigest() != BUNDLED_SHA256:
                fail('BUDGET_PRICING_INTEGRITY_FAILED')
        if len(raw) > 65536:
            fail('BUDGET_PRICING_INVALID')
        data = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data, dict) or data.get('currency') != 'CNY' or data.get('schema_version') != 1:
            fail('BUDGET_PRICING_INVALID')
        rates = data.get('record_rates')
        if not isinstance(rates, dict) or not rates:
            fail('BUDGET_PRICING_INVALID')
        return directory / 'budget.sqlite3', hashlib.sha256(raw).hexdigest(), amount(data.get('stat_success')), {k: amount(v) for k, v in rates.items()}
    except SkillError:
        raise
    except Exception:
        fail('BUDGET_CONFIGURATION_INVALID')


class Budget:
    def __init__(self, run, request_id, account_scope, contract_hash):
        valid_id(request_id)
        self.key = digest([account_scope, request_id])
        self.contract_hash = contract_hash
        self.path, self.price_hash, self.stat_rate, self.rates = private_settings(run.path)
        if run.state.get('budget_key') and (run.state['budget_key'] != self.key or not self.path.exists()):
            fail('BUDGET_LEDGER_MISSING_OR_CHANGED')
        # Never put money, rates, or the internal file paths into Run.state/events.
        self.run = run

    @contextlib.contextmanager
    def connect(self):
        # Only real budget operations initialize the default ledger. doctor stays read-only.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('CREATE TABLE IF NOT EXISTS charges (id TEXT PRIMARY KEY, task TEXT NOT NULL, operation TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0), status TEXT NOT NULL)')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_creation ON charges(task) WHERE operation="create"')
            conn.execute('CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, task TEXT NOT NULL, price_hash TEXT NOT NULL, evidence_hash TEXT NOT NULL)')
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def check(self, conn, extra):
        used = conn.execute('SELECT COALESCE(SUM(amount),0) FROM charges WHERE task=? AND status IN ("reserved","incurred")', (self.key,)).fetchone()[0]
        if used + extra > LIMIT_MICRO:
            fail('BUDGET_LIMIT_EXCEEDED')

    def check_platforms(self, platforms):
        if any(p not in self.rates for p in platforms):
            fail('BUDGET_PLATFORM_PRICE_REQUIRED')

    def begin_precheck(self, requests):
        self.check_platforms([r['platform'] for r in requests])
        with self.connect() as conn:
            if conn.execute('SELECT 1 FROM charges WHERE task=? AND operation="create"', (self.key,)).fetchone():
                fail('BUDGET_CREATE_OUTCOME_RECONCILE_ONLY')
            self.check(conn, len(requests) * self.stat_rate)
        self.run.update(_progress=False, budget_key=self.key)

    def reserve_stat(self):
        ticket = uuid.uuid4().hex
        with self.connect() as conn:
            self.check(conn, self.stat_rate)
            conn.execute('INSERT INTO charges VALUES (?,?,?,?,?)', (ticket, self.key, 'stat', self.stat_rate, 'reserved'))
        return ticket

    def settle(self, ticket, status):
        with self.connect() as conn:
            changed = conn.execute('UPDATE charges SET status=? WHERE id=? AND task=? AND status="reserved"', (status, ticket, self.key)).rowcount
            if changed != 1:
                fail('BUDGET_LEDGER_CONFLICT')

    def stat_call(self, call, timeout):
        """Invoked for EACH HTTP attempt, including transport retries.

        A confirmed provider failure is free. Uncertain transport/JSON results keep
        their reservation. A successful envelope is charged even if data is invalid.
        """
        ticket = self.reserve_stat()
        response = call(timeout)  # A crash/timeout keeps the reservation durably.
        try:
            status = getattr(response, 'status_code', 200)
            status = status if type(status) is int else 200
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and payload.get('success') is True and status < 300 and payload.get('code') == 200:
                self.settle(ticket, 'incurred')
            elif (isinstance(payload, dict) and (payload.get('success') is False or
                    (type(payload.get('code')) is int and payload['code'] >= 400))) or 400 <= status < 500:
                self.settle(ticket, 'failed')
            # Other responses are ambiguous, not proved free by a local parse error.
        except BaseException:
            if callable(getattr(response, 'close', None)):
                response.close()
            raise
        return response

    def evidence(self, results):
        return digest([self.contract_hash, [(r['platform'], r['count']) for r in results]])

    def estimate(self, results, maximum=None):
        total = 0
        count = 0
        rates = []
        for row in results:
            platform, n = row['platform'], row['count']
            self.check_platforms([platform])
            if type(n) is not int or n < 0:
                fail('BUDGET_VOLUME_INVALID')
            total += n * self.rates[platform]
            count += n
            rates.append(self.rates[platform])
        if not rates:
            fail('BUDGET_VOLUME_INVALID')
        if maximum is not None:
            if type(maximum) is not int or maximum < count:
                fail('BUDGET_VOLUME_INVALID')
            # Reserve approved headroom at the highest selected platform rate.
            total += (maximum - count) * max(rates)
        return total

    def record_quote(self, results):
        ref = uuid.uuid4().hex
        with self.connect() as conn:
            self.check(conn, self.estimate(results))
            conn.execute('INSERT INTO quotes VALUES (?,?,?,?)', (ref, self.key, self.price_hash, self.evidence(results)))
        return ref

    def check_estimate(self, results):
        with self.connect() as conn:
            self.check(conn, self.estimate(results))

    def validate_quote(self, conn, receipt, maximum):
        row = conn.execute('SELECT task,price_hash,evidence_hash FROM quotes WHERE id=?', (receipt.get('budget_ref'),)).fetchone()
        if row != (self.key, self.price_hash, self.evidence(receipt['results'])):
            fail('BUDGET_PRECHECK_REQUIRED_OR_CHANGED')
        self.check(conn, self.estimate(receipt['results'], maximum))

    def approve(self, receipt, maximum):
        with self.connect() as conn:
            self.validate_quote(conn, receipt, maximum)

    def mark_known_creation(self):
        """Reconciliation confirms occurrence, not the final invoice amount."""
        with self.connect() as conn:
            row = conn.execute('SELECT id,status FROM charges WHERE task=? AND operation="create"', (self.key,)).fetchone()
            if not row or row[1] not in ('reserved', 'incurred'):
                fail('BUDGET_LEDGER_MISSING_OR_CHANGED')
            conn.execute('UPDATE charges SET status="incurred" WHERE id=?', (row[0],))

    def claim_create(self, receipt, maximum, claim):
        """Hold the money transaction across the local idempotency claim, never HTTP.

        Interruption after registry claim but before money commit still prohibits
        replay through the registry. Money is committed before any POST can occur.
        """
        with self.connect() as conn:
            existing = conn.execute('SELECT 1 FROM charges WHERE task=? AND operation="create"', (self.key,)).fetchone()
            if existing:
                fail('BUDGET_CREATE_OUTCOME_RECONCILE_ONLY')
            self.validate_quote(conn, receipt, maximum)
            record, won = claim()
            ticket = uuid.uuid4().hex if won else None
            if won:
                conn.execute('INSERT INTO charges VALUES (?,?,?,?,?)', (ticket, self.key, 'create', self.estimate(receipt['results'], maximum), 'reserved'))
        return record, won, ticket
