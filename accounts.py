"""Instagram ACCOUNT watchlist for the Agent-Reach browser lane.

While the gallery-dl hashtag lane is broken (dead /api/v1/tags endpoint),
this lane watches hiring accounts' recent posts instead — via
`opencli instagram user <account>` in the logged-in Agent Reach browser.

Proven accounts posted real hiring reels we already captured; the platform
handles are best-known guesses — if a fetch comes back empty, verify the
exact handle with `opencli instagram search <name> --limit 5`.
"""

ACCOUNTS: list[str] = [
    # proven hiring-reel posters (owners of reels we already scraped)
    "careerswala",
    "job.shob",
    "jobnow247",
    "official.gurugram",
    "rojgarwithfauji",
    "gutsymonks",
    "ncr____job____noida_01",
    # Indian job platforms / aggregators with hiring content
    "naukridotcom",
    "workindia",
    "instahyre",
    "cutshort",
    "hirect",
    "apna.co",
]
