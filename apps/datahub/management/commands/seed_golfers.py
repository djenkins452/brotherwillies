"""
Seed ~200 top PGA Tour golfers into the Golfer table.

Idempotent: uses get_or_create on name. Also backfills first_name/last_name
on any existing Golfer rows that have them blank.

Usage:
    python manage.py seed_golfers
"""
from django.core.management.base import BaseCommand
from apps.golf.models import Golfer


# Top ~200 PGA Tour players (2024-25 season, FedEx Cup rankings + major
# winners + notable names). Maintained manually for seed purposes.
# Format: "First Last" — the save() auto-splits into first_name / last_name.
GOLFERS = [
    # Top 50 FedEx Cup / world ranking
    "Scottie Scheffler",
    "Xander Schauffele",
    "Rory McIlroy",
    "Collin Morikawa",
    "Ludvig Aberg",
    "Wyndham Clark",
    "Patrick Cantlay",
    "Viktor Hovland",
    "Sahith Theegala",
    "Hideki Matsuyama",
    "Shane Lowry",
    "Tommy Fleetwood",
    "Russell Henley",
    "Sam Burns",
    "Sungjae Im",
    "Tony Finau",
    "Matt Fitzpatrick",
    "Brian Harman",
    "Max Homa",
    "Keegan Bradley",
    "Jason Day",
    "Akshay Bhatia",
    "Robert MacIntyre",
    "Tom Kim",
    "Chris Kirk",
    "Billy Horschel",
    "Corey Conners",
    "Justin Thomas",
    "Cameron Young",
    "Byeong Hun An",
    "Si Woo Kim",
    "Adam Scott",
    "Denny McCarthy",
    "Taylor Moore",
    "Christiaan Bezuidenhout",
    "Aaron Rai",
    "Sepp Straka",
    "Davis Thompson",
    "Tom Hoge",
    "Harris English",
    "Cam Davis",
    "Maverick McNealy",
    "Austin Eckroat",
    "Eric Cole",
    "Jake Knapp",
    "J.T. Poston",
    "Stephan Jaeger",
    "Nick Taylor",
    "Taylor Pendrith",
    "Nick Dunlap",
    # 51-100
    "Mackenzie Hughes",
    "Keith Mitchell",
    "Ben Griffin",
    "Luke Clanton",
    "Min Woo Lee",
    "Beau Hossler",
    "Andrew Novak",
    "Patrick Rodgers",
    "Matt McCarty",
    "Jhonattan Vegas",
    "Doug Ghim",
    "Justin Lower",
    "Lee Hodges",
    "Brendon Todd",
    "Nico Echavarria",
    "Luke List",
    "Mark Hubbard",
    "Peter Malnati",
    "Davis Riley",
    "Kurt Kitayama",
    "Kevin Yu",
    "Alex Noren",
    "Adam Hadwin",
    "Andrew Putnam",
    "Trace Crowe",
    "Thomas Detry",
    "Rico Hoey",
    "Taylor Montgomery",
    "Will Zalatoris",
    "Joel Dahmen",
    "Carson Young",
    "Chris Gotterup",
    "Brice Garnett",
    "Garrick Higgo",
    "Gary Woodland",
    "Charlie Reiter",
    "Sean O'Hair",
    "Harry Hall",
    "Parker Coody",
    "Michael Kim",
    "Greyson Sigg",
    "Ryan Fox",
    "Patrick Fishburn",
    "Mac Meissner",
    "Dylan Wu",
    "Emiliano Grillo",
    "Kevin Streelman",
    "Daniel Berger",
    "Ben Martin",
    "Matti Schmid",
    # 101-150
    "Cameron Champ",
    "Lucas Glover",
    "K.H. Lee",
    "Zach Johnson",
    "Francesco Molinari",
    "Webb Simpson",
    "Lanto Griffin",
    "Charley Hoffman",
    "Ryan Palmer",
    "Patton Kizzire",
    "Chad Ramey",
    "C.T. Pan",
    "Henrik Norlander",
    "David Lipsky",
    "Chan Kim",
    "Jimmy Walker",
    "Wesley Bryan",
    "Nate Lashley",
    "Austin Smotherman",
    "S.H. Kim",
    "Matt Kuchar",
    "Tyson Alexander",
    "Ben Silverman",
    "Chesson Hadley",
    "Joseph Bramlett",
    "Sam Ryder",
    "Vince Whaley",
    "Callum Tarren",
    "Kevin Tway",
    "Vincent Norrman",
    "Tom Whitney",
    "Martin Laird",
    "Michael Thorbjornsen",
    "Pierceson Coody",
    "Paul Haley II",
    "Sam Stevens",
    "Erik van Rooyen",
    "Matt NeSmith",
    "Sami Valimaki",
    "Doc Redman",
    "Adam Schenk",
    "Taylor Dickson",
    "Trevor Cone",
    "Carson Schaake",
    "Isaiah Salinda",
    "Chandler Phillips",
    "Cameron Beckman",
    "Dylan Frittelli",
    "Hayden Springer",
    "Alex Smalley",
    # 151-200 (notable names, past champions, fan favorites)
    "Brooks Koepka",
    "Bryson DeChambeau",
    "Jordan Spieth",
    "Jon Rahm",
    "Phil Mickelson",
    "Tiger Woods",
    "Dustin Johnson",
    "Cameron Smith",
    "Patrick Reed",
    "Sergio Garcia",
    "Louis Oosthuizen",
    "Abraham Ancer",
    "Joaquin Niemann",
    "Talor Gooch",
    "Harold Varner III",
    "Jason Kokrak",
    "Matthew Wolff",
    "Kevin Na",
    "Charles Howell III",
    "Pat Perez",
    "Lee Westwood",
    "Ian Poulter",
    "Bubba Watson",
    "Graeme McDowell",
    "Henrik Stenson",
    "Charl Schwartzel",
    "Branden Grace",
    "Anirban Lahiri",
    "Adrian Meronk",
    "Dean Burmester",
    "Tyrrell Hatton",
    "Rickie Fowler",
    "Justin Rose",
    "Zach Johnson",
    "Stewart Cink",
    "Kevin Kisner",
    "Brandt Snedeker",
    "Ryan Moore",
    "Martin Trainer",
    "Chez Reavie",
    "Brendan Steele",
    "Troy Merritt",
    "Dawie van der Walt",
    "Alejandro Tosti",
    "Thorbjorn Olesen",
    "Tom Fleetwood",
    "Nicolai Hojgaard",
    "Rasmus Hojgaard",
    "Victor Perez",
    "Matthieu Pavon",
]


class Command(BaseCommand):
    help = 'Seed ~200 top PGA Tour golfers into the Golfer table'

    def handle(self, *args, **options):
        created = 0
        updated = 0
        seen = set()

        for name in GOLFERS:
            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)

            golfer, was_created = Golfer.objects.get_or_create(
                name=name,
                defaults={},
            )

            if was_created:
                created += 1
            elif not golfer.last_name:
                # Backfill first/last on existing rows
                golfer.save()  # triggers auto-split in save()
                updated += 1

        # Also backfill any existing golfers from live data that lack first/last
        blank_last = Golfer.objects.filter(last_name='')
        for g in blank_last:
            g.save()  # triggers auto-split
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Golfers seeded: {created} created, {updated} backfilled'
        ))
