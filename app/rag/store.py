"""Index documentaire sur SQLite : métadonnées, FTS5 (BM25) et vecteurs.

Choix techniques dictés par la cible (NAS 2 Go de RAM, CPU faible) :

* un seul fichier SQLite en mode WAL, aucune base externe à faire tourner ;
* recherche lexicale native via FTS5 (`bm25()`), donc sans coût mémoire ;
* recherche sémantique par produit scalaire NumPy sur une matrice d'embeddings
  normalisés maintenue en cache et reconstruite uniquement quand l'index change ;
* fusion des deux classements par *Reciprocal Rank Fusion*, qui ne nécessite
  aucune calibration des scores.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_WORD_RE = re.compile(r"[\w\u00C0-\u024F]{2,}", re.UNICODE)
#: Constante de lissage du Reciprocal Rank Fusion (valeur usuelle de la littérature).
RRF_K = 60


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    text: str
    ordinal: int
    location: str
    real_path: str
    dsm_path: str
    share: str
    name: str
    mtime: int
    score: float = 0.0
    lexical_rank: int | None = None
    semantic_rank: int | None = None

    def to_source(self, index: int) -> dict:
        return {
            "index": index,
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "name": self.name,
            "path": self.dsm_path,
            "location": self.location,
            "score": round(self.score, 5),
            "mtime": self.mtime,
            "excerpt": self.text[:400],
        }


@dataclass
class DocumentRecord:
    real_path: str
    dsm_path: str
    share: str
    name: str
    ext: str
    size: int
    mtime: int
    content_hash: str
    status: str = "ok"
    error: str = ""


@dataclass
class Chunk:
    text: str
    ordinal: int
    location: str = ""
    embedding: np.ndarray | None = None


@dataclass
class _VectorCache:
    generation: int = -1
    ids: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    matrix: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), dtype=np.float32))


def tokenize_query(query: str) -> list[str]:
    """Extrait les termes significatifs d'une question utilisateur."""
    return [token.lower() for token in _WORD_RE.findall(query or "")]


def build_fts_query(query: str) -> str:
    """Construit une requête FTS5 sûre (les termes sont cités puis joints par OR)."""
    tokens = tokenize_query(query)[:24]
    if not tokens:
        return ""
    escaped = [token.replace('"', '""') for token in tokens]
    return " OR ".join(f'"{token}"*' for token in escaped)


