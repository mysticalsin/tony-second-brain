#!/usr/bin/env python3
"""recall_daemon.py — warm local retrieval daemon for Ultron (B1 foundation).

Keeps fastembed + QdrantClient resident so retrieval is ~10-40 ms instead of
~600 ms (uv cold-start + model reload per call).

Usage (via launchd or by hand):
    uv run --with qdrant-client --with fastembed python recall_daemon.py

Port  : 127.0.0.1:7766 (constant)
Owner : this daemon is the SOLE owner of ~/AI-Brain-build/qdrant while running.
        The indexer (build_recall_index.py) must not run concurrently;
        use POST /reindex which closes + reopens the client around the indexer.

Endpoints:
    GET  /health                 → 200 {"ok":true,"points":<n>,"model":"..."}
    GET  /retrieve?q=<text>&top=6 → 200 [{path,score,snippet,title,source_type}]
    POST /reindex                → 200 {"reindexed":true,"points":<n>}
                                   (closes client, runs recall-index.sh, reopens)

Fallback safety: if this daemon is not running, main.js falls back to the
existing `uv run recall_vector.py` path — zero behaviour change.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock

# ── constants ────────────────────────────────────────────────────────────────
PORT            = 7766
HOST            = "127.0.0.1"
QDRANT_PATH     = Path.home() / "AI-Brain-build" / "qdrant"
COLLECTION      = "recall"
VECTOR_NAME     = "fast-all-minilm-l6-v2"
MODEL_NAME      = "sentence-transformers/all-MiniLM-L6-v2"
# ULT-G2 (2026-06-09): the daemon must NOT run recall-index.sh — its rsync child
# inherits this uv-python process's TCC-denied CloudStorage context and dies rc=23
# (logged hourly for days). brain-refresh.sh (Apple bash, TCC-granted) refreshes the
# staging mirror BEFORE POSTing /reindex; we only run the indexer against staging.
REINDEX_UV      = Path.home() / ".local" / "bin" / "uv"
REINDEX_TOOL    = Path.home() / "AI-Brain-build" / "build" / "tools" / "build_recall_index.py"
REINDEX_STAGING = Path.home() / "AI-Brain-build" / "recall-staging"
REINDEX_TIMEOUT = 1800  # first post-alignment run re-embeds ~1300 never-indexed files
LOG_DIR         = Path.home() / ".cache" / "ai-brain"
LOG_FILE        = LOG_DIR / "recall-daemon.log"

# ── logging ──────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [recall-daemon] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("recall-daemon")


# ── model + client state (global, protected by _lock) ───────────────────────
_lock    = Lock()          # serialise retrieve/reindex
_model   = None            # fastembed.TextEmbedding (loaded once)
_client  = None            # QdrantClient (may be None during reindex)
_points  = 0               # last-known collection count


def _open_client():
    """Open QdrantClient; return (client, point_count)."""
    from qdrant_client import QdrantClient
    QDRANT_PATH.mkdir(parents=True, exist_ok=True)
    c = QdrantClient(path=str(QDRANT_PATH))
    n = c.count(COLLECTION).count if c.collection_exists(COLLECTION) else 0
    return c, n


def _load_model():
    """Load fastembed model from a PERSISTENT cache. The default cache lives in the
    macOS temp dir (/var/folders/.../T/fastembed_cache) which the OS purges — a partial
    purge (snapshot dir left, model.onnx gone) crash-looped the daemon on 2026-06-09."""
    from fastembed import TextEmbedding
    cache = Path.home() / "AI-Brain-build" / "fastembed-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(MODEL_NAME, cache_dir=str(cache))


def startup():
    """Warm the model and open the DB.  Called once before the server loop."""
    global _model, _client, _points
    log.info("warming fastembed model %s …", MODEL_NAME)
    t0 = time.monotonic()
    _model = _load_model()
    # Force model load by embedding a throwaway string
    list(_model.query_embed(["warm"]))
    log.info("model warm in %.1f s", time.monotonic() - t0)

    log.info("opening qdrant at %s …", QDRANT_PATH)
    _client, _points = _open_client()
    log.info("qdrant open — collection '%s' has %d points", COLLECTION, _points)


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    # Silence the default per-request access log (daemon runs 24/7; INFO is enough)
    def log_message(self, fmt, *args):  # noqa: N802
        pass

    def _send(self, code: int, body: dict | list):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, msg: str, code: int = 500):
        log.warning("request error %d: %s", code, msg)
        self._send(code, {"ok": False, "error": msg})

    # ── GET ─────────────────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._handle_health()
            elif parsed.path == "/retrieve":
                self._handle_retrieve(parsed.query)
            else:
                self._err("not found", 404)
        except Exception as exc:  # pragma: no cover
            self._err(f"internal error: {exc}")

    def _handle_health(self):
        global _points
        try:
            with _lock:
                if _client is None:
                    self._send(503, {"ok": False, "error": "client closed (reindex in progress)"})
                    return
                _points = _client.count(COLLECTION).count if _client.collection_exists(COLLECTION) else 0
            self._send(200, {"ok": True, "points": _points, "model": MODEL_NAME})
        except Exception as exc:
            self._err(f"health check failed: {exc}")

    def _handle_retrieve(self, query_string: str):
        global _points
        params = urllib.parse.parse_qs(query_string)
        q = params.get("q", [""])[0].strip()
        if not q:
            self._err("missing ?q=", 400)
            return
        try:
            top = int(params.get("top", ["6"])[0])
        except ValueError:
            top = 6

        try:
            t0 = time.monotonic()
            with _lock:
                if _client is None:
                    self._err("service unavailable — reindex in progress", 503)
                    return
                if _model is None:
                    self._err("model not loaded", 503)
                    return

                q_vec = list(_model.query_embed([q]))[0].tolist()
                hits = _client.query_points(
                    collection_name=COLLECTION,
                    query=q_vec,
                    using=VECTOR_NAME,
                    limit=top,
                    with_payload=True,
                ).points

            results = []
            for h in hits:
                meta = (h.payload or {}).get("metadata") or {}
                results.append({
                    "path":        meta.get("path", ""),
                    "score":       round(float(h.score), 4),
                    "snippet":     meta.get("snippet", ""),
                    "title":       meta.get("title", ""),
                    "source_type": meta.get("source_type", ""),
                })
            elapsed_ms = (time.monotonic() - t0) * 1000
            log.info("retrieve q=%r top=%d → %d hits in %.0f ms", q[:60], top, len(results), elapsed_ms)
            self._send(200, results)
        except Exception as exc:
            self._err(f"retrieve failed: {exc}")

    # ── POST ────────────────────────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/reindex":
                self._handle_reindex()
            else:
                self._err("not found", 404)
        except Exception as exc:
            self._err(f"internal error: {exc}")

    def _handle_reindex(self):
        """Close the qdrant client (release lock), run the indexer, reopen."""
        global _client, _points

        if not REINDEX_TOOL.exists():
            self._err(f"indexer not found: {REINDEX_TOOL}", 500)
            return
        if not REINDEX_STAGING.exists():
            self._err(f"staging mirror missing: {REINDEX_STAGING} — run recall-index.sh --stage-only first", 500)
            return

        log.info("reindex requested — closing qdrant client …")
        try:
            with _lock:
                if _client is not None:
                    _client.close()
                    _client = None

            t0 = time.monotonic()
            env = dict(os.environ, CLAUDE_VAULT=str(REINDEX_STAGING))
            result = subprocess.run(
                [str(REINDEX_UV), "run", "--quiet", "--with", "qdrant-client",
                 "--with", "fastembed", "python", str(REINDEX_TOOL), "--once-if-free"],
                capture_output=True, text=True, timeout=REINDEX_TIMEOUT, env=env,
            )
            elapsed = time.monotonic() - t0
            if result.returncode != 0:
                log.warning("reindex script exited %d after %.0f s\nstdout: %s\nstderr: %s",
                            result.returncode, elapsed, result.stdout[-500:], result.stderr[-500:])

            log.info("reindex script finished in %.0f s (rc=%d) — reopening client …",
                     elapsed, result.returncode)

            with _lock:
                _client, _points = _open_client()

            log.info("qdrant reopened — %d points", _points)
            self._send(200, {"reindexed": True, "points": _points,
                             "elapsed_s": round(elapsed, 1),
                             "rc": result.returncode})
        except subprocess.TimeoutExpired:
            log.error("reindex timed out — reopening client")
            with _lock:
                if _client is None:
                    try:
                        _client, _points = _open_client()
                    except Exception as exc2:
                        log.error("failed to reopen client after timeout: %s", exc2)
            self._err(f"reindex timed out ({REINDEX_TIMEOUT} s)", 504)
        except Exception as exc:
            # Always try to reopen the client so retrieval can recover
            with _lock:
                if _client is None:
                    try:
                        _client, _points = _open_client()
                    except Exception as exc2:
                        log.error("failed to reopen client: %s", exc2)
            self._err(f"reindex failed: {exc}")


# ── server lifecycle ─────────────────────────────────────────────────────────

_server: HTTPServer | None = None


def _shutdown(signum, frame):  # noqa: ARG001
    log.info("SIGTERM received — shutting down …")
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            _client = None
            log.info("qdrant client closed (lock released)")
    if _server:
        # Stop the serve_forever() loop from another thread context
        _server.shutdown()
    sys.exit(0)


def main():
    global _server
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    log.info("recall-daemon starting — pid=%d port=%d", os.getpid(), PORT)
    startup()

    _server = HTTPServer((HOST, PORT), Handler)
    log.info("listening on %s:%d", HOST, PORT)
    try:
        _server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        with _lock:
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
        log.info("recall-daemon stopped")


if __name__ == "__main__":
    main()
