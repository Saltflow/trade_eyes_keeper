"""Durable budgets, evidence revisions, publication history and delivery outbox."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .contracts import canonical, fingerprint, issue


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "research.sqlite3"
        with self.transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, stage TEXT NOT NULL,
                    screen TEXT NOT NULL DEFAULT 'unknown', errors TEXT NOT NULL DEFAULT '[]',
                    packet TEXT, input_hash TEXT, attempt_hash TEXT, updated_at TEXT NOT NULL,
                    acquired_day TEXT, research_day TEXT
                );
                CREATE TABLE IF NOT EXISTS budgets (
                    day TEXT NOT NULL, kind TEXT NOT NULL, used INTEGER NOT NULL,
                    PRIMARY KEY(day,kind)
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    day TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                    PRIMARY KEY(day,kind,key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publications (
                    version TEXT PRIMARY KEY, company_id TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    delivered TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS breaker_states (
                    company_id TEXT NOT NULL, breaker_id TEXT NOT NULL,
                    state TEXT NOT NULL, version TEXT NOT NULL,
                    PRIMARY KEY(company_id,breaker_id)
                );
                CREATE TABLE IF NOT EXISTS publication_deliveries (
                    version TEXT NOT NULL, channel TEXT NOT NULL,
                    event_id TEXT NOT NULL, delivered_at TEXT NOT NULL,
                    PRIMARY KEY(version,channel)
                );
            """
            # executescript() implicitly commits a surrounding transaction.
            # Keep schema creation and migrations under one write transaction.
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            for name, declaration in (
                ("publication_refs", "TEXT NOT NULL DEFAULT '[]'"),
                ("target_channels", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {declaration}")
            if "publication_refs" not in columns:
                # An old unversioned pending digest cannot safely be sent after
                # migration; retain its audit row without pretending delivery.
                db.execute(
                    "UPDATE outbox SET status='cancelled' WHERE status='pending' AND kind IN ('weekly','publication','correction')"
                )

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, company_id: str, kind: str, payload: dict):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO events(company_id,kind,payload,created_at) VALUES(?,?,?,?)",
                (company_id, kind, canonical(payload), utcnow()),
            )

    def register(self, company: dict):
        self.register_many([company])

    def register_many(self, companies):
        """Freeze a discovered manifest in one transaction, not N fsyncs."""
        companies = list(companies)
        if len({company["id"] for company in companies}) != len(companies):
            raise ValueError("duplicate discovered issuer")
        with self.transaction() as db:
            retained = {
                row["id"]: (row["stage"], json.loads(row["company"]))
                for row in db.execute(
                    "SELECT id,stage,company FROM companies WHERE stage='excluded' "
                    "OR json_extract(company,'$.universe_revision') IS NOT NULL"
                )
            }
            rows = []
            for incoming in companies:
                company = dict(incoming)
                previous = retained.get(company["id"])
                if previous:
                    stage, old = previous
                    if stage == "excluded":
                        if company.get("listing_status") != "active":
                            # An incomplete manifest or peer lookup cannot undo
                            # an explicit, evidence-backed removal.
                            company = old
                        else:
                            company["universe_revision"] = fingerprint(
                                {"previous_exclusion": old, "reentry": company}
                            )
                            db.execute(
                                "UPDATE companies SET stage='discovered',screen='unknown',"
                                "errors='[]',attempt_hash=NULL,updated_at=? WHERE id=?",
                                (utcnow(), company["id"]),
                            )
                            db.execute(
                                "INSERT INTO events(company_id,kind,payload,created_at) VALUES(?,'universe_reentered',?,?)",
                                (
                                    company["id"],
                                    canonical(
                                        {
                                            "universe_revision": company[
                                                "universe_revision"
                                            ],
                                            "previous_reason": old.get(
                                                "exclusion_reason"
                                            ),
                                        }
                                    ),
                                    utcnow(),
                                ),
                            )
                    elif old.get("universe_revision"):
                        company["universe_revision"] = old["universe_revision"]
                rows.append((company["id"], canonical(company), utcnow()))
            db.executemany(
                """INSERT INTO companies(id,company,stage,updated_at)
                VALUES(?,?,'discovered',?) ON CONFLICT(id) DO UPDATE SET company=excluded.company""",
                rows,
            )

    def exclude(self, tombstone: dict):
        """Apply only a positive, sourced exclusion and revoke atomically."""
        if (
            not isinstance(tombstone.get("id"), str)
            or not tombstone["id"]
            or tombstone.get("listing_status") not in {"inactive", "out_of_universe"}
            or not tombstone.get("source_refs")
            or not tombstone.get("exclusion_reason")
        ):
            raise ValueError("exclusion requires identity, status, reason and sources")
        effective = date.fromisoformat(tombstone["effective_date"])
        if effective > date.fromisoformat(tombstone["as_of"]):
            raise ValueError("exclusion effective date is in the future")
        issuer = tombstone["id"]
        reason = "Explicit universe exclusion: " + tombstone["exclusion_reason"]
        with self.transaction() as db:
            old = db.execute(
                "SELECT company,stage FROM companies WHERE id=?", (issuer,)
            ).fetchone()
            company = {**(json.loads(old["company"]) if old else {}), **tombstone}
            errors = [
                issue(
                    "universe_excluded",
                    company=issuer,
                    message=reason,
                    stage="discovery",
                )
            ]
            db.execute(
                "INSERT INTO companies(id,company,stage,screen,errors,updated_at) VALUES(?,?,'excluded','not_applicable',?,?) "
                "ON CONFLICT(id) DO UPDATE SET company=excluded.company,stage='excluded',screen='not_applicable',errors=excluded.errors,updated_at=excluded.updated_at",
                (issuer, canonical(company), canonical(errors), utcnow()),
            )
            if (
                not old
                or old["stage"] != "excluded"
                or json.loads(old["company"]) != company
            ):
                db.execute(
                    "INSERT INTO events(company_id,kind,payload,created_at) VALUES(?,'universe_excluded',?,?)",
                    (issuer, canonical(tombstone), utcnow()),
                )
            self._revoke(db, issuer, reason)

    def reserve(
        self, day: str, kind: str, limit: int, *, key: str | None = None, amount=1
    ) -> bool:
        """Reserve before side effects; a crashed attempt is never refunded."""
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
        ):
            raise ValueError(
                "budget requires nonnegative integer limit and positive integer amount"
            )
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("budget day must be ISO calendar date")
        if (
            not isinstance(kind, str)
            or not kind
            or (key is not None and not isinstance(key, str))
        ):
            raise ValueError("invalid budget identity")
        with self.transaction() as db:
            if (
                key is not None
                and db.execute(
                    "SELECT 1 FROM reservations WHERE day=? AND kind=? AND key=?",
                    (day, kind, key),
                ).fetchone()
            ):
                return False
            row = db.execute(
                "SELECT used FROM budgets WHERE day=? AND kind=?", (day, kind)
            ).fetchone()
            used = row[0] if row else 0
            if used + amount > limit:
                return False
            db.execute(
                "INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET used=excluded.used",
                (day, kind, used + amount),
            )
            if key is not None:
                db.execute("INSERT INTO reservations VALUES(?,?,?)", (day, kind, key))
            return True

    def add_usage(self, day: str, usage: dict):
        with self.transaction() as db:
            for kind in ("input_tokens", "output_tokens"):
                value = usage.get(kind)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    continue
                db.execute(
                    "INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET used=used+excluded.used",
                    (day, kind, value),
                )

    def reserve_many(
        self, day: str, limits_and_amounts: dict[str, tuple[int, int]]
    ) -> bool:
        """Reserve all model resources atomically before calling a provider.

        Each entry is ``kind: (daily_limit, amount)``. A refusal or exception
        changes no counters. Accepted reservations survive failed calls/restarts.
        Actual measured usage is recorded independently through add_usage().
        """
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("budget day must be ISO calendar date")
        if not isinstance(limits_and_amounts, dict) or not limits_and_amounts:
            raise ValueError("at least one named resource budget is required")
        for kind, pair in limits_and_amounts.items():
            if (
                not isinstance(kind, str)
                or not kind
                or not isinstance(pair, (tuple, list))
                or len(pair) != 2
            ):
                raise ValueError("budget resources require kind: (limit, amount)")
            limit, amount = pair
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit < 0
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount <= 0
            ):
                raise ValueError("invalid integer resource budget")
        with self.transaction() as db:
            updates = []
            for kind, (limit, amount) in limits_and_amounts.items():
                row = db.execute(
                    "SELECT used FROM budgets WHERE day=? AND kind=?", (day, kind)
                ).fetchone()
                used = row[0] if row else 0
                if used + amount > limit:
                    return False
                updates.append((day, kind, used + amount))
            db.executemany(
                "INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET used=excluded.used",
                updates,
            )
        return True

    def update(
        self,
        company_id: str,
        stage: str,
        *,
        errors=(),
        screen=None,
        packet=None,
        attempt_hash=None,
    ):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM companies WHERE id=?", (company_id,)
            ).fetchone()
            if not row:
                raise KeyError(company_id)
            db.execute(
                """UPDATE companies SET stage=?,errors=?,screen=?,packet=?,input_hash=?,
                attempt_hash=?,updated_at=? WHERE id=?""",
                (
                    stage,
                    canonical(list(errors)),
                    screen if screen is not None else row["screen"],
                    canonical(packet) if packet is not None else row["packet"],
                    fingerprint(packet) if packet is not None else row["input_hash"],
                    attempt_hash if attempt_hash is not None else row["attempt_hash"],
                    utcnow(),
                    company_id,
                ),
            )

    def companies(self, *, include_packet: bool = True) -> list[dict]:
        with self.transaction() as db:
            columns = (
                "*"
                if include_packet
                else (
                    "id,company,stage,screen,errors,input_hash,attempt_hash,updated_at,"
                    "acquired_day,research_day"
                )
            )
            rows = db.execute(
                f"SELECT {columns} FROM companies ORDER BY updated_at,id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field in ("company", "errors", "packet"):
                if field in item:
                    item[field] = json.loads(item[field]) if item[field] else None
            result.append(item)
        return result

    def current(self, company_id: str) -> dict | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload,status,version FROM publications WHERE company_id=? ORDER BY rowid DESC LIMIT 1",
                (company_id,),
            ).fetchone()
            revoked = None
            if row and row["status"] == "revoked":
                revoked = db.execute(
                    "SELECT payload FROM events WHERE company_id=? AND kind='revoked' ORDER BY id DESC LIMIT 1",
                    (company_id,),
                ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        if row["status"] != "published":
            payload = {
                key: payload[key]
                for key in ("as_of", "reason", "issues", "replaced_by")
                if key in payload
            }
            if revoked:
                payload["reason"] = json.loads(revoked[0]).get("reason")
        return {**payload, "status": row["status"], "version": row["version"]}

    def current_many(self) -> dict[str, dict]:
        """Read one coherent publication snapshot without a transaction per issuer."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT p.company_id,p.payload,p.status,p.version FROM publications p "
                "JOIN (SELECT company_id,MAX(rowid) AS latest FROM publications "
                "GROUP BY company_id) x ON p.rowid=x.latest"
            ).fetchall()
            revoked_rows = db.execute(
                "SELECT e.company_id,e.payload FROM events e JOIN "
                "(SELECT company_id,MAX(id) AS latest FROM events "
                "WHERE kind='revoked' GROUP BY company_id) x ON e.id=x.latest"
            ).fetchall()
        reasons = {
            row["company_id"]: json.loads(row["payload"]).get("reason")
            for row in revoked_rows
        }
        result = {}
        for row in rows:
            payload = json.loads(row["payload"])
            if row["status"] != "published":
                payload = {
                    key: payload[key]
                    for key in ("as_of", "reason", "issues", "replaced_by")
                    if key in payload
                }
                if row["status"] == "revoked" and row["company_id"] in reasons:
                    payload["reason"] = reasons[row["company_id"]]
            result[row["company_id"]] = {
                **payload,
                "status": row["status"],
                "version": row["version"],
            }
        return result

    def publication_history(self, company_id: str) -> list[dict]:
        """Explicit audit access to immutable, status-labelled prior claims."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM publications WHERE company_id=? ORDER BY rowid",
                (company_id,),
            ).fetchall()
        return [
            {
                **json.loads(row["payload"]),
                "status": row["status"],
                "version": row["version"],
            }
            for row in rows
        ]

    def publish(self, company_id: str, payload: dict) -> str:
        version = fingerprint({"company": company_id, "payload": payload})
        with self.transaction() as db:
            registry = db.execute(
                "SELECT stage FROM companies WHERE id=?", (company_id,)
            ).fetchone()
            if registry and registry["stage"] == "excluded":
                raise ValueError(
                    "excluded issuer requires explicit reentry and fresh verification"
                )
            existing = db.execute(
                "SELECT status FROM publications WHERE version=?", (version,)
            ).fetchone()
            if existing:
                if existing["status"] != "published":
                    raise ValueError(
                        "publication version was withdrawn; new evidence/review version is required"
                    )
                return version
            previous = db.execute(
                "SELECT version FROM publications WHERE company_id=? AND status='published'",
                (company_id,),
            ).fetchall()
            for row in previous:
                self._invalidate_pending(db, row["version"])
            db.execute(
                "UPDATE publications SET status='superseded' WHERE company_id=? AND status='published' AND version<>?",
                (company_id, version),
            )
            db.execute(
                "INSERT INTO publications VALUES(?,?,'published',?,?)",
                (version, company_id, canonical(payload), utcnow()),
            )
            db.execute(
                "INSERT INTO events(company_id,kind,payload,created_at) VALUES(?,'published',?,?)",
                (company_id, canonical({"version": version}), utcnow()),
            )
        return version

    def revoke(self, company_id: str, reason: str):
        with self.transaction() as db:
            self._revoke(db, company_id, reason)

    def _revoke(self, db, company_id: str, reason: str):
        rows = db.execute(
            "SELECT version FROM publications WHERE company_id=? AND status='published'",
            (company_id,),
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE publications SET status='revoked' WHERE version=?",
                (row["version"],),
            )
            payload = {
                "company_id": company_id,
                "version": row["version"],
                "reason": reason,
            }
            self._invalidate_pending(db, row["version"])
            self._enqueue_correction(db, row["version"], reason)
            db.execute(
                "INSERT INTO events(company_id,kind,payload,created_at) VALUES(?,'revoked',?,?)",
                (company_id, canonical(payload), utcnow()),
            )

    @staticmethod
    def _invalidate_pending(db, version: str):
        rows = db.execute(
            "SELECT id,publication_refs FROM outbox WHERE status='pending' AND kind IN ('weekly','publication')"
        ).fetchall()
        for row in rows:
            if version in json.loads(row["publication_refs"]):
                db.execute(
                    "UPDATE outbox SET status='cancelled' WHERE id=?", (row["id"],)
                )

    @staticmethod
    def _enqueue_correction(db, version: str, reason: str):
        channels = sorted(
            row[0]
            for row in db.execute(
                "SELECT channel FROM publication_deliveries WHERE version=?", (version,)
            )
        )
        if not channels:
            return
        publication = db.execute(
            "SELECT company_id FROM publications WHERE version=?", (version,)
        ).fetchone()
        payload = {"company_id": publication[0], "version": version, "reason": reason}
        oid = fingerprint({"kind": "correction", "version": version})
        existing = db.execute(
            "SELECT delivered FROM outbox WHERE id=?", (oid,)
        ).fetchone()
        delivered = json.loads(existing[0]) if existing else {}
        complete = all(
            delivered.get(c, {}).get("status") in {"sent", "delivered"}
            for c in channels
        )
        db.execute(
            """INSERT INTO outbox(id,kind,payload,created_at,target_channels)
            VALUES(?,'correction',?,?,?) ON CONFLICT(id) DO UPDATE SET
            target_channels=excluded.target_channels,status=?""",
            (
                oid,
                canonical(payload),
                utcnow(),
                canonical(channels),
                "delivered" if complete else "pending",
            ),
        )

    def enqueue(self, kind: str, payload: dict) -> str:
        if kind == "correction":
            raise ValueError(
                "corrections are created only from acknowledged publication revocations"
            )
        if kind == "weekly" and not payload.get("week"):
            raise ValueError("weekly notification requires a stable ISO week key")
        oid = fingerprint(
            {"kind": kind, "week": payload["week"]}
            if kind == "weekly"
            else {"kind": kind, "payload": payload}
        )
        with self.transaction() as db:
            refs = []
            if kind == "weekly":
                candidates = set(payload.get("candidates", []))
                requested = payload.get("publication_refs")
                requested_versions = (
                    {ref["company_id"]: ref["version"] for ref in requested}
                    if requested is not None
                    else None
                )
                matched = set()
                rows = db.execute(
                    "SELECT p.company_id,p.version,p.payload,c.company FROM publications p JOIN companies c ON c.id=p.company_id WHERE p.status='published'"
                ).fetchall()
                for row in rows:
                    company = json.loads(row["company"])
                    if (
                        company.get("code") in candidates
                        and json.loads(row["payload"]).get("candidate") is True
                        and (
                            requested_versions is None
                            or requested_versions.get(row["company_id"])
                            == row["version"]
                        )
                    ):
                        matched.add(company["code"])
                        refs.append(row["version"])
                payload = dict(payload, candidates=sorted(matched))
                if candidates - matched:
                    payload["excluded_unverified_candidates"] = sorted(
                        candidates - matched
                    )
            elif kind == "publication":
                version = payload.get("version")
                row = db.execute(
                    "SELECT status FROM publications WHERE version=?", (version,)
                ).fetchone()
                if not row or row[0] != "published":
                    raise ValueError("cannot enqueue an inactive publication")
                refs = [version]
            db.execute(
                "INSERT OR IGNORE INTO outbox(id,kind,payload,created_at,publication_refs) VALUES(?,?,?,?,?)",
                (oid, kind, canonical(payload), utcnow(), canonical(refs)),
            )
        return oid

    def outbox(self) -> list[dict]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        return [
            {
                **dict(r),
                "payload": json.loads(r["payload"]),
                "delivered": json.loads(r["delivered"]),
                "publication_refs": json.loads(r["publication_refs"]),
                "target_channels": json.loads(r["target_channels"])
                if r["target_channels"]
                else None,
            }
            for r in rows
        ]

    def delivery(self, oid: str, results: dict, required: list[str]):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
            if row is None:
                raise KeyError(oid)
            delivered = json.loads(row["delivered"])
            targets = (
                json.loads(row["target_channels"]) if row["target_channels"] else None
            )
            if targets is not None:
                required = targets  # Disabled channels remain pending, never falsely acknowledged.
            for channel, result in results.items():
                if channel in required and delivered.get(channel, {}).get(
                    "status"
                ) not in {"sent", "delivered"}:
                    delivered[channel] = result
                if delivered.get(channel, {}).get("status") not in {
                    "sent",
                    "delivered",
                }:
                    continue
                for version in json.loads(row["publication_refs"]):
                    db.execute(
                        "INSERT OR IGNORE INTO publication_deliveries VALUES(?,?,?,?)",
                        (version, channel, oid, utcnow()),
                    )
                    pub = db.execute(
                        "SELECT status FROM publications WHERE version=?", (version,)
                    ).fetchone()
                    if pub and pub[0] != "published":
                        # A revocation may race an in-flight transport. Its late
                        # acknowledgement must still produce one correction.
                        self._enqueue_correction(
                            db,
                            version,
                            "Publication withdrawn before delivery acknowledgement",
                        )
            complete = bool(required) and all(
                delivered.get(c, {}).get("status") in {"sent", "delivered"}
                for c in required
            )
            status = (
                row["status"]
                if row["status"] == "cancelled"
                else ("delivered" if complete else "pending")
            )
            db.execute(
                "UPDATE outbox SET delivered=?,status=? WHERE id=?",
                (canonical(delivered), status, oid),
            )

    def budgets(self, day: str) -> dict:
        with self.transaction() as db:
            return dict(
                db.execute(
                    "SELECT kind,used FROM budgets WHERE day=?", (day,)
                ).fetchall()
            )

    def events(
        self, limit=10000, *, kind: str | None = None, company_id: str | None = None
    ) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("event limit must be a positive integer")
        filters, arguments = [], []
        if kind is not None:
            filters.append("kind=?")
            arguments.append(kind)
        if company_id is not None:
            filters.append("company_id=?")
            arguments.append(company_id)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?",
                (*arguments, limit),
            ).fetchall()
        return [
            {**dict(r), "payload": json.loads(r["payload"])} for r in reversed(rows)
        ]

    def breaker(self, company_id: str, bid: str, state: str, version: str) -> bool:
        with self.transaction() as db:
            row = db.execute(
                "SELECT state FROM breaker_states WHERE company_id=? AND breaker_id=?",
                (company_id, bid),
            ).fetchone()
            changed = (row is None and state == "triggered") or (
                row is not None and row[0] != state
            )
            db.execute(
                "INSERT INTO breaker_states VALUES(?,?,?,?) ON CONFLICT(company_id,breaker_id) DO UPDATE SET state=excluded.state,version=excluded.version",
                (company_id, bid, state, version),
            )
        return changed
