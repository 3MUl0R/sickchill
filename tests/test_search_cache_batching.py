"""
Tests for the batched cache lookup in TVCache (the fix for the 19-minute daily search).

find_needed_episodes used to issue one SQL query per wanted episode, each an unindexed full scan of
the results table, all of it inside a single cache.db lock. With ~12,000 wanted episodes that took a
quarter of an hour, monopolising the serial search queue and blocking post-processing.

The replacement fetches each wanted show's rows once and replays the episode list in Python. That is
only safe if it reproduces `episodes LIKE '%|N|%' AND quality IN (...)` exactly -- and that predicate
is subtler than it looks:

  * LIKE is NUL-terminated: length("\\0|1|") is 0, so "\\0|1|" matches nothing.
  * A number must be flanked by pipes: "1|2" matches neither 1 nor 2.
  * "|1|1|" satisfies the predicate once, not twice.
  * quality is TEXT compared against integer literals under TEXT affinity, so "08" != 8.
  * indexerid is NUMERIC: `= 10` matches 10 and 10.0, but not 10.5, "10"-as-BLOB, or NULL.

So the centrepiece here is a differential test that runs the *original* SQL as an oracle against the
new code, over an exhaustive edge-case table and randomised inputs, comparing row identity,
multiplicity and order. Every bullet above is a bug that oracle caught during review.
"""
import itertools
import random
import sqlite3
import unittest

from sickchill.oldbeard import common
from sickchill.oldbeard.databases import cache
from sickchill.oldbeard.tvcache import TVCache
from sickchill.tv import TVEpisode, TVShow
from tests import conftest

PROVIDER = "testprovider"

# Every shape review turned up: well-formed, malformed (missing delimiters), and NUL-bearing.
EPISODES_VALUES = [
    "|1|",
    "|1|1|",
    "|1|2|",
    "|1|2|1|",
    "|11|",
    "|0|",
    "",
    None,
    "1",
    "|1",
    "1|",
    "1|2",
    "1|2|",
    "|1|2",
    "||1||",
    "|",
    "||",
    "\x00",
    "\x00|1|",
    "|1|\x00|2|",
    "|1\x00|",
    "|1|2|\x00|3|",
]
QUALITY_VALUES = ["8", "08", "8.0", "", None, 8, 8.0, 32]
INDEXERID_VALUES = [10, 10.0, 10.5, "10", None]
SEASON_VALUES = [1, 2, -1, 1.0]


class FakeShow:
    def __init__(self, indexerid):
        self.indexerid = indexerid


class FakeEpisode:
    def __init__(self, indexerid, season, episode, wanted_quality):
        self.show = FakeShow(indexerid)
        self.season = season
        self.episode = episode
        self.wantedQuality = wanted_quality


class FakeDBConnection:
    """Just enough of db.DBConnection for _cached_results_for_episodes: a select() returning Rows.

    The projection is widened to carry rowid so the differential tests can compare true row identity
    rather than a column that merely happens to be unique. WHERE and ORDER BY are untouched, so this
    does not change which rows come back or in what order.
    """

    def __init__(self, connection):
        self.connection = connection

    def select(self, query, args=None):
        query = query.replace("SELECT * FROM results", "SELECT rowid AS rowid, * FROM results", 1)
        return self.connection.execute(query, args or []).fetchall()


