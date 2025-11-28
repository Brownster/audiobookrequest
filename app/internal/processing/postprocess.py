from __future__ import annotations

import asyncio
import os
import shutil
import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

from aiohttp import ClientSession

from app.util.log import logger
from app.internal.models import BookRequest

AUDIO_EXTENSIONS = {".mp3", ".m4b", ".m4a", ".flac", ".aac", ".ogg", ".wav", ".opus"}


class PostProcessingError(RuntimeError):
    pass


def _sanitize_component(name: str, fallback: str = "Unknown") -> str:
    # Keep letters/numbers/spaces/dashes/apostrophes, strip the rest, collapse spaces
    cleaned = re.sub(r"[^A-Za-z0-9\s\-\']", "", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback


class PostProcessor:
    def __init__(
        self,
        output_dir: Path,
        tmp_dir: Path,
        enable_merge: bool = True,
        http_session: Optional[ClientSession] = None,
    ):
        self.output_dir = output_dir
        self.tmp_dir = tmp_dir
        self.enable_merge = enable_merge
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_path = shutil.which("ffmpeg")
        self.http_session = http_session

    async def process(self, job_id: str, request: BookRequest, torrent_snapshot: dict) -> Path:
        """
        Process a completed download.
        
        Args:
            job_id: The ID of the download job (used for unique naming if needed).
            request: The BookRequest containing metadata.
            torrent_snapshot: The torrent info from the client (files, downloadDir, etc).
        """
        download_dir = Path(torrent_snapshot.get("downloadDir", ""))
        metadata = self._extract_metadata(request)
        if not download_dir.exists() and download_dir.parent.exists():
            download_dir = download_dir.parent
            logger.debug("PostProcessor: adjusted download_dir to parent", base=str(download_dir))
        
        # Determine source path
        # torrent_snapshot['name'] is usually the folder name or file name
        name = torrent_snapshot.get("name") or request.title
        source_path = download_dir / name

        if not source_path.exists():
            source_path = self._find_source_fallback(download_dir, name, torrent_snapshot.get("files", []))
            if not source_path or not source_path.exists():
                raise PostProcessingError(
                    f"Source path does not exist: {download_dir / name}. If this path lives on a remote seedbox, set qB Remote Download Path and Local Path Prefix in Settings ▸ MAM so it can be mapped locally."
                )

        files = torrent_snapshot.get("files", [])
        audio_files = self._gather_audio_files(download_dir, files)
        if not audio_files and download_dir.exists():
            audio_files = self._find_audio_files_recursive(download_dir)
        
        authors = metadata.get("authors") or ["Unknown Author"]
        primary_author = authors[0]
        safe_author = _sanitize_component(primary_author, "Unknown Author")
        safe_title = _sanitize_component(metadata.get("title") or name, "Audiobook")
        base_dir = self.output_dir / safe_author / safe_title
        base_dir.mkdir(parents=True, exist_ok=True)
        destination = base_dir / f"{safe_title}"
        if destination.exists():
            destination = base_dir / f"{safe_title}_{str(job_id)[:8]}"

        if not audio_files:
            # no audio metadata, copy entire folder/file
            await asyncio.to_thread(self._copy_any, source_path, destination)
            await self._cleanup_tmp()
            return destination

        if len(audio_files) == 1:
            destination = destination.with_suffix(audio_files[0].suffix.lower())
            await asyncio.to_thread(self._copy_file, audio_files[0], destination)
            await self._finalize_metadata(destination, metadata)
            await self._cleanup_tmp()
            return destination

        if self.enable_merge and self.ffmpeg_path:
            # If all inputs are mp3, keep mp3 container to avoid invalid mp3-in-m4b
            all_mp3 = all(p.suffix.lower() == ".mp3" for p in audio_files)
            merged_ext = ".mp3" if all_mp3 else ".m4b"
            merged = destination.with_suffix(merged_ext)
            merged.parent.mkdir(parents=True, exist_ok=True)
            # Filter out cover/art files (commonly mp3+jpg pairs)
            audio_only = [p for p in audio_files if p.suffix.lower() in AUDIO_EXTENSIONS and p.suffix.lower() != ".jpg"]
            await self._merge_with_ffmpeg(audio_only or audio_files, merged)
            await self._finalize_metadata(merged, metadata)
            await self._cleanup_tmp()
            return merged

        # fallback: copy directory containing files
        await asyncio.to_thread(self._copy_any, source_path, destination)
        await self._finalize_metadata(destination, metadata)
        await self._cleanup_tmp()
        return destination


class EbookPostProcessor:
    """Lightweight processor for ebook downloads (no audio merge)."""

    PREFERRED_EXTS = [".epub", ".mobi", ".azw3", ".pdf", ".txt"]

    def __init__(
        self,
        output_dir: Path,
        tmp_dir: Path,
        http_session: Optional[ClientSession] = None,
    ):
        self.output_dir = output_dir
        self.tmp_dir = tmp_dir
        self.http_session = http_session
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    async def process(self, job_id: str, request: BookRequest, torrent_snapshot: dict) -> Path:
        download_dir = Path(torrent_snapshot.get("downloadDir", ""))
        if not download_dir.exists() and download_dir.parent.exists():
            download_dir = download_dir.parent

        files = torrent_snapshot.get("files", []) or []
        candidate = self._find_best_file(download_dir, files)
        if not candidate:
            raise PostProcessingError("No ebook file found to process.")

        authors = request.authors or ["Unknown Author"]
        safe_author = _sanitize_component(authors[0], "Unknown Author")
        safe_title = _sanitize_component(request.title, "Book")
        dest_dir = self.output_dir / safe_author / safe_title
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_path = dest_dir / f"{safe_title}{candidate.suffix.lower()}"
        if dest_path.exists():
            dest_path = dest_dir / f"{safe_title}_{str(job_id)[:8]}{candidate.suffix.lower()}"

        await asyncio.to_thread(shutil.copy2, candidate, dest_path)
        await self._write_metadata(dest_dir, request)

        cover_path = await self._download_cover(request.cover_image)
        if cover_path:
            final_cover = dest_dir / "cover.jpg"
            cover_path.replace(final_cover)

        return dest_path

    def _find_best_file(self, download_dir: Path, files: list[dict]) -> Optional[Path]:
        # Prefer torrent-reported file list first
        ordered: list[Path] = []
        for entry in files:
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            path = download_dir / name
            ordered.append(path)

        if download_dir.exists():
            for ext in self.PREFERRED_EXTS:
                matches = list(download_dir.rglob(f"*{ext}"))
                if matches:
                    ordered.extend(matches)

        for ext in self.PREFERRED_EXTS:
            for candidate in ordered:
                if candidate.suffix.lower() == ext and candidate.exists():
                    return candidate
        return None

    async def _write_metadata(self, dest_dir: Path, request: BookRequest) -> None:
        payload = {
            "title": request.title,
            "authors": request.authors,
            "narrators": request.narrators,
            "asin": request.asin,
            "publishDate": request.release_date.isoformat() if request.release_date else None,
            "cover": request.cover_image,
        }
        meta_path = dest_dir / "metadata.json"
        meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    async def _download_cover(self, url: Optional[str]) -> Optional[Path]:
        if not url or not self.http_session:
            return None
        try:
            async with self.http_session.get(url) as resp:
                if not resp.ok:
                    return None
                data = await resp.read()
        except Exception as exc:
            logger.debug("PostProcessor: cover fetch failed", error=str(exc))
            return None
        cover_path = self.tmp_dir / f"cover_{os.getpid()}.jpg"
        cover_path.write_bytes(data)
        return cover_path
    def _normalize(self, value: str) -> str:
        return "".join(c for c in value.lower() if c.isalnum())

    def _find_source_fallback(self, download_dir: Path, name: str, files: list[dict]) -> Optional[Path]:
        """
        Attempt to locate the real source folder/file when the reported torrent name
        doesn't match the actual path (e.g., punctuation differences).
        """
        # Try using the first file entry to derive the parent
        for f in files or []:
            rel = f.get("name")
            if not isinstance(rel, str):
                continue
            parts = Path(rel).parts
            if parts:
                candidate = download_dir / parts[0]
                if candidate.exists():
                    return candidate
        # Try fuzzy match on directory names
        target = self._normalize(name)
        try:
            for entry in download_dir.iterdir():
                if self._normalize(entry.name) == target:
                    return entry
        except FileNotFoundError:
            return None
        return None

    def _gather_audio_files(self, base_dir: Path, files: Iterable[dict]) -> List[Path]:
        audio_paths: List[Path] = []
        for f in files:
            name = f.get("name")
            if not isinstance(name, str):
                continue
            # 'name' in files list is relative to the download dir (usually includes the torrent name folder)
            # but sometimes it's just the file name if single file torrent.
            # We construct full path.
            path = base_dir / name
            if path.suffix.lower() in AUDIO_EXTENSIONS and path.exists():
                audio_paths.append(path)
        audio_paths.sort()
        return audio_paths

    def _find_audio_files_recursive(self, base_dir: Path) -> List[Path]:
        found: List[Path] = []
        for ext in AUDIO_EXTENSIONS:
            found.extend(base_dir.rglob(f"*{ext}"))
        found = [p for p in found if p.is_file()]
        found.sort()
        return found

    async def _merge_with_ffmpeg(self, files: List[Path], destination: Path) -> None:
        list_file_path = self.tmp_dir / f"ffmpeg_concat_{os.getpid()}_{destination.stem}.txt"
        with list_file_path.open("w", encoding="utf-8") as fh:
            for file in files:
                # ffmpeg concat requires escaping single quotes
                safe_path = file.as_posix().replace("'", r"'\''")
                fh.write(f"file '{safe_path}'\n")

        # Choose codec/container based on output suffix
        is_mp3_out = destination.suffix.lower() == ".mp3"
        cmd = [
            self.ffmpeg_path,
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file_path),
            "-map",
            "0:a:0",
        ]
        if is_mp3_out:
            cmd += ["-c:a", "copy", "-vn", "-f", "mp3"]
        else:
            cmd += ["-c", "copy"]
        cmd.append(str(destination))
        logger.info("PostProcessor: merging audio with ffmpeg", output=str(destination))
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        list_file_path.unlink(missing_ok=True)
        if process.returncode != 0:
            raise PostProcessingError(
                f"ffmpeg failed ({process.returncode}): {stderr.decode() or stdout.decode()}"
            )

    def _copy_any(self, source: Path, destination: Path) -> None:
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def _copy_file(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    async def _finalize_metadata(self, destination: Path, metadata: dict) -> None:
        await self._write_metadata_file(destination, metadata)
        if destination.is_file():
            await self._apply_audio_metadata(destination, metadata)

    def _extract_metadata(self, request: BookRequest) -> dict:
        title = request.title or "Untitled"
        authors = request.authors or ["Unknown Author"]
        narrators = request.narrators or []
        primary_author = authors[0] if authors else ""
        display_name = f"{primary_author} - {title}" if primary_author else title

        ffmpeg_tags = {
            "title": title,
            "album": title,
            "artist": ", ".join(narrators or authors),
            "album_artist": primary_author or ", ".join(authors),
            "composer": ", ".join(narrators) if narrators else None,
        }

        return {
            "title": title,
            "authors": authors,
            "narrators": narrators,
            "asin": request.asin,
            "cover_url": request.cover_image,
            "publish_date": request.release_date.isoformat() if request.release_date else None,
            "ffmpeg_tags": ffmpeg_tags,
            "display_name": display_name,
        }

    async def _write_metadata_file(self, destination: Path, metadata: dict) -> None:
        if not metadata.get("title"):
            return
        payload = {
            "title": metadata.get("title"),
            "authors": metadata.get("authors"),
            "narrators": metadata.get("narrators"),
            "asin": metadata.get("asin"),
            "publishDate": metadata.get("publish_date"),
            "cover": metadata.get("cover_url"),
        }
        if destination.is_dir():
            meta_path = destination / "metadata.json"
        else:
            meta_path = destination.with_suffix(destination.suffix + ".metadata.json")
        meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    async def _apply_audio_metadata(self, file_path: Path, metadata: dict) -> None:
        if not self.ffmpeg_path or not file_path.exists():
            return
        tags = metadata.get("ffmpeg_tags") or {}
        cover_path = await self._download_cover(metadata.get("cover_url"))
        ext = file_path.suffix
        temp_path = file_path.with_name(file_path.stem + ".tmp" + ext)
        cmd = [self.ffmpeg_path, "-y", "-i", str(file_path)]
        if cover_path:
            cmd += [
                "-i",
                str(cover_path),
                "-map",
                "0",
                "-map",
                "1",
                "-c",
                "copy",
                "-metadata:s:v",
                "title=Cover",
                "-metadata:s:v",
                "comment=Cover (front)",
                "-disposition:v",
                "attached_pic",
            ]
        else:
            cmd += ["-c", "copy"]
        for key, value in tags.items():
            if value:
                cmd += ["-metadata", f"{key}={value}"]
        cmd.append(str(temp_path))
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if cover_path:
            cover_path.unlink(missing_ok=True)
        if process.returncode != 0:
            temp_path.unlink(missing_ok=True)
            logger.error(
                "PostProcessor: failed to apply metadata",
                error=stderr.decode() or stdout.decode(),
            )
            return
        file_path.unlink(missing_ok=True)
        temp_path.rename(file_path)

    async def _download_cover(self, url: Optional[str]) -> Optional[Path]:
        if not url or not self.http_session:
            return None
        try:
            async with self.http_session.get(url) as resp:
                if not resp.ok:
                    return None
                data = await resp.read()
        except Exception as exc:
            logger.debug("PostProcessor: cover fetch failed", error=str(exc))
            return None
        cover_path = self.tmp_dir / f"cover_{os.getpid()}.jpg"
        cover_path.write_bytes(data)
        return cover_path

async def _cleanup_tmp(self) -> None:
    try:
        for p in self.tmp_dir.glob("ffmpeg_concat_*"):
            p.unlink(missing_ok=True)
        for p in self.tmp_dir.glob("cover_*"):
            p.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("PostProcessor: tmp cleanup skipped", error=str(exc))
        

# Fallback: bind _extract_metadata to the class if missing (defensive against reload issues)
def _pp_extract_metadata(self, request: BookRequest) -> dict:
    title = request.title or "Untitled"
    authors = request.authors or ["Unknown Author"]
    narrators = request.narrators or []
    primary_author = authors[0] if authors else ""
    display_name = f"{primary_author} - {title}" if primary_author else title
    ffmpeg_tags = {
        "title": title,
        "album": title,
        "artist": ", ".join(narrators or authors),
        "album_artist": primary_author or ", ".join(authors),
        "composer": ", ".join(narrators) if narrators else None,
    }
    return {
        "title": title,
        "authors": authors,
        "narrators": narrators,
        "asin": request.asin,
        "cover_url": request.cover_image,
        "publish_date": request.release_date.isoformat() if request.release_date else None,
        "ffmpeg_tags": ffmpeg_tags,
        "display_name": display_name,
    }

# Attach if missing
try:
    if not hasattr(PostProcessor, "_extract_metadata"):
        PostProcessor._extract_metadata = _pp_extract_metadata  # type: ignore[attr-defined]
except NameError:
    pass

# Fallback: bind other helper methods if missing (defensive against reload issues)
def _pp_find_source_fallback(self, download_dir: Path, name: str, files: list[dict]) -> Optional[Path]:
    for f in files or []:
        rel = f.get("name")
        if not isinstance(rel, str):
            continue
        parts = Path(rel).parts
        if parts:
            candidate = download_dir / parts[0]
            if candidate.exists():
                return candidate
    target = self._normalize(name)
    try:
        for entry in download_dir.iterdir():
            if self._normalize(entry.name) == target:
                return entry
    except FileNotFoundError:
        return None
    return None

def _pp_gather_audio_files(self, base_dir: Path, files: Iterable[dict]) -> List[Path]:
    audio_paths: List[Path] = []
    for f in files:
        name = f.get("name")
        if not isinstance(name, str):
            continue
        path = base_dir / name
        if path.suffix.lower() in AUDIO_EXTENSIONS and path.exists():
            audio_paths.append(path)
    audio_paths.sort()
    return audio_paths

def _pp_find_audio_files_recursive(self, base_dir: Path) -> List[Path]:
    found: List[Path] = []
    for ext in AUDIO_EXTENSIONS:
        found.extend(base_dir.rglob(f"*{ext}"))
    found = [p for p in found if p.is_file()]
    found.sort()
    return found

try:
    if not hasattr(PostProcessor, "_find_source_fallback"):
        PostProcessor._find_source_fallback = _pp_find_source_fallback  # type: ignore[attr-defined]
    if not hasattr(PostProcessor, "_gather_audio_files"):
        PostProcessor._gather_audio_files = _pp_gather_audio_files  # type: ignore[attr-defined]
    if not hasattr(PostProcessor, "_find_audio_files_recursive"):
        PostProcessor._find_audio_files_recursive = _pp_find_audio_files_recursive  # type: ignore[attr-defined]
except NameError:
    pass

    def _extract_metadata(self, request: BookRequest) -> dict:
        title = request.title or "Untitled"
        authors = request.authors or ["Unknown Author"]
        narrators = request.narrators or []
        primary_author = authors[0] if authors else ""
        display_name = f"{primary_author} - {title}" if primary_author else title

        ffmpeg_tags = {
            "title": title,
            "album": title,
            "artist": ", ".join(narrators or authors),
            "album_artist": primary_author or ", ".join(authors),
            "composer": ", ".join(narrators) if narrators else None,
        }

        return {
            "title": title,
            "authors": authors,
            "narrators": narrators,
            "asin": request.asin,
            "cover_url": request.cover_image,
            "publish_date": request.release_date.isoformat() if request.release_date else None,
            "ffmpeg_tags": ffmpeg_tags,
            "display_name": display_name,
        }
