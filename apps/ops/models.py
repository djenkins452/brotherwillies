"""Ops Command Center models — the durable record of API calls + cron runs.

Two append-only tables. Both are indexed on the timestamp column descending
because every dashboard query is "the most recent N rows."

Why these live in their own app: they instrument code across datahub, mlb,
core, and management commands. Putting them in datahub or mockbets would
create a circular import risk. apps.ops imports nothing app-specific.
"""
import uuid

from django.conf import settings
from django.db import models


class OddsApiUsage(models.Model):
    """One row per outbound call to the-odds-api.com.

    Captures enough to answer:
      - "Is the API healthy right now?" (recent status codes, latency)
      - "How close are we to quota?" (credits_used / credits_remaining
        from the response headers)
      - "When did the last 401 happen and on which sport?" (status_code
        + sport)
    """
    SPORT_CHOICES = [
        ('cbb', 'CBB'),
        ('cfb', 'CFB'),
        ('mlb', 'MLB'),
        ('college_baseball', 'College Baseball'),
        ('golf', 'Golf'),
        ('unknown', 'Unknown'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    sport = models.CharField(max_length=20, choices=SPORT_CHOICES, default='unknown')
    endpoint = models.CharField(max_length=512)
    status_code = models.IntegerField(null=True, blank=True)
    success = models.BooleanField(default=False)
    response_time_ms = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')
    # Credits info from response headers — only available on successful calls.
    # x-requests-used / x-requests-remaining are standard for the-odds-api.
    credits_used = models.IntegerField(null=True, blank=True)
    credits_remaining = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp']),
            models.Index(fields=['sport', '-timestamp']),
            models.Index(fields=['status_code', '-timestamp']),
        ]

    def __str__(self):
        marker = 'OK' if self.success else f'FAIL{self.status_code or "?"}'
        return f'[{self.sport}] {marker} {self.endpoint} @ {self.timestamp:%H:%M:%S}'


class CronRunLog(models.Model):
    """One row per management-command run, whether triggered by Railway cron,
    a manual trigger from the ops UI, or the deploy startup sequence.

    The status field is treated as a small state machine:
        running → success | failure | partial
    A row stuck in 'running' for more than a few minutes is itself a signal
    (the worker died mid-run); the dashboard should flag those.
    """
    TRIGGER_CHOICES = [
        ('cron', 'Scheduled Cron'),
        ('manual', 'Manual'),
        ('deploy', 'Deploy Startup'),
    ]
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('success', 'Success'),
        ('failure', 'Failure'),
        ('partial', 'Partial Success'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    command = models.CharField(max_length=120, db_index=True)
    trigger = models.CharField(max_length=10, choices=TRIGGER_CHOICES, default='cron')
    triggered_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cron_runs',
    )
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='running')
    duration_seconds = models.FloatField(null=True, blank=True)
    summary = models.TextField(blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    # Truncated stdout for at-a-glance debugging without leaving the dashboard.
    stdout_tail = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['-started_at']),
            models.Index(fields=['command', '-started_at']),
            models.Index(fields=['status', '-started_at']),
        ]

    def __str__(self):
        return f'[{self.status}] {self.command} ({self.trigger}) @ {self.started_at:%H:%M:%S}'

    @property
    def is_running(self) -> bool:
        return self.status == 'running'

    @property
    def is_success(self) -> bool:
        return self.status == 'success'
