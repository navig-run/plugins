"""Song folders and the catalog built from them.

One song = one folder. Everything that belongs to it lives there — lyrics, the released master,
every take, stems, the beat it is on, its video, its art — and a ``README.md`` *card* whose YAML
front matter says what the song is (id, title, status, release, track, language). The catalog is
never typed by hand: it is generated from the cards, so a song cannot be filed in one place and
described in another.

    songs/albums/2016-kholodets/01-stranitsy/
        README.md                      ← the card (front matter + a generated facts block)
        ru--stranitsy.md               ← lyrics, one file per language or version
        Сенсей Монкс - Страницы.mp3    ← the release version, at the top
        takes/ stems/ beat/ video/ art/ sfx/ soundcloud/

Shared sources (an Audition session folder that holds five songs, a beat used by three) stay
whole where they are; the card points at them (``session:``, ``beat:``) instead of splitting them.
"""

from navig_audio.songs.card import (  # noqa: F401
    STATUSES,
    Song,
    SongError,
    inspect_folder,
    read_card,
    scan,
    write_card,
)
from navig_audio.songs.catalog import render_catalog, write_catalog, write_csv  # noqa: F401
