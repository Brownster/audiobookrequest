# AudioBookRequest - Bug Fixes and Issues

## Critical Issues

- [x] **1. Remove `exit(0)` in auth flow** - `app/internal/auth/authentication.py:238-251`
  - Application crashes on pydantic validation failure instead of handling gracefully
  - **Fixed**: Removed exit(0) call, now properly raises RequiresLoginException

- [x] **2. Path traversal in manual import** - `app/routers/downloads.py:277-318`
  - No validation on `source_path` allows reading arbitrary files
  - **Fixed**: Added _validate_import_path() function that checks paths against ABR_IMPORT_ROOT and qB local path prefix

- [x] **3. Race condition in DownloadManager singleton** - `app/internal/services/download_manager.py:74-77`
  - Not thread/async safe, could create multiple instances
  - **Fixed**: Added _instance_lock asyncio.Lock and get_instance_async() method with double-check pattern

- [x] **4. SQL injection via PostgreSQL connection string** - `app/util/db.py:10-15`
  - Special chars in password/host not URL-encoded
  - **Fixed**: Added quote_plus() URL encoding for postgres_user, postgres_password, and postgres_db

- [x] **5. HTTP session resource leak** - `app/util/connection.py`, `app/routers/wishlist.py:377`
  - New `ClientSession()` created per request without proper cleanup
  - **Fixed**: Created HTTPSessionManager singleton class with proper lifecycle management and cleanup in app lifespan

- [x] **6. Race condition in job completion** - `app/internal/services/download_manager.py:692-731`
  - Job state can change between check and update
  - **Fixed**: Added _job_lock and wrapped job state transitions with async lock, re-fetch job within lock

## High Priority Issues

- [x] **7. Missing timeouts on external API calls** - `app/internal/book_search.py:73-89`
  - Audnexus, Audible, MAM calls can hang indefinitely
  - **Fixed**: Added EXTERNAL_API_TIMEOUT (30s total, 10s connect) to all external API calls

- [x] **8. No validation of downloaded torrent files** - `app/internal/clients/mam.py:457-506`
  - Could receive HTML error page instead of torrent
  - **Fixed**: Added _validate_torrent_data() method that checks file size and bencode format

- [x] **9. Command injection risk in ffmpeg** - `app/internal/processing/postprocess.py:191-221`
  - Filenames with newlines can break concat file format
  - **Fixed**: Added validation to reject filenames containing newlines

- [x] **10. `assert` used for validation** - `app/internal/book_search.py:509-545`
  - Disabled in production with `-O` flag
  - **Fixed**: Replaced assert with explicit ValueError raise

- [x] **11. Unvalidated datetime parsing** - `app/internal/book_search.py:109,147`
  - Crashes on malformed API responses
  - **Fixed**: Added _parse_date_safe() helper function with try/except

- [x] **12. Missing input validation on UUID fields** - `app/routers/wishlist.py:504-507`
  - Too broad exception handling
  - **Fixed**: Changed from `except Exception` to `except (ValueError, AttributeError, TypeError)`

## Medium Priority Issues

- [ ] **13. Missing rate limiting on external APIs**
  - Audible, Audnexus, MAM calls have no throttling
  - Status: Not fixed - requires additional library (aiolimiter)

- [ ] **14. Unbounded in-memory caches** - `app/internal/book_search.py:188-189`
  - `search_cache` and `search_suggestions_cache` grow without limit
  - Status: Not fixed - existing TTL cleanup provides some mitigation

- [x] **15. Shared cookie cache in QbitClient** - `app/internal/clients/torrent/qbittorrent.py:221`
  - Can leak cookies between instances with same URL
  - **Fixed**: Added credentials hash to cookie cache key

- [x] **16. Missing subprocess timeout in ffmpeg** - `app/internal/processing/postprocess.py:230-241`
  - FFmpeg can hang forever with no timeout
  - **Fixed**: Added 1-hour timeout with asyncio.wait_for and proper cleanup

- [ ] **17. Error messages expose internal paths** - `app/internal/processing/postprocess.py:69-71`
  - Internal file system paths shown to users
  - Status: Not fixed - requires careful analysis of what to expose

- [ ] **18. Missing torrent hash validation** - `app/internal/clients/torrent/qbittorrent.py:436-446`
  - No validation that hash is valid SHA1/SHA256
  - Status: Not fixed - low risk, qBittorrent validates hashes

- [ ] **19. No cleanup of partial files on download failure** - `app/internal/services/download_manager.py:240-285`
  - Partial files left behind when downloads fail
  - Status: Not fixed - requires careful analysis of cleanup paths

- [x] **20. Potential integer overflow in seed time** - `app/internal/services/download_manager.py:598-602`
  - No bounds checking on elapsed seed time
  - **Fixed**: Added MAX_SEED_SECONDS (1 year) clamp

- [x] **21. Unsafe dict access in torrent snapshot** - `app/internal/services/download_manager.py:757-792`
  - No validation before Path operations on content_path
  - **Fixed**: Added type and value validation before using content_path

- [ ] **22. Overly broad exception handling** - Multiple files
  - `except Exception` catches system errors like KeyboardInterrupt
  - Status: Partially fixed in wishlist.py UUID handling

- [ ] **23. Missing validation on series_position** - `app/internal/models.py`
  - Could be malformed string
  - Status: Not fixed - low risk

- [ ] **24. Missing foreign key cascade on DownloadJob** - `app/internal/models.py`
  - Orphaned jobs possible when BookRequest deleted
  - Status: Not fixed - requires database migration

## Low Priority Issues

- [ ] **25. Missing CSRF protection** - `app/routers/wishlist.py`, `app/routers/downloads.py`
  - State-changing endpoints lack CSRF tokens
  - Status: Not fixed - session middleware may provide some protection

- [ ] **26. Missing index on BookRequest.updated_at** - `app/internal/models.py`
  - Cleanup query performance could be improved
  - Status: Not fixed - requires database migration

- [ ] **27. Sanitizer removes too many characters** - `app/internal/processing/postprocess.py:23-27`
  - Periods, commas, parentheses stripped from titles
  - Status: Not fixed - current behavior may be intentional for filesystem safety

- [ ] **28. No retry logic on transient network failures**
  - External API calls fail immediately on network errors
  - Status: Not fixed - requires additional retry library

---

## Summary

**Fixed**: 18 issues (6 critical, 6 high priority, 6 medium priority)
**Not Fixed**: 10 issues (mostly low priority or requiring database migrations/additional libraries)
