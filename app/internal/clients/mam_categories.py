"""Category metadata and helper utilities for MAM searches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

AUDIO_TORZNAB = (3000, 3030)
EBOOK_TORZNAB = (7000, 7020)
COMIC_TORZNAB = (7000, 7030)
MAGAZINE_TORZNAB = (7000, 7010)
TECH_TORZNAB = (7000, 7040)


@dataclass(frozen=True)
class MamCategory:
    tracker_id: int
    name: str
    torznab_ids: tuple[int, ...]


CATEGORY_MAPPINGS: tuple[MamCategory, ...] = (
    MamCategory(13, "AudioBooks", AUDIO_TORZNAB),
    MamCategory(14, "E-Books", EBOOK_TORZNAB),
    MamCategory(15, "Musicology", AUDIO_TORZNAB),
    MamCategory(16, "Radio", AUDIO_TORZNAB),
    MamCategory(39, "Audiobooks - Action/Adventure", AUDIO_TORZNAB),
    MamCategory(49, "Audiobooks - Art", AUDIO_TORZNAB),
    MamCategory(50, "Audiobooks - Biographical", AUDIO_TORZNAB),
    MamCategory(83, "Audiobooks - Business", AUDIO_TORZNAB),
    MamCategory(51, "Audiobooks - Computer/Internet", AUDIO_TORZNAB),
    MamCategory(97, "Audiobooks - Crafts", AUDIO_TORZNAB),
    MamCategory(40, "Audiobooks - Crime/Thriller", AUDIO_TORZNAB),
    MamCategory(41, "Audiobooks - Fantasy", AUDIO_TORZNAB),
    MamCategory(106, "Audiobooks - Food", AUDIO_TORZNAB),
    MamCategory(42, "Audiobooks - General Fiction", AUDIO_TORZNAB),
    MamCategory(52, "Audiobooks - General Non-Fic", AUDIO_TORZNAB),
    MamCategory(98, "Audiobooks - Historical Fiction", AUDIO_TORZNAB),
    MamCategory(54, "Audiobooks - History", AUDIO_TORZNAB),
    MamCategory(55, "Audiobooks - Home/Garden", AUDIO_TORZNAB),
    MamCategory(43, "Audiobooks - Horror", AUDIO_TORZNAB),
    MamCategory(99, "Audiobooks - Humor", AUDIO_TORZNAB),
    MamCategory(84, "Audiobooks - Instructional", AUDIO_TORZNAB),
    MamCategory(44, "Audiobooks - Juvenile", AUDIO_TORZNAB),
    MamCategory(56, "Audiobooks - Language", AUDIO_TORZNAB),
    MamCategory(45, "Audiobooks - Literary Classics", AUDIO_TORZNAB),
    MamCategory(57, "Audiobooks - Math/Science/Tech", AUDIO_TORZNAB),
    MamCategory(85, "Audiobooks - Medical", AUDIO_TORZNAB),
    MamCategory(87, "Audiobooks - Mystery", AUDIO_TORZNAB),
    MamCategory(119, "Audiobooks - Nature", AUDIO_TORZNAB),
    MamCategory(88, "Audiobooks - Philosophy", AUDIO_TORZNAB),
    MamCategory(58, "Audiobooks - Pol/Soc/Relig", AUDIO_TORZNAB),
    MamCategory(59, "Audiobooks - Recreation", AUDIO_TORZNAB),
    MamCategory(46, "Audiobooks - Romance", AUDIO_TORZNAB),
    MamCategory(47, "Audiobooks - Science Fiction", AUDIO_TORZNAB),
    MamCategory(53, "Audiobooks - Self-Help", AUDIO_TORZNAB),
    MamCategory(89, "Audiobooks - Travel/Adventure", AUDIO_TORZNAB),
    MamCategory(100, "Audiobooks - True Crime", AUDIO_TORZNAB),
    MamCategory(108, "Audiobooks - Urban Fantasy", AUDIO_TORZNAB),
    MamCategory(48, "Audiobooks - Western", AUDIO_TORZNAB),
    MamCategory(111, "Audiobooks - Young Adult", AUDIO_TORZNAB),
    MamCategory(60, "Ebooks - Action/Adventure", EBOOK_TORZNAB),
    MamCategory(71, "Ebooks - Art", EBOOK_TORZNAB),
    MamCategory(72, "Ebooks - Biographical", EBOOK_TORZNAB),
    MamCategory(90, "Ebooks - Business", EBOOK_TORZNAB),
    MamCategory(61, "Ebooks - Comics/Graphic novels", COMIC_TORZNAB),
    MamCategory(73, "Ebooks - Computer/Internet", EBOOK_TORZNAB),
    MamCategory(101, "Ebooks - Crafts", EBOOK_TORZNAB),
    MamCategory(62, "Ebooks - Crime/Thriller", EBOOK_TORZNAB),
    MamCategory(63, "Ebooks - Fantasy", EBOOK_TORZNAB),
    MamCategory(107, "Ebooks - Food", EBOOK_TORZNAB),
    MamCategory(64, "Ebooks - General Fiction", EBOOK_TORZNAB),
    MamCategory(74, "Ebooks - General Non-Fiction", EBOOK_TORZNAB),
    MamCategory(102, "Ebooks - Historical Fiction", EBOOK_TORZNAB),
    MamCategory(76, "Ebooks - History", EBOOK_TORZNAB),
    MamCategory(77, "Ebooks - Home/Garden", EBOOK_TORZNAB),
    MamCategory(65, "Ebooks - Horror", EBOOK_TORZNAB),
    MamCategory(103, "Ebooks - Humor", EBOOK_TORZNAB),
    MamCategory(115, "Ebooks - Illusion/Magic", EBOOK_TORZNAB),
    MamCategory(91, "Ebooks - Instructional", EBOOK_TORZNAB),
    MamCategory(66, "Ebooks - Juvenile", EBOOK_TORZNAB),
    MamCategory(78, "Ebooks - Language", EBOOK_TORZNAB),
    MamCategory(67, "Ebooks - Literary Classics", EBOOK_TORZNAB),
    MamCategory(79, "Ebooks - Magazines/Newspapers", MAGAZINE_TORZNAB),
    MamCategory(80, "Ebooks - Math/Science/Tech", TECH_TORZNAB),
    MamCategory(92, "Ebooks - Medical", EBOOK_TORZNAB),
    MamCategory(118, "Ebooks - Mixed Collections", EBOOK_TORZNAB),
    MamCategory(94, "Ebooks - Mystery", EBOOK_TORZNAB),
    MamCategory(120, "Ebooks - Nature", EBOOK_TORZNAB),
    MamCategory(95, "Ebooks - Philosophy", EBOOK_TORZNAB),
    MamCategory(81, "Ebooks - Pol/Soc/Relig", EBOOK_TORZNAB),
    MamCategory(82, "Ebooks - Recreation", EBOOK_TORZNAB),
    MamCategory(68, "Ebooks - Romance", EBOOK_TORZNAB),
    MamCategory(69, "Ebooks - Science Fiction", EBOOK_TORZNAB),
    MamCategory(75, "Ebooks - Self-Help", EBOOK_TORZNAB),
    MamCategory(96, "Ebooks - Travel/Adventure", EBOOK_TORZNAB),
    MamCategory(104, "Ebooks - True Crime", EBOOK_TORZNAB),
    MamCategory(109, "Ebooks - Urban Fantasy", EBOOK_TORZNAB),
    MamCategory(70, "Ebooks - Western", EBOOK_TORZNAB),
    MamCategory(112, "Ebooks - Young Adult", EBOOK_TORZNAB),
    MamCategory(19, "Guitar/Bass Tabs", AUDIO_TORZNAB),
    MamCategory(20, "Individual Sheet", AUDIO_TORZNAB),
    MamCategory(24, "Individual Sheet MP3", AUDIO_TORZNAB),
    MamCategory(126, "Instructional Book with Video", AUDIO_TORZNAB),
    MamCategory(22, "Instructional Media - Music", AUDIO_TORZNAB),
    MamCategory(113, "Lick Library - LTP/Jam With", AUDIO_TORZNAB),
    MamCategory(114, "Lick Library - Techniques/QL", AUDIO_TORZNAB),
    MamCategory(17, "Music - Complete Editions", AUDIO_TORZNAB),
    MamCategory(26, "Music Book", AUDIO_TORZNAB),
    MamCategory(27, "Music Book MP3", AUDIO_TORZNAB),
    MamCategory(30, "Sheet Collection", AUDIO_TORZNAB),
    MamCategory(31, "Sheet Collection MP3", AUDIO_TORZNAB),
    MamCategory(127, "Radio - Comedy", AUDIO_TORZNAB),
    MamCategory(130, "Radio - Drama", AUDIO_TORZNAB),
    MamCategory(128, "Radio - Factual/Documentary", AUDIO_TORZNAB),
    MamCategory(132, "Radio - Reading", AUDIO_TORZNAB),
)

_CATEGORY_LOOKUP = {category.tracker_id: category for category in CATEGORY_MAPPINGS}
_TORZNAB_TO_TRACKER: dict[int, set[int]] = {}
for category in CATEGORY_MAPPINGS:
    for torznab_id in category.torznab_ids:
        _TORZNAB_TO_TRACKER.setdefault(torznab_id, set()).add(category.tracker_id)


def tracker_categories_for_torznab(
    torznab_categories: Sequence[int | str] | Iterable[int | str] | None,
) -> list[int]:
    """Map requested Torznab categories to tracker categories."""

    if not torznab_categories:
        return []
    tracker_ids: set[int] = set()
    for entry in torznab_categories:
        try:
            torznab_id = int(entry)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        tracker_ids.update(_TORZNAB_TO_TRACKER.get(torznab_id, ()))
    return sorted(tracker_ids)


def describe_category(tracker_id: int) -> MamCategory | None:
    """Return the metadata for a tracker category if known."""

    return _CATEGORY_LOOKUP.get(int(tracker_id))
