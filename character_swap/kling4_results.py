"""Strict parsing and URL-free reporting for documented Kling 4.0 tasks.

Protected URLs are returned separately to the provider for immediate use in
memory. They never appear in TaskResult or the safe billing/task report.
"""

from dataclasses import dataclass
from decimal import Decimal
import re

from .kling_http import media_url
from .providers.base import ProviderError, TaskResult, TaskStatus
from .store import SafetyError


RESULT_MARKER = 'https://result.invalid/available'
STATUSES = {'submitted': TaskStatus.PENDING, 'processing': TaskStatus.RUNNING,
            'succeeded': TaskStatus.SUCCEEDED, 'failed': TaskStatus.FAILED}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise SafetyError('Invalid Kling 4.0 task or asset identifier.')
    return value


def _decimal(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
        raise ValueError
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise ValueError
    return value  # Preserve documented decimal strings, avoiding float rounding.


@dataclass(frozen=True)
class Usage:
    input_duration: str | None = None
    output_duration: str | None = None


@dataclass(frozen=True)
class BillingRecord:
    charge_type: str
    amount: str
    currency: str | None = None
    package_type: str | None = None
    cash_type: str | None = None
    list_price: str | None = None


@dataclass(frozen=True)
class TaskReport:
    result: TaskResult
    video_id: str | None = None
    video_duration: str | None = None
    usage: Usage | None = None
    billing: tuple[BillingRecord, ...] | None = None
    create_time: int | None = None
    update_time: int | None = None
    provider_failure_message_present: bool = False

    def to_dict(self):
        """Safe to display/persist; never includes raw provider messages/URLs.

        No amounts are combined across currencies, cash types or unit packages.
        Missing usage/billing means unknown, not zero or refunded spending.
        """
        from dataclasses import asdict
        return {'task_id': self.result.task_id, 'status': self.result.status.value,
                'error_code': self.result.error.code if self.result.error else None,
                'error_message': self.result.error.message if self.result.error else None,
                'video_id': self.video_id, 'reported_video_duration_seconds': self.video_duration,
                'normalized_input_duration_seconds': self.usage.input_duration if self.usage else None,
                'billed_output_duration_seconds': self.usage.output_duration if self.usage else None,
                'usage_available': self.usage is not None,
                'billing': [asdict(row) for row in self.billing] if self.billing is not None else None,
                'create_time': self.create_time, 'update_time': self.update_time,
                'provider_failure_message_present': self.provider_failure_message_present,
                'protected_results_retention_days': 30}


def _envelope(response):
    if not isinstance(response, dict) or type(response.get('code')) is not int or response['code'] != 0:
        raise ValueError
    return response.get('data')


def _task(data):
    task_id = identifier(data.get('id'))
    status = data.get('status')
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError
    if 'message' in data and not isinstance(data['message'], str):
        raise ValueError
    usage = None
    if 'usage' in data:
        if not isinstance(data['usage'], dict):
            raise ValueError
        usage = Usage(*(_decimal(data['usage'][name]) if name in data['usage'] else None
                        for name in ('input_duration', 'output_duration')))
    billing = None
    if 'billing' in data:
        if not isinstance(data['billing'], list):
            raise ValueError
        records = []
        for row in data['billing']:
            if not isinstance(row, dict) or row.get('charge_type') not in {'cash', 'unit'}:
                raise ValueError
            amount = _decimal(row.get('amount'))
            currency, package, cash_type = (row.get(name) for name in ('currency', 'package_type', 'cash_type'))
            if row['charge_type'] == 'cash':
                if currency not in {'CNY', 'USD'} or package is not None:
                    raise ValueError
                if cash_type is not None and cash_type not in {'balance', 'test_balance', 'reward_balance'}:
                    raise ValueError
            elif package not in {'video', 'image', 'audio'} or currency is not None or cash_type is not None:
                raise ValueError
            price = _decimal(row['list_price']) if 'list_price' in row else None
            if row['charge_type'] != 'cash' and price is not None:
                raise ValueError
            records.append(BillingRecord(row['charge_type'], amount, currency, package, cash_type, price))
        billing = tuple(records)
    timestamps = []
    for name in ('create_time', 'update_time'):
        value = data.get(name)
        if name in data and (type(value) is not int or not 0 <= value <= 2**63-1):
            raise ValueError
        timestamps.append(value)
    if all(value is not None for value in timestamps) and timestamps[1] < timestamps[0]:
        raise ValueError
    outputs = data.get('outputs', [])
    if not isinstance(outputs, list) or any(not isinstance(row, dict) or not isinstance(row.get('type'), str) for row in outputs):
        raise ValueError
    videos = [row for row in outputs if row['type'] == 'video']
    if len(videos) > 1:
        raise ValueError  # Explicit selection policy required for multiple videos.
    video_id = duration = output_url = None
    if videos:
        video = videos[0]
        video_id = identifier(video.get('id'))
        duration = _decimal(video.get('duration'))
        if Decimal(duration) <= 0:
            raise ValueError
        output_url = video.get('url')
        try:
            media_url(output_url)
        except SafetyError:
            output_url = None
        if 'watermark_url' in video and video['watermark_url'] not in (None, ''):
            media_url(video['watermark_url'])  # Validate, then discard.
    normalized = STATUSES[status]
    error = None
    if normalized == TaskStatus.SUCCEEDED and output_url is None:
        normalized = TaskStatus.FAILED
        error = ProviderError('invalid_output_url', 'Provider success had no valid video URL. Manual reconciliation required.')
    elif normalized == TaskStatus.FAILED:
        # Arbitrary provider messages can contain credentials or protected URLs.
        error = ProviderError('task_failed', 'Kling could not complete the video edit.')
    # URLs are only useful after successful completion, never for a failed job.
    output_url = output_url if normalized == TaskStatus.SUCCEEDED else None
    result = TaskResult(task_id, normalized, RESULT_MARKER if output_url else None, error, status)
    return TaskReport(result, video_id, duration, usage, billing, *timestamps,
                      status == 'failed' and bool(data.get('message'))), output_url


def parse_receipt(response):
    try:
        data = _envelope(response)
        if not isinstance(data, dict):
            raise ValueError
        return _task(data)
    except Exception:
        raise SafetyError('Invalid Kling 4.0 creation response; reconcile before any resubmission.') from None


def parse_query(response, expected_task_id):
    try:
        expected = identifier(expected_task_id)
        tasks = _envelope(response)
        if not isinstance(tasks, list) or not tasks or any(not isinstance(row, dict) for row in tasks):
            raise ValueError
        ids = [identifier(row.get('id')) for row in tasks]
        if len(set(ids)) != len(ids) or expected not in ids:
            raise ValueError
        return _task(tasks[ids.index(expected)])
    except Exception:
        raise SafetyError('Invalid Kling 4.0 task response; no task state was accepted.') from None
