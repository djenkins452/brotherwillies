import uuid
from django.db import models
from django.conf import settings


class FeedbackComponent(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PartnerFeedback(models.Model):
    class Status(models.TextChoices):
        NEW = 'NEW', 'New'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        READY = 'READY', 'Ready'
        DISMISSED = 'DISMISSED', 'Dismissed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='partner_feedback',
    )
    component = models.ForeignKey(
        FeedbackComponent,
        on_delete=models.PROTECT,
        related_name='feedback_items',
    )
    title = models.CharField(max_length=200)
    description = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.NEW,
    )
    reviewer_notes = models.TextField(blank=True, default='')
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} ({self.get_status_display()})'

    @property
    def is_ready_for_ai(self):
        return self.status == self.Status.READY