def make_cache_db(rows):
    """Build an in-memory results table. `rows` are (season, episodes, indexerid, url, quality)."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE results (provider TEXT, name TEXT, season NUMERIC, episodes TEXT, indexerid NUMERIC, "
        "url TEXT, time NUMERIC, quality TEXT, release_group TEXT, version NUMERIC)"
    )
    for season, episodes, indexerid, url, quality in rows:
        connection.execute(
            "INSERT INTO results (provider, name, season, episodes, indexerid, url, time, quality, release_group, version) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, '', -1)",
            (PROVIDER, url, season, episodes, indexerid, url, quality),
        )
    return connection


def oracle(connection, episodes, key="rowid"):
    """The original implementation: one query per episode, flattened in request order.

    Returns each matched row's rowid, so the comparison is on row identity -- which is precisely what
    the equivalence claim covers: the same rows, the same duplicates, in the same order.
    """
    results = []
    for episode in episodes:
        query = (
            "SELECT rowid AS rowid, url FROM results WHERE provider = ? AND indexerid = ? AND season = ? AND episodes LIKE ? "
            "AND quality IN (" + ",".join(str(quality) for quality in episode.wantedQuality) + ")"
        )
        args = [PROVIDER, episode.show.indexerid, episode.season, "%|" + str(episode.episode) + "|%"]
        results.append([row[key] for row in connection.execute(query, args)])
    return list(itertools.chain(*results))


def subject(connection, episodes, chunk=None, key="rowid"):
    """The new implementation, reached through the real TVCache method."""
    tvcache = TVCache.__new__(TVCache)
    tvcache.provider_id = PROVIDER
    if chunk is not None:
        tvcache.SHOW_ID_CHUNK = chunk
    return [row[key] for row in tvcache._cached_results_for_episodes(FakeDBConnection(connection), episodes)]


class CacheRowKeysTest(unittest.TestCase):
    """_cache_row_keys must reproduce `episodes LIKE '%|N|%'` for arbitrary column values."""

    @staticmethod
    def tokens(episodes, indexerid=10, season=1):
        row = {"episodes": episodes, "indexerid": indexerid, "season": season}
        return [key[2] for key in TVCache._cache_row_keys(row)]

    def test_well_formed(self):
        self.assertEqual(self.tokens("|1|"), ["1"])
        self.assertEqual(self.tokens("|3|4|"), ["3", "4"])
        self.assertEqual(self.tokens("|0|"), ["0"], "episode 0 must stay matchable")
        self.assertEqual(self.tokens("|11|"), ["11"])
        self.assertEqual(self.tokens("||1||"), ["1"])

    def test_duplicate_tokens_are_collapsed(self):
        # "|1|1|" satisfies LIKE '%|1|%' once, so the row must be offered once, not twice.
        self.assertEqual(self.tokens("|1|1|"), ["1"])
        self.assertEqual(self.tokens("|1|2|1|"), ["1", "2"])

    def test_tokens_must_be_flanked_by_pipes(self):
        for episodes in ("1", "|1", "1|", "1|2", "|", "||", ""):
            self.assertEqual(self.tokens(episodes), [], f"{episodes!r} should yield no token")
        # Only the flanked number survives.
        self.assertEqual(self.tokens("|1|2"), ["1"])
        self.assertEqual(self.tokens("1|2|"), ["2"])
        self.assertEqual(self.tokens("1|2|1"), ["2"])

    def test_nul_truncates_like_sqlite_does(self):
        # SQLite's LIKE stops at the first NUL: length("\0|1|") is 0, not 4.
        self.assertEqual(self.tokens("\x00|1|"), [])
        self.assertEqual(self.tokens("\x00"), [])
        self.assertEqual(self.tokens("|1|\x00|2|"), ["1"])
        self.assertEqual(self.tokens("|1|2|\x00|3|"), ["1", "2"])
        self.assertEqual(self.tokens("|1\x00|"), [])

    def test_non_text_episodes_yield_nothing(self):
        self.assertEqual(self.tokens(None), [])
        # A BLOB does satisfy the LIKE, but the downstream split("|")[1] then raises TypeError and
        # search_rss's caller skips the whole provider. Skipping the row is the safe divergence.
        self.assertEqual(self.tokens(b"|1|"), [])

    def test_keys_carry_raw_values_not_coercions(self):
        # int() would fold 10.5 and b"10" onto 10; SQLite's `indexerid = 10` matches neither.
        self.assertEqual(list(TVCache._cache_row_keys({"episodes": "|1|", "indexerid": 10.5, "season": 1})), [(10.5, 1, "1")])
        self.assertEqual(list(TVCache._cache_row_keys({"episodes": "|1|", "indexerid": None, "season": 1})), [(None, 1, "1")])
        # A REAL that equals the int hashes and compares equal, exactly as SQLite matches it.
        self.assertEqual(list(TVCache._cache_row_keys({"episodes": "|1|", "indexerid": 10.0, "season": 1})), [(10.0, 1, "1")])
        self.assertEqual((10.0, 1, "1"), (10, 1, "1"))

    def test_agrees_with_sqlite_like_over_every_episodes_value(self):
        """The parity check that caught both the flanking and the NUL bugs."""
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE probe (episodes TEXT)")
        mismatches = []
        for episodes, number in itertools.product(EPISODES_VALUES, ("0", "1", "2", "3", "11")):
            if not isinstance(episodes, str):
                continue
            connection.execute("DELETE FROM probe")
            connection.execute("INSERT INTO probe VALUES (?)", (episodes,))
            matched_sql = connection.execute("SELECT COUNT(*) FROM probe WHERE episodes LIKE ?", (f"%|{number}|%",)).fetchone()[0] > 0
            matched_helper = number in self.tokens(episodes)
            if matched_sql != matched_helper:
                mismatches.append((episodes, number, matched_sql, matched_helper))
        self.assertEqual(mismatches, [], f"helper disagrees with SQLite LIKE: {mismatches}")


class DifferentialTest(unittest.TestCase):
    """The new code must return the same rows, duplicates and order as the SQL it replaces."""

    def test_exhaustive_edge_table(self):
        rows = [
            (season, episodes, indexerid, f"u{index}", quality)
            for index, (episodes, quality, indexerid, season) in enumerate(
                itertools.product(EPISODES_VALUES, QUALITY_VALUES, INDEXERID_VALUES, SEASON_VALUES)
            )
        ]
        connection = make_cache_db(rows)
        episodes = [
            FakeEpisode(10, 1, 1, [8, 32]),
            FakeEpisode(10, 1, 1, [8, 32]),  # the same episode requested twice must duplicate its rows
            FakeEpisode(10, 2, 2, [8]),
            FakeEpisode(10, 1, 0, [8]),
            FakeEpisode(10, 1, 3, [8]),
            FakeEpisode(10, 1, 11, [8]),
            FakeEpisode(10, 1, 1, []),  # empty wantedQuality -> SQL "IN ()" matches nothing
            FakeEpisode(99, 1, 1, [8]),  # a show with no cached rows
        ]
        expected = oracle(connection, episodes)
        self.assertTrue(expected, "fixture should match something, or it proves nothing")
        self.assertEqual(subject(connection, episodes), expected)

    def test_randomised_seeds(self):
        for seed in range(200):
            random.seed(seed)
            rows = [
                (
                    random.choice(SEASON_VALUES),
                    random.choice(EPISODES_VALUES),
                    random.choice([10, 20, 10.0]),
                    f"u{index}",
                    random.choice(QUALITY_VALUES),
                )
                for index in range(30)
            ]
            connection = make_cache_db(rows)
            episodes = [
                FakeEpisode(
                    random.choice([10, 20]),
                    random.choice([1, 2, -1]),
                    random.choice([0, 1, 2, 3, 11]),
                    random.sample([8, 32], random.randint(0, 2)),
                )
                for _ in range(random.randint(1, 8))
            ]
            if episodes and random.random() < 0.4:
                episodes.append(episodes[0])
            self.assertEqual(subject(connection, episodes), oracle(connection, episodes), f"seed {seed}")

    def test_multi_episode_row_is_returned_once_per_wanted_episode(self):
        """Multiplicity is observable: should_use_ai_fallback gates on len(results)."""
        connection = make_cache_db([(1, "|3|4|", 10, "u0", "8")])
        both = [FakeEpisode(10, 1, 3, [8]), FakeEpisode(10, 1, 4, [8])]
        self.assertEqual(subject(connection, both, key="url"), ["u0", "u0"])
        self.assertEqual(subject(connection, both), oracle(connection, both))
        # ... but only once when a single episode names it twice.
        connection = make_cache_db([(1, "|3|3|", 10, "u0", "8")])
        one = [FakeEpisode(10, 1, 3, [8])]
        self.assertEqual(subject(connection, one, key="url"), ["u0"])
        self.assertEqual(subject(connection, one), oracle(connection, one))

    def test_quality_is_compared_as_canonical_decimal_string(self):
        connection = make_cache_db([(1, "|1|", 10, "canonical", "8"), (1, "|1|", 10, "padded", "08"), (1, "|1|", 10, "real", "8.0")])
        episodes = [FakeEpisode(10, 1, 1, [8])]
        # SQLite compares the TEXT column against the literal 8 under TEXT affinity: only "8" matches.
        self.assertEqual(subject(connection, episodes, key="url"), ["canonical"])
        self.assertEqual(subject(connection, episodes), oracle(connection, episodes))

    def test_order_is_preserved_because_ties_are_decided_by_it(self):
        # Two same-quality rows for one episode: pick_best_result sorts stably, so order decides.
        connection = make_cache_db([(1, "|1|", 10, "first", "8"), (1, "|1|", 10, "second", "8")])
        episodes = [FakeEpisode(10, 1, 1, [8])]
        self.assertEqual(subject(connection, episodes, key="url"), ["first", "second"])
        self.assertEqual(subject(connection, episodes), oracle(connection, episodes))

    def test_order_is_pinned_where_the_old_query_left_it_to_the_planner(self):
        """ORDER BY rowid is load-bearing, and this is the case that shows why.

        SQLite promises no row order without ORDER BY. A plain scan happens to yield rowid order, so
        only an index whose trailing column sorts a group differently reveals the difference. Here
        'earlier' has the higher quality string, so an index on (indexerid, season, quality) walks the
        group backwards -- and the *old* per-episode query, having no ORDER BY, walks it backwards too.

        The order the old code produced was therefore a property of the query planner, not of the code.
        Under the index this change actually ships, (indexerid, season, provider), provider is constant
        within a group and both orders collapse to rowid order -- which is why every other test here
        sees the old and new implementations agree. We pin rowid order rather than inherit that
        fragility, because pick_best_result sorts stably and ties are decided by arrival order.
        """
        connection = make_cache_db([(1, "|1|", 10, "earlier", "9"), (1, "|1|", 10, "later", "1")])
        connection.execute("CREATE INDEX ix_reorder ON results (indexerid, season, quality)")
        episodes = [FakeEpisode(10, 1, 1, [9, 1])]

        planner_order = [row["url"] for row in connection.execute("SELECT * FROM results WHERE provider = ? AND indexerid IN (10)", (PROVIDER,))]
        self.assertEqual(planner_order, ["later", "earlier"], "fixture must reorder, or this proves nothing")
        self.assertEqual(oracle(connection, episodes, key="url"), ["later", "earlier"], "the old query inherited the planner's order")

        # We do not. Dropping ORDER BY rowid from the fetch makes this assertion fail.
        self.assertEqual(subject(connection, episodes, key="url"), ["earlier", "later"])

    def test_chunking_beyond_sqlite_variable_limit(self):
        rows = [(1, "|1|", show_id, f"u{show_id}", "8") for show_id in range(1200)]
        connection = make_cache_db(rows)
        episodes = [FakeEpisode(show_id, 1, 1, [8]) for show_id in range(1200)]
        self.assertEqual(subject(connection, episodes, chunk=500), oracle(connection, episodes))

    def test_results_is_a_rowid_table(self):
        # ORDER BY rowid is load-bearing now, not incidental. A WITHOUT ROWID redefinition must fail here.
        connection = make_cache_db([(1, "|1|", 10, "u0", "8")])
        self.assertEqual(connection.execute("SELECT rowid FROM results").fetchone()[0], 1)


class BoundValueContractTest(conftest.SickChillTestDBCase):
    """The equivalence holds only for int bound values. Assert the real callers still provide them.

    _cached_results_for_episodes keys on raw values, so a str season silently matches nothing rather
    than raising. If TVShow.get_episode ever stops coercing, this fails instead of the daily search
    quietly finding no cached results.
    """

    def test_real_episode_objects_carry_int_season_and_episode(self):
        show = TVShow(1, 121361)
        show.name = "contract"
        show.save_to_db()

        # get_episode runs season/episode through try_int; pass strings to prove the coercion happens.
        episode = show.get_episode("2", "3")
        self.assertIsInstance(episode.season, int)
        self.assertIsInstance(episode.episode, int)
        self.assertIsInstance(show.indexerid, int)

    def test_wanted_quality_is_a_list_of_ints(self):
        show = TVShow(1, 121361)
        show.name = "contract"
        show.quality = common.Quality.combineQualities([common.Quality.SDTV, common.Quality.HDTV], [])
        show.save_to_db()

        episode = TVEpisode(show, 1, 1)
        # search.wanted_episodes() assigns exactly this expression.
        allowed, preferred = common.Quality.splitQuality(show.quality)
        episode.wantedQuality = [quality for quality in set(allowed + preferred) if quality > common.Quality.SDTV and quality != common.Quality.UNKNOWN]
        self.assertTrue(episode.wantedQuality)
        for quality in episode.wantedQuality:
            self.assertIsInstance(quality, int)

    def test_string_season_would_not_match_which_is_why_the_contract_matters(self):
        connection = make_cache_db([(1, "|1|", 10, "u0", "8")])
        self.assertEqual(subject(connection, [FakeEpisode(10, 1, 1, [8])], key="url"), ["u0"])
        # Out of contract: SQLite applies NUMERIC affinity to a bound "1" and still matches; the raw
        # tuple key does not. Recorded so the divergence is known rather than discovered in the field.
        self.assertEqual(subject(connection, [FakeEpisode(10, "1", 1, [8])], key="url"), [])
        self.assertEqual(oracle(connection, [FakeEpisode(10, "1", 1, [8])], key="url"), ["u0"])


class ResultsIndexesMigrationTest(unittest.TestCase):
    class Connection:
        """Minimal db.DBConnection surface used by SchemaUpgrade."""

        def __init__(self, connection):
            self.connection = connection

        def select(self, query, args=None):
            return self.connection.execute(query, args or []).fetchall()

        def action(self, query, args=None):
            return self.connection.execute(query, args or [])

        def has_table(self, table_name):
            return bool(self.select("SELECT 1 FROM sqlite_master WHERE name = ?", [table_name]))

    @staticmethod
    def make_results_table(connection):
        connection.execute("CREATE TABLE results (provider TEXT, season NUMERIC, episodes TEXT, indexerid NUMERIC, url TEXT, quality TEXT)")

    def index_rows(self, connection):
        return connection.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?", [cache.ResultsIndexes.index_name]
        ).fetchall()

    def test_creates_the_index_on_a_bare_results_table(self):
        connection = sqlite3.connect(":memory:")
        self.make_results_table(connection)
        migration = cache.ResultsIndexes(self.Connection(connection))

        self.assertFalse(migration.test())
        migration.execute()
        self.assertTrue(migration.test())
        self.assertEqual(self.index_rows(connection), [(cache.ResultsIndexes.index_name, "results")])

        migration.execute()  # idempotent
        self.assertEqual(len(self.index_rows(connection)), 1)

    def test_missing_results_table_does_not_raise(self):
        connection = sqlite3.connect(":memory:")
        migration = cache.ResultsIndexes(self.Connection(connection))
        migration.execute()  # CREATE INDEX on a missing table would raise OperationalError
        self.assertFalse(migration.test())

    def test_same_name_index_on_another_table_is_not_mistaken_for_success(self):
        """The idx_url-on-oznzb bug class: index names are global, CREATE ... IF NOT EXISTS no-ops."""
        connection = sqlite3.connect(":memory:")
        self.make_results_table(connection)
        connection.execute("CREATE TABLE oznzb (url TEXT)")
        connection.execute(f"CREATE INDEX {cache.ResultsIndexes.index_name} ON oznzb (url)")

        migration = cache.ResultsIndexes(self.Connection(connection))
        # has_index() would say True here; test() must not, because results is still unindexed.
        self.assertFalse(migration.test())
        migration.execute()  # a no-op, but must not raise
        self.assertFalse(migration.test())


if __name__ == "__main__":
    unittest.main()
