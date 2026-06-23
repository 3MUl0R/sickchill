from sickchill.oldbeard import db


# Add new migrations at the bottom of the list; subclass the previous migration.
class InitialSchema(db.SchemaUpgrade):
    def test(self):
        return self.has_table("db_version")

    def execute(self):
        queries = (
            ("CREATE TABLE lastUpdate (provider TEXT, time NUMERIC);",),
            ("CREATE TABLE lastSearch (provider TEXT, time NUMERIC);",),
            (
                "CREATE TABLE scene_exceptions ("
                "exception_id INTEGER PRIMARY KEY, indexer_id INTEGER, show_name TEXT, season NUMERIC DEFAULT -1, custom NUMERIC DEFAULT 0);",
            ),
            ("CREATE TABLE scene_names (indexer_id INTEGER, name TEXT);",),
            ("CREATE TABLE network_timezones (network_name TEXT PRIMARY KEY, timezone TEXT);",),
            ("CREATE TABLE scene_exceptions_refresh (list TEXT PRIMARY KEY, last_refreshed INTEGER);",),
            ("CREATE TABLE db_version (db_version INTEGER);",),
            (
                "CREATE TABLE results (provider TEXT, name TEXT, season NUMERIC, episodes TEXT, indexerid NUMERIC, url TEXT, time NUMERIC, quality TEXT, "
                "release_group TEXT, version NUMERIC, seeders INTEGER DEFAULT 0, leechers INTEGER DEFAULT 0, size INTEGER DEFAULT -1, status INTEGER DEFAULT 0, "
                "failed INTEGER DEFAULT 0, added TEXT DEFAULT CURRENT_TIMESTAMP);",
            ),
            ("INSERT INTO db_version(db_version) VALUES (1);",),
            ("CREATE UNIQUE INDEX IF NOT EXISTS idx_url ON results (url);",),
            ("CREATE INDEX IF NOT EXISTS provider ON results (provider);",),
            ("CREATE INDEX IF NOT EXISTS seeders ON results (seeders);",),
        )
        for query in queries:
            self.connection.action(query[0])


class ResultsTable(InitialSchema):
    def test(self):
        return self.has_table("results")

    def execute(self):
        import sickchill.settings

        self.connection.action(
            "CREATE TABLE results (provider TEXT, name TEXT, season NUMERIC, episodes TEXT, indexerid NUMERIC, url TEXT, time NUMERIC, quality TEXT,"
            "release_group TEXT, version NUMERIC, seeders INTEGER DEFAULT 0, leechers INTEGER DEFAULT 0, size INTEGER DEFAULT -1, status INTEGER DEFAULT 0, "
            "failed INTEGER DEFAULT 0, added TEXT DEFAULT CURRENT_TIMESTAMP);"
        )

        for provider in sickchill.settings.providerList:
            provider_id = provider.get_id()
            if self.has_table(provider_id):
                self.add_column(provider_id, "provider", col_type="TEXT", default=provider_id)
                self.add_column(provider_id, "seeders", col_type="INTEGER")
                self.add_column(provider_id, "leechers", col_type="INTEGER")
                self.add_column(provider_id, "size", col_type="INTEGER")
                self.add_column(provider_id, "status", col_type="INTEGER")
                self.add_column(provider_id, "failed", col_type="INTEGER")
                timestamp = self.connection.select("SELECT CURRENT_TIMESTAMP;")
                self.add_column(provider_id, "added", col_type="TEXT", default=timestamp[0]["CURRENT_TIMESTAMP"])

                # language=TEXT
                self.connection.action(
                    "INSERT INTO results SELECT provider, name , season, episodes, indexerid, url, time, quality,"
                    "release_group, version, seeders, leechers, size, status, failed, added FROM ?",
                    [provider_id],
                )
                self.connection.action("DROP TABLE {}".format(provider_id))


class AITables(ResultsTable):
    """Migration to add AI throttle and cache tables."""

    def test(self):
        return self.has_table("ai_throttle")

    def execute(self):
        # Create AI throttle table for rate limiting
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_throttle (
                context TEXT,
                scope TEXT,
                scope_key TEXT,
                last_attempt NUMERIC,
                last_success NUMERIC,
                PRIMARY KEY(context, scope, scope_key)
            )
            """
        )

        # Create AI cache table for response caching
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_cache (
                request_hash TEXT PRIMARY KEY,
                response_json TEXT,
                created NUMERIC,
                expires NUMERIC,
                context TEXT,
                scope TEXT,
                scope_key TEXT
            )
            """
        )

        # Index for cache cleanup
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache (expires)")


