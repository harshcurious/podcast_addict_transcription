"""Listened Episodes, read from a Podcast Addict backup.

A `.backup` file is a zip holding `podcastAddict.db` (SQLite) and a preferences XML. An
Episode counts as listened when Podcast Addict marked it played (`episodes.seen_status = 1`)
or its listening history records a completed play. Standard library only.
"""
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass

DB_MEMBER = "podcastAddict.db"


@dataclass(frozen=True)
class ListenedEpisode:
    episode_id: int
    podcast: str
    feed_url: str
    language: str
    title: str
    guid: str
    episode_url: str
    audio_url: str
    published_at: str
    played_at: str
    duration_seconds: float
    transcript_urls: tuple  # ((type, url), ...) as Podcast Addict stored them
    description: str

    @property
    def key(self):
        """Stable across backups: the feed plus the publisher's GUID for the item."""
        ident = f"{self.feed_url}\n{self.guid or self.audio_url or self.title}"
        return hashlib.sha256(ident.encode()).hexdigest()[:24]


def database_path(backup, workdir):
    """Return a SQLite path for `backup`, extracting the zip's database once into `workdir`."""
    given = backup
    backup = pathlib.Path(backup).expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(f"backup not found: {given} (looked for {backup})")
    if not zipfile.is_zipfile(backup):
        with backup.open("rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                raise ValueError(f"{backup} is neither a Podcast Addict backup (zip) "
                                 "nor a SQLite database")
        return backup
    stat = backup.stat()
    target = pathlib.Path(workdir) / f"{backup.stem}-{int(stat.st_mtime)}-{stat.st_size}.db"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup) as archive:
        names = [n for n in archive.namelist() if pathlib.PurePosixPath(n).name == DB_MEMBER]
        if not names:
            raise ValueError(f"{backup} contains no {DB_MEMBER}")
        tmp = target.with_suffix(".tmp")
        with archive.open(names[0]) as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        tmp.replace(target)
    return target


def _iso_from_ms(value, date_only=False):
    if not value or value <= 0:
        return ""
    stamp = dt.datetime.fromtimestamp(value / 1000, dt.UTC)
    return stamp.date().isoformat() if date_only else stamp.isoformat(timespec="seconds")


def parse_transcript_urls(raw):
    """Podcast Addict stores `{"urls":[{"type":..,"url":..}]}`; older rows hold a bare URL."""
    raw = (raw or "").strip()
    if not raw:
        return ()
    if raw.startswith("{"):
        try:
            urls = json.loads(raw).get("urls") or []
            return tuple((str(u.get("type") or ""), str(u["url"]))
                         for u in urls if isinstance(u, dict) and u.get("url"))
        except (ValueError, AttributeError):
            return ()
    return (("", raw),) if raw.startswith("http") else ()


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def listened_episodes(db_path):
    """Every listened Episode, most recently played first."""
    conn = sqlite3.connect(f"file:{pathlib.Path(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda raw: raw.decode("utf-8", "replace")  # feeds hold bad UTF-8
    try:
        completed = ("OR e._id IN (SELECT episode_id FROM playback_listening_history "
                     "WHERE completed = 1)"
                     if _has_table(conn, "playback_listening_history") else "")
        rows = conn.execute(f"""
            SELECT e._id, COALESCE(NULLIF(p.custom_name, ''), p.name) AS podcast,
                   p.feed_url, p.language, e.name, e.guid, e.url, e.download_url,
                   e.publication_date, e.playbackDate, e.duration_ms, e.transcript_url,
                   COALESCE(NULLIF(e.description, ''), e.short_description, '') AS description
            FROM episodes e JOIN podcasts p ON p._id = e.podcast_id
            WHERE e.seen_status = 1 {completed}
            ORDER BY e.playbackDate DESC, e._id DESC""").fetchall()
    finally:
        conn.close()
    return [ListenedEpisode(
        episode_id=r["_id"], podcast=r["podcast"] or "", feed_url=r["feed_url"] or "",
        language=r["language"] or "", title=r["name"] or "", guid=(r["guid"] or "").strip(),
        episode_url=r["url"] or "", audio_url=r["download_url"] or "",
        published_at=_iso_from_ms(r["publication_date"], date_only=True),
        played_at=_iso_from_ms(r["playbackDate"]),
        duration_seconds=max(0, r["duration_ms"] or 0) / 1000,
        transcript_urls=parse_transcript_urls(r["transcript_url"]),
        description=r["description"] or "",
    ) for r in rows]
