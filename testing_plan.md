# Regression Testing Plan for AudioBookRequest

## Objective
Establish a robust regression testing suite to ensure stability, prevent regressions, and facilitate safe refactoring as the application grows.

## 1. Testing Infrastructure Setup

### Dependencies
Add the following development dependencies:
- **pytest**: Core testing framework.
- **pytest-asyncio**: For testing async functions.
- **httpx**: For async API testing.
- **pytest-cov**: For code coverage reporting.
- **playwright**: For End-to-End (E2E) browser testing.
- **faker**: For generating test data.

### Directory Structure
Create a `tests` directory in the project root:
```
tests/
├── conftest.py          # Global fixtures (DB session, API client, Mock settings)
├── unit/                # Unit tests for isolated logic
│   ├── test_mam_client.py
│   ├── test_download_manager.py
│   └── ...
├── integration/         # API and DB integration tests
│   ├── test_search_routes.py
│   ├── test_auth.py
│   └── ...
└── e2e/                 # Playwright browser tests
    ├── test_login_flow.py
    ├── test_request_flow.py
    └── ...
```

## 2. Unit Testing Strategy
Focus on business logic and isolated components.

- **MAM Client**: Mock the `aiohttp` session to verify `search`, `download_torrent`, and error handling without hitting real APIs.
- **Download Manager**: Test job state transitions, queue management, and post-processing logic using mocked clients.
- **Book Search**: Verify parsing logic and fallback mechanisms (Audimeta/Audnexus) using mocked responses.

## 3. Integration Testing Strategy
Focus on API endpoints and Database interactions.

- **Database Fixtures**: Use a separate SQLite test database (or in-memory) that resets between tests.
- **API Tests**: Use `httpx.AsyncClient` to test FastAPI routes (`/search`, `/request`, `/auth`).
- **Authentication**: Test protected routes by injecting mock user sessions.

## 4. End-to-End (E2E) Testing Strategy
Simulate real user behavior using Playwright.

- **Critical Flows**:
    1. **Login**: Verify login with valid/invalid credentials.
    2. **Search & Request**: Search for a book, add a request, verify it appears in the wishlist.
    3. **MAM Integration**:
        - Trigger "Check on MAM" from a request.
        - Verify MAM search results load (using Mock API mode).
        - Click "Download" and verify success toast.
    4. **Browse Page**: Verify sections load and navigation works.

## 5. CI/CD Integration
- Create a GitHub Action workflow (`.github/workflows/test.yml`) to run:
    - Linting (`ruff`, `pyright`)
    - Unit & Integration Tests (`pytest`)
    - E2E Tests (`playwright`)

## 6. Implementation Steps

1.  **Install Dependencies**: Add packages to `pyproject.toml` and sync.
2.  **Configure Pytest**: Create `pytest.ini` or add config to `pyproject.toml`.
3.  **Create Fixtures**: Set up `conftest.py` for DB and Client.
4.  **Write Initial Tests**:
    - Unit: `MamIndexer` results parsing.
    - Integration: `/search/mam` route (using mock mode).
    - E2E: Basic login and MAM browse test.
5.  **Run & Refine**: Execute tests, fix bugs, and expand coverage.

