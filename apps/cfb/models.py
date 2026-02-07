import uuid
from django.db import models


class Conference(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Team(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    conference = models.ForeignKey(Conference, on_delete=models.CASCADE, related_name='teams')
    rating = models.FloatField(default=50.0)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Game(models.Model):
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('live', 'Live'),
        ('final', 'Final'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_games')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_games')
    kickoff = models.DateTimeField()
    neutral_site = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='scheduled')

    class Meta:
        ordering = ['kickoff']

    def __str__(self):
        return f"{self.away_team} @ {self.home_team}"


class OddsSnapshot(models.Model):
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='odds_snapshots')
    captured_at = models.DateTimeField()
    sportsbook = models.CharField(max_length=50, default='consensus')
    market_home_win_prob = models.FloatField()
    market_away_win_prob = models.FloatField(null=True, blank=True)
    spread = models.FloatField(null=True, blank=True)
    total = models.FloatField(null=True, blank=True)
    moneyline_home = models.IntegerField(null=True, blank=True)
    moneyline_away = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-captured_at']

    def __str__(self):
        return f"Odds for {self.game}"

    def save(self, *args, **kwargs):
        if self.market_away_win_prob is None and self.market_home_win_prob is not None:
            self.market_away_win_prob = 1.0 - self.market_home_win_prob
        super().save(*args, **kwargs)


class InjuryImpact(models.Model):
    IMPACT_CHOICES = [
        ('low', 'Low'),
        ('med', 'Medium'),
        ('high', 'High'),
    ]
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='injuries')
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    impact_level = models.CharField(max_length=4, choices=IMPACT_CHOICES)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.team} - {self.impact_level} impact"
