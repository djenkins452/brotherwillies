from django.db import models


class GolfEvent(models.Model):
    name = models.CharField(max_length=200)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ['start_date']

    def __str__(self):
        return self.name


class Golfer(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class GolfRound(models.Model):
    event = models.ForeignKey(GolfEvent, on_delete=models.CASCADE, related_name='rounds')
    golfer = models.ForeignKey(Golfer, on_delete=models.CASCADE, related_name='rounds')
    round_number = models.IntegerField()
    score = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['event', 'round_number']
        unique_together = ['event', 'golfer', 'round_number']

    def __str__(self):
        return f"{self.golfer} R{self.round_number} ({self.event})"
