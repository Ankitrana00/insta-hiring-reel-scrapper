"""India-focused hiring hashtags for the daily scrape rotation.

The daily batch picks `REELS_BATCH_SIZE` tags starting at
`(day_of_year * batch) % len(WATCHLIST)` and wraps modularly, so every tag is
visited once per rotation cycle regardless of list/batch sizes. Keep tags
lowercase, no `#` prefix (Instagram tag slugs are lowercase).
"""

WATCHLIST: list[str] = [
    # national / generic
    "jobsindia",
    "hiringindia",
    "hiringnow",
    "nowhiring",
    "jobalert",
    "jobalertindia",
    "jobvacancy",
    "vacancyindia",
    "jobopeningindia",
    "jobsearchindia",
    "applynowindia",
    # startup ecosystem
    "startupjobs",
    "startupjobsindia",
    "startuphiring",
    # walk-in / immediate joiners
    "walkininterview",
    "walkinindia",
    "walkindelhi",
    # cities (NCR first — this is a NCR-focused map)
    "delhijobs",
    "ncrjobs",
    "gurugramjobs",
    "gurgaonjobs",
    "noidajobs",
    "delhincareers",
    "faridabadjobs",
    "mumbaijobs",
    "bangalorejobs",
    "bengalurujobs",
    "hyderabadjobs",
    "punejobs",
    "chennaijobs",
    "kolkatajobs",
    "ahmedabadjobs",
    "jaipurjobs",
    # work modes / seniority
    "remotejobsindia",
    "workfromhomeindia",
    "internshipsindia",
    "internshipindia",
    "freshersjobs",
    "techjobsindia",
    "itjobsindia",
]