def normalize_vector(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


def _escape_like(value: str) -> str:
    """Neutralise les jokers LIKE (`%` et `_`) d'un préfixe de chemin.

    Les noms de comptes DSM acceptent le tiret bas, qui remplacerait n'importe
    quel caractère dans un `LIKE` : sans échappement, `/homes/jean_dupont/`
    engloberait aussi `/homes/jeanXdupont/`.
    """
    for char in ("\\", "%", "_"):
        value = value.replace(char, "\\" + char)
    return value


def _visibility_condition(
    shares: set[str] | None, home_prefix: str = "", alias: str = "d."
) -> tuple[str, list]:
    """Condition SQL restreignant les documents à ce qu'un utilisateur peut voir.

    `shares` énumère les partages visibles ; `home_prefix` désigne le dossier
    personnel tel qu'il est *indexé* (`/homes/<compte>`), que DSM ne fait jamais
    apparaître dans la liste des partages de l'utilisateur. Retourne `("", [])`
    quand aucun filtrage n'est demandé, et `("0", [])` quand l'utilisateur ne
    peut rien voir.
    """
    if shares is None:
        return "", []
    conditions, params = [], []
    if shares:
        placeholders = ",".join("?" for _ in shares)
        conditions.append(f"LOWER({alias}share) IN ({placeholders})")
        params.extend(sorted(shares))
    if home_prefix:
        conditions.append(f"LOWER({alias}dsm_path) LIKE ? ESCAPE '\\'")
        params.append(_escape_like(home_prefix.rstrip("/").lower()) + "/%")
    if not conditions:
        return "0", []
    return f"({' OR '.join(conditions)})", params


class DocumentStore:
    """Accès thread-safe à l'index SQLite."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._vectors = _VectorCache()
        self._generation = 0
        self._configure()
        self._migrate()

    # ------------------------------------------------------------------ setup
    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            # Cache volontairement modeste : la cible dispose de peu de RAM.
            self._conn.execute("PRAGMA cache_size=-16000")

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id           INTEGER PRIMARY KEY,
                    real_path    TEXT NOT NULL UNIQUE,
                    dsm_path     TEXT NOT NULL,
                    share        TEXT NOT NULL,
                    name         TEXT NOT NULL,
                    ext          TEXT NOT NULL DEFAULT '',
                    size         INTEGER NOT NULL DEFAULT 0,
                    mtime        INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL DEFAULT '',
                    chunk_count  INTEGER NOT NULL DEFAULT 0,
                    status       TEXT NOT NULL DEFAULT 'ok',
                    error        TEXT NOT NULL DEFAULT '',
                    indexed_at   REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_documents_share ON documents(share);
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

                CREATE TABLE IF NOT EXISTS chunks (
                    id          INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    ordinal     INTEGER NOT NULL,
                    location    TEXT NOT NULL DEFAULT '',
                    text        TEXT NOT NULL,
                    embedding   BLOB,
                    dim         INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text,
                    content='chunks',
                    content_rowid='id',
                    tokenize="unicode61 remove_diacritics 2"
                );

                CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
                END;

                -- Restreint à « text » : le rattrapage des vecteurs met à jour
                -- « embedding » et « dim » sur des milliers de fragments, sans
                -- qu'il soit utile d'y réécrire l'index FTS à chaque ligne.
                -- Recréé systématiquement pour migrer les bases antérieures.
                DROP TRIGGER IF EXISTS chunks_au;
                CREATE TRIGGER chunks_au AFTER UPDATE OF text ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                """
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value))
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # -------------------------------------------------------------- écriture
    def document_fingerprint(self, real_path: str) -> tuple[int, int] | None:
        """Retourne (taille, mtime) du document déjà indexé, s'il existe."""
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime FROM documents WHERE real_path = ?", (real_path,)
            ).fetchone()
        return (int(row["size"]), int(row["mtime"])) if row else None

    def upsert_document(self, record: DocumentRecord, chunks: Sequence[Chunk]) -> int:
        """Insère ou remplace un document et l'intégralité de ses fragments."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO documents
                    (real_path, dsm_path, share, name, ext, size, mtime,
                     content_hash, chunk_count, status, error, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(real_path) DO UPDATE SET
                    dsm_path=excluded.dsm_path, share=excluded.share, name=excluded.name,
                    ext=excluded.ext, size=excluded.size, mtime=excluded.mtime,
                    content_hash=excluded.content_hash, chunk_count=excluded.chunk_count,
                    status=excluded.status, error=excluded.error, indexed_at=excluded.indexed_at
                RETURNING id
                """,
                (
                    record.real_path,
                    record.dsm_path,
                    record.share,
                    record.name,
                    record.ext,
                    record.size,
                    record.mtime,
                    record.content_hash,
                    len(chunks),
                    record.status,
                    record.error,
                    time.time(),
                ),
            )
            document_id = int(cursor.fetchone()[0])
            self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            if chunks:
                self._conn.executemany(
                    """
                    INSERT INTO chunks (document_id, ordinal, location, text, embedding, dim)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            document_id,
                            chunk.ordinal,
                            chunk.location,
                            chunk.text,
                            (
                                normalize_vector(chunk.embedding).tobytes()
                                if chunk.embedding is not None
                                else None
                            ),
                            0 if chunk.embedding is None else int(np.size(chunk.embedding)),
                        )
                        for chunk in chunks
                    ],
                )
        self._generation += 1
        return document_id

    def delete_document(self, real_path: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM documents WHERE real_path = ?", (real_path,)
            )
        self._generation += 1
        return cursor.rowcount > 0

    def delete_missing(self, seen_paths: Iterable[str], roots: Sequence[str]) -> int:
        """Supprime les documents d'un ensemble de racines qui n'existent plus."""
        seen = set(seen_paths)
        removed = 0
        with self._lock:
            rows = self._conn.execute("SELECT real_path FROM documents").fetchall()
        stale = [
            row["real_path"]
            for row in rows
            if row["real_path"] not in seen
            and any(
                row["real_path"] == root or row["real_path"].startswith(root.rstrip("/") + "/")
                for root in roots
            )
        ]
        if stale:
            with self._lock, self._conn:
                self._conn.executemany(
                    "DELETE FROM documents WHERE real_path = ?", [(path,) for path in stale]
                )
            removed = len(stale)
            self._generation += 1
        return removed

    def clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        self._generation += 1

    def optimize(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        with self._lock:
            self._conn.execute("VACUUM")

    # --------------------------------------------------------------- lecture
    def stats(self) -> dict:
        with self._lock:
            documents = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM documents "
                "WHERE status = 'ok'"
            ).fetchone()
            failed = self._conn.execute(
                "SELECT COUNT(*) AS n FROM documents WHERE status != 'ok'"
            ).fetchone()
            chunks = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(dim > 0), 0) AS vectors FROM chunks"
            ).fetchone()
            shares = self._conn.execute(
                "SELECT share, COUNT(*) AS n FROM documents WHERE status = 'ok' "
                "GROUP BY share ORDER BY n DESC"
            ).fetchall()
        return {
            "documents": int(documents["n"]),
            "documents_failed": int(failed["n"]),
            "bytes": int(documents["bytes"]),
            "chunks": int(chunks["n"]),
            "vectors": int(chunks["vectors"]),
            "shares": [{"share": row["share"], "documents": row["n"]} for row in shares],
            "db_size": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    def list_documents(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str = "",
        shares: set[str] | None = None,
        home_prefix: str = "",
    ) -> list[dict]:
        clauses, params = [], []
        if query:
            clauses.append("(name LIKE ? OR dsm_path LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        scope, scope_params = _visibility_condition(shares, home_prefix, alias="")
        if scope:
            if scope == "0":
                return []
            clauses.append(scope)
            params.extend(scope_params)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT id, name, dsm_path, share, ext, size, mtime, chunk_count, status, error,
                       indexed_at
                FROM documents {where}
                ORDER BY indexed_at DESC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_documents(
        self, *, query: str = "", shares: set[str] | None = None, home_prefix: str = ""
    ) -> dict:
        """Totaux restreints aux partages visibles, pour ne rien révéler du reste de l'index."""
        clauses, params = ["d.status = 'ok'"], []
        if query:
            clauses.append("(d.name LIKE ? OR d.dsm_path LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        scope, scope_params = _visibility_condition(shares, home_prefix)
        if scope:
            if scope == "0":
                return {"documents": 0, "chunks": 0}
            clauses.append(scope)
            params.extend(scope_params)
        where = " AND ".join(clauses)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COUNT(*) AS documents,
                       COALESCE(SUM(d.chunk_count), 0) AS chunks
                FROM documents d WHERE {where}
                """,
                params,
            ).fetchone()
        return {"documents": int(row["documents"]), "chunks": int(row["chunks"])}

    def count_chunks_without_vectors(self, dimension: int) -> int:
        """Fragments dont le vecteur manque ou n'a plus la bonne dimension."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE dim != ?", (int(dimension),)
            ).fetchone()
        return int(row["n"])

    def chunks_without_vectors(self, dimension: int, limit: int) -> list[tuple[int, str]]:
        """Lot de fragments à vectoriser, du plus ancien au plus récent."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text FROM chunks WHERE dim != ? ORDER BY id LIMIT ?",
                (int(dimension), int(limit)),
            ).fetchall()
        return [(int(row["id"]), row["text"]) for row in rows]

    def set_chunk_embeddings(self, items: Sequence[tuple[int, np.ndarray | None]]) -> int:
        """Rattache après coup leurs vecteurs à des fragments déjà indexés."""
        payload = [
            (normalize_vector(vector).tobytes(), int(np.size(vector)), chunk_id)
            for chunk_id, vector in items
            if vector is not None
        ]
        if not payload:
            return 0
        with self._lock, self._conn:
            # Seule la colonne « text » déclenche la synchronisation FTS (trigger
            # chunks_au) : ces écritures ne touchent donc pas l'index lexical.
            self._conn.executemany(
                "UPDATE chunks SET embedding = ?, dim = ? WHERE id = ?", payload
            )
        self._generation += 1
        return len(payload)

    def get_chunk_text(self, chunk_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
        return row["text"] if row else ""

    # -------------------------------------------------------------- recherche
    def _allowed_share_clause(
        self, shares: set[str] | None, home_prefix: str = ""
    ) -> tuple[str, list]:
        condition, params = _visibility_condition(shares, home_prefix)
        return (f"AND {condition}" if condition else ""), params

    def search_lexical(
        self,
        query: str,
        limit: int,
        shares: set[str] | None = None,
        home_prefix: str = "",
    ) -> list[SearchHit]:
        match = build_fts_query(query)
        if not match:
            return []
        clause, params = self._allowed_share_clause(shares, home_prefix)
        sql = f"""
            SELECT c.id AS chunk_id, c.document_id, c.text, c.ordinal, c.location,
                   d.real_path, d.dsm_path, d.share, d.name, d.mtime,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN documents d ON d.id = c.document_id
            WHERE chunks_fts MATCH ? {clause}
            ORDER BY rank
            LIMIT ?
        """
        with self._lock:
            try:
                rows = self._conn.execute(sql, [match, *params, limit]).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning("Requête FTS invalide (%s) : %s", match, exc)
                return []
        hits = []
        for rank, row in enumerate(rows):
            hit = _row_to_hit(row)
            hit.lexical_rank = rank
            hits.append(hit)
        return hits

    def _vector_matrix(self) -> _VectorCache:
        """Matrice d'embeddings en mémoire, reconstruite après chaque modification."""
        if self._vectors.generation == self._generation:
            return self._vectors
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, embedding, dim FROM chunks WHERE dim > 0 ORDER BY id"
            ).fetchall()
        if not rows:
            self._vectors = _VectorCache(generation=self._generation)
            return self._vectors
        dim = int(rows[0]["dim"])
        ids, vectors = [], []
        for row in rows:
            if int(row["dim"]) != dim or row["embedding"] is None:
                continue
            ids.append(int(row["id"]))
            vectors.append(np.frombuffer(row["embedding"], dtype=np.float32))
        if not vectors:
            self._vectors = _VectorCache(generation=self._generation)
            return self._vectors
        self._vectors = _VectorCache(
            generation=self._generation,
            ids=np.asarray(ids, dtype=np.int64),
            matrix=np.vstack(vectors).astype(np.float32, copy=False),
        )
        logger.debug("Cache vectoriel reconstruit : %d vecteurs de dimension %d", len(ids), dim)
        return self._vectors

    def search_semantic(
        self,
        embedding: np.ndarray,
        limit: int,
        shares: set[str] | None = None,
        home_prefix: str = "",
    ) -> list[SearchHit]:
        cache = self._vector_matrix()
        if cache.matrix.size == 0 or embedding is None:
            return []
        query_vector = normalize_vector(embedding)
        if query_vector.shape[0] != cache.matrix.shape[1]:
            logger.warning(
                "Dimension d'embedding incompatible (%d vs %d) : réindexation nécessaire",
                query_vector.shape[0],
                cache.matrix.shape[1],
            )
            return []
        scores = cache.matrix @ query_vector
        # Sur-échantillonnage : le filtrage par partage se fait après le calcul.
        take = min(len(scores), max(limit * 4, limit))
        top = np.argpartition(-scores, take - 1)[:take] if take < len(scores) else np.arange(len(scores))
        top = top[np.argsort(-scores[top])]
        chunk_ids = [int(cache.ids[i]) for i in top]
        score_by_id = {int(cache.ids[i]): float(scores[i]) for i in top}

        clause, params = self._allowed_share_clause(shares, home_prefix)
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.id AS chunk_id, c.document_id, c.text, c.ordinal, c.location,
                       d.real_path, d.dsm_path, d.share, d.name, d.mtime
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders}) {clause}
                """,
                [*chunk_ids, *params],
            ).fetchall()
        hits = [_row_to_hit(row) for row in rows]
        hits.sort(key=lambda hit: score_by_id.get(hit.chunk_id, 0.0), reverse=True)
        for rank, hit in enumerate(hits[:limit]):
            hit.semantic_rank = rank
            hit.score = score_by_id.get(hit.chunk_id, 0.0)
        return hits[:limit]


def _row_to_hit(row: sqlite3.Row) -> SearchHit:
    return SearchHit(
        chunk_id=int(row["chunk_id"]),
        document_id=int(row["document_id"]),
        text=row["text"],
        ordinal=int(row["ordinal"]),
        location=row["location"] or "",
        real_path=row["real_path"],
        dsm_path=row["dsm_path"],
        share=row["share"],
        name=row["name"],
        mtime=int(row["mtime"] or 0),
    )


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]], *, k: int = RRF_K
) -> list[SearchHit]:
    """Fusionne plusieurs classements sans avoir à normaliser les scores."""
    merged: dict[int, SearchHit] = {}
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            existing = merged.get(hit.chunk_id)
            if existing is None:
                merged[hit.chunk_id] = hit
            else:
                existing.lexical_rank = (
                    hit.lexical_rank if hit.lexical_rank is not None else existing.lexical_rank
                )
                existing.semantic_rank = (
                    hit.semantic_rank if hit.semantic_rank is not None else existing.semantic_rank
                )
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank + 1)
    for chunk_id, score in scores.items():
        merged[chunk_id].score = score
    return sorted(merged.values(), key=lambda hit: hit.score, reverse=True)
