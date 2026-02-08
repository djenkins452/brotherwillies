from django.db import models


class GolfEvent(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, blank=True, null=True)
    external_id = models.CharField(max_length=100, blank=True, db_index=True)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ['start_date']

    def __str__(self):
        return self.name


class Golfer(models.Model):
    name = models.CharField(max_length=100)
    external_id = models.CharField(max_length=100, blank=True, db_index=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class GolfOddsSnapshot(models.Model):
    event = models.ForeignKey(GolfEvent, on_delete=models.CASCADE, related_name='odds_snapshots')
    golfer = models.ForeignKey(Golfer, on_delete=models.CASCADE, related_name='odds_snapshots')
    captured_at = models.DateTimeField()
    sportsbook = models.CharField(max_length=50, default='consensus')
    outright_odds = models.IntegerField(help_text='American format odds')
    implied_prob = models.FloatField()

    class Meta:
        ordering = ['-captured_at']

    def __str__(self):
        return f"{self.golfer} @ {self.event} ({self.outright_odds})"


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
