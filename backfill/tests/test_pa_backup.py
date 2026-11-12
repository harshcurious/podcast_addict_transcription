import json
import sqlite3
import zipfile

from pa_backup import database_path, listened_episodes, parse_transcript_urls

FEED = "https://example.com/feed.xml"


def make_db(path, *, history=True):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE podcasts (_id INTEGER PRIMARY KEY, name TEXT, custom_name TEXT,
                               feed_url TEXT, language TEXT);
        CREATE TABLE episodes (_id INTEGER PRIMARY KEY, name TEXT, podcast_id INTEGER,
            guid TEXT, url TEXT, download_url TEXT, publication_date INTEGER,
            playbackDate INTEGER, duration_ms INTEGER, transcript_url TEXT,
            description TEXT, short_description TEXT, seen_status INTEGER);
    """)
    conn.execute("INSERT INTO podcasts VALUES (1, 'Show', '', ?, 'English')", (FEED,))
    urls = json.dumps({"urls": [{"type": "vtt", "url": "https://example.com/1.vtt"}]})
    rows = [  # id, name, guid, playbackDate, transcript_url, seen
        (1, "Played early", "g1", 1_700_000_000_000, urls, 1),
        (2, "Played late", "g2", 1_800_000_000_000, "", 1),
        (3, "Never played", "g3", -1, "", 0),
        (4, "Completed, not marked", "g4", 1_750_000_000_000, "", 0),
    ]
    for _id, name, guid, played, turl, seen in rows:
        conn.execute("INSERT INTO episodes VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?)",
                     (_id, name, guid, f"https://example.com/{_id}",
                      f"https://cdn.example.com/{_id}.mp3", 1_690_000_000_000, played,
                      1_800_000, turl, "<p>About</p>", "", seen))
    if history:
        conn.execute("CREATE TABLE playback_listening_history (episode_id INTEGER, "
                     "completed INTEGER)")
        conn.execute("INSERT INTO playback_listening_history VALUES (4, 1)")
    conn.commit()
    conn.close()


def test_listened_means_marked_played_or_completed_in_history_newest_first(tmp_path):
    make_db(tmp_path / "podcastAddict.db")
    eps = listened_episodes(tmp_path / "podcastAddict.db")
    assert [e.title for e in eps] == ["Played late", "Completed, not marked", "Played early"]
    early = eps[-1]
    assert early.transcript_urls == (("vtt", "https://example.com/1.vtt"),)
    assert early.duration_seconds == 1800
    assert early.published_at == "2023-07-22"
    assert early.feed_url == FEED and early.language == "English"


def test_backups_without_listening_history_still_work(tmp_path):
    make_db(tmp_path / "podcastAddict.db", history=False)
    assert len(listened_episodes(tmp_path / "podcastAddict.db")) == 2


def test_episode_key_is_stable_and_distinct(tmp_path):
    make_db(tmp_path / "podcastAddict.db")
    first = {e.title: e.key for e in listened_episodes(tmp_path / "podcastAddict.db")}
    again = {e.title: e.key for e in listened_episodes(tmp_path / "podcastAddict.db")}
    assert first == again and len(set(first.values())) == 3


def test_backup_zip_is_extracted_once(tmp_path):
    make_db(tmp_path / "podcastAddict.db")
    backup = tmp_path / "PodcastAddict_x.backup"
    with zipfile.ZipFile(backup, "w") as z:
        z.write(tmp_path / "podcastAddict.db", "podcastAddict.db")
        z.writestr("com.bambuna.podcastaddict_preferences.xml", "<map/>")
    db = database_path(backup, tmp_path / "work")
    assert db.parent == tmp_path / "work" and db.exists()
    assert database_path(backup, tmp_path / "work") == db
    assert len(listened_episodes(db)) == 3


def test_transcript_url_formats():
    assert parse_transcript_urls("") == ()
    assert parse_transcript_urls("https://x/t.srt") == (("", "https://x/t.srt"),)
    assert parse_transcript_urls("{broken") == ()