class AIPhase4Tables(AITables):
    """Migration to add Phase 4 AI tables for cost tracking, preferences, and feedback."""

    def test(self):
        return self.has_table("ai_usage")

    def execute(self):
        # Create AI usage table for cost tracking
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp NUMERIC NOT NULL,
                model TEXT NOT NULL,
                context TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                scope_key TEXT
            )
            """
        )
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_usage_timestamp ON ai_usage (timestamp)")
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_usage_scope ON ai_usage (scope_key)")

        # Create AI show preferences table
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_show_preferences (
                show_id INTEGER PRIMARY KEY,
                preferences_json TEXT NOT NULL,
                updated_at NUMERIC NOT NULL DEFAULT 0
            )
            """
        )

        # Create AI decisions table for feedback loop
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_decisions (
                decision_id TEXT PRIMARY KEY,
                decision_type TEXT NOT NULL,
                timestamp NUMERIC NOT NULL,
                show_id INTEGER,
                input_summary TEXT NOT NULL,
                output_summary TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning TEXT,
                raw_response TEXT
            )
            """
        )
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_decisions_timestamp ON ai_decisions (timestamp)")
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_decisions_show ON ai_decisions (show_id)")

        # Create AI feedback table
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                user_correction TEXT,
                notes TEXT,
                timestamp NUMERIC NOT NULL,
                FOREIGN KEY (decision_id) REFERENCES ai_decisions (decision_id)
            )
            """
        )
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_feedback_decision ON ai_feedback (decision_id)")


class AISchemaRepair(AIPhase4Tables):
    """Idempotently bring any prior or partial AI cache schema to the canonical shape.

    The AI cache tables can be created by either this migration chain or, as a dev/test
    fallback, on-demand by the AI modules. Earlier migration guards each check only a
    single table, so a partially-built or legacy database (e.g. a 2-column
    ai_show_preferences created by the old module path) could be left unrepaired. This
    migration always runs until every expected table, the ai_show_preferences.updated_at
    column, and every canonical index are present.
    """

    _TABLES = ("ai_throttle", "ai_cache", "ai_usage", "ai_show_preferences", "ai_decisions", "ai_feedback")
    _INDEXES = (
        "idx_ai_cache_expires",
        "idx_ai_usage_timestamp",
        "idx_ai_usage_scope",
        "idx_ai_decisions_timestamp",
        "idx_ai_decisions_show",
        "idx_ai_feedback_decision",
    )

    def test(self):
        return (
            all(self.has_table(table) for table in self._TABLES)
            and self.has_column("ai_show_preferences", "updated_at")
            and all(self.has_index(index) for index in self._INDEXES)
        )

    def execute(self):
        # 1) Ensure every table exists with the canonical definition. CREATE TABLE IF NOT
        #    EXISTS covers any table an earlier narrow-guarded migration skipped (for
        #    example ai_cache when only ai_throttle pre-existed).
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_throttle (
                context TEXT,
                scope TEXT,
                scope_key TEXT,
                last_attempt NUMERIC,
                last_success NUMERIC,
                PRIMARY KEY(context, scope, scope_key)
            )
            """
        )
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_cache (
                request_hash TEXT PRIMARY KEY,
                response_json TEXT,
                created NUMERIC,
                expires NUMERIC,
                context TEXT,
                scope TEXT,
                scope_key TEXT
            )
            """
        )
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp NUMERIC NOT NULL,
                model TEXT NOT NULL,
                context TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                scope_key TEXT
            )
            """
        )
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_show_preferences (
                show_id INTEGER PRIMARY KEY,
                preferences_json TEXT NOT NULL,
                updated_at NUMERIC NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_decisions (
                decision_id TEXT PRIMARY KEY,
                decision_type TEXT NOT NULL,
                timestamp NUMERIC NOT NULL,
                show_id INTEGER,
                input_summary TEXT NOT NULL,
                output_summary TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning TEXT,
                raw_response TEXT
            )
            """
        )
        self.connection.action(
            """
            CREATE TABLE IF NOT EXISTS ai_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                user_correction TEXT,
                notes TEXT,
                timestamp NUMERIC NOT NULL,
                FOREIGN KEY (decision_id) REFERENCES ai_decisions (decision_id)
            )
            """
        )

        # 2) Repair a legacy 2-column ai_show_preferences by adding the missing column.
        #    ADD COLUMN with NOT NULL requires a non-null default, which also makes the
        #    repaired column identical to a fresh canonical create.
        if not self.has_column("ai_show_preferences", "updated_at"):
            self.connection.action("ALTER TABLE ai_show_preferences ADD COLUMN updated_at NUMERIC NOT NULL DEFAULT 0")

        # 3) Ensure every canonical index exists (no-op when already present).
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache (expires)")
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_usage_timestamp ON ai_usage (timestamp)")
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_usage_scope ON ai_usage (scope_key)")
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_decisions_timestamp ON ai_decisions (timestamp)")
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_decisions_show ON ai_decisions (show_id)")
        self.connection.action("CREATE INDEX IF NOT EXISTS idx_ai_feedback_decision ON ai_feedback (decision_id)")
