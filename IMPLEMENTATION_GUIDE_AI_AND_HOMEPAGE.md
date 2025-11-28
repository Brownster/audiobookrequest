# Implementation Guide: AI Feature Tuning & Homepage Category Management

**Project**: AudioBookRequest
**Features**: Enhanced AI Configuration & Custom Homepage Categories
**Estimated Effort**: 6-8 hours
**Difficulty**: Intermediate

---

## Table of Contents

1. [Background & Context](#background--context)
2. [Architecture Overview](#architecture-overview)
3. [Feature 1: AI Feature Tuning](#feature-1-ai-feature-tuning)
4. [Feature 2: Homepage Category Management](#feature-2-homepage-category-management)
5. [Implementation Order](#implementation-order)
6. [Testing Guide](#testing-guide)
7. [Troubleshooting](#troubleshooting)

---

## Background & Context

### Current State

**AI Features**:
- Located in: `app/internal/ai/` and `app/routers/settings/ai.py`
- Supports two providers: Ollama (self-hosted) and OpenAI-compatible APIs
- Generates personalized book categories and recommendations
- Already has admin settings page at `/settings/ai`
- Current config: provider, model, endpoint, API key, cache TTL

**Homepage Categories**:
- Located in: `app/util/recommendations.py` and `templates/root.html`
- 7 hardcoded categories: trending, business, fiction, biography, science, recent_releases
- Search terms are hardcoded in `get_homepage_recommendations_async()`
- No way to add/remove/customize without code changes

### What We're Building

**AI Feature Tuning** (Extends existing settings):
- Toggle to enable/disable AI features
- Control which AI sections appear on homepage
- Edit prompt templates for customization
- Temperature/creativity controls
- Max categories limit

**Homepage Category Management** (New feature):
- Database-driven category system
- Admin UI to add/edit/delete/reorder categories
- Custom search terms per category
- Enable/disable individual categories
- Priority/ordering control

---

## Architecture Overview

### File Structure

```
app/
├── internal/
│   ├── ai/
│   │   ├── client.py              # AI client (existing)
│   │   └── config.py              # AI config (existing)
│   └── models.py                  # Add HomepageCategory model
├── routers/
│   ├── root.py                    # Homepage route (modify)
│   └── settings/
│       ├── ai.py                  # AI settings (extend)
│       └── homepage.py            # NEW: Homepage settings
├── util/
│   └── recommendations.py         # Recommendation logic (modify)
templates/
├── root.html                      # Homepage (modify)
├── settings/
│   ├── ai.html                    # AI settings page (extend)
│   └── homepage.html              # NEW: Homepage settings page
└── components/
    └── category_manager.html      # NEW: Category list component
alembic/
└── versions/
    └── XXXXXX_add_homepage_categories.py  # NEW: Migration
```

### Tech Stack

- **Backend**: FastAPI, SQLModel (SQLAlchemy)
- **Frontend**: Jinja2 templates, HTMX, DaisyUI/Tailwind CSS
- **Database**: SQLite (PostgreSQL compatible)
- **AI**: Ollama or OpenAI API

---

## Feature 1: AI Feature Tuning

### 1.1 Extend AI Configuration Model

**File**: `app/internal/ai/config.py`

**Current code** (lines 17-29):

```python
@dataclass
class AIConfig:
    provider: Literal["ollama", "openai"]
    model: str
    endpoint: str
    api_key: str | None = None
    cache_ttl_days: int = 3
```

**Add these fields**:

```python
@dataclass
class AIConfig:
    provider: Literal["ollama", "openai"]
    model: str
    endpoint: str
    api_key: str | None = None
    cache_ttl_days: int = 3

    # NEW: Enable/disable controls
    enabled: bool = True
    show_categories: bool = True
    show_recommendations: bool = True
    max_categories: int = 3

    # NEW: Prompt customization
    category_system_prompt: str | None = None
    category_user_prompt_template: str | None = None
    recommendation_system_prompt: str | None = None
    recommendation_user_prompt_template: str | None = None

    # NEW: Generation controls
    temperature: float = 0.2
    max_search_terms_per_category: int = 8
```

**Default prompts to use** (if None):

```python
# Add these as module-level constants in config.py

DEFAULT_CATEGORY_SYSTEM_PROMPT = """You are an assistant that suggests discovery categories for audiobooks.
Your job is to look at a user's recent audiobook requests and generate 1-3 unique discovery categories that would help them find more books they might enjoy."""

DEFAULT_CATEGORY_USER_PROMPT_TEMPLATE = """Based on this user's audiobook history:

Recent Requests:
{recent_requests}

Top Authors: {top_authors}
Top Narrators: {top_narrators}

Generate {count} discovery categories as JSON with this structure:
{{
  "categories": [
    {{
      "title": "Category Name",
      "description": "Why this category suits the user",
      "search_terms": ["term1", "term2", ...],
      "reasoning": "Brief explanation"
    }}
  ]
}}

Requirements:
- Each category should have 3-8 search terms
- Make categories specific to user's tastes
- Avoid generic categories like "Popular Books"
- Return ONLY valid JSON, no other text"""

DEFAULT_RECOMMENDATION_SYSTEM_PROMPT = """You recommend specific audiobook titles that match a user's tastes.
Analyze their recent requests and suggest similar books they haven't read yet."""

DEFAULT_RECOMMENDATION_USER_PROMPT_TEMPLATE = """Based on these recent audiobooks:

{recent_requests}

Recommend 5 specific audiobook titles as JSON:
{{
  "recommendations": [
    {{
      "seed_title": "Book that inspired this rec",
      "seed_author": "Author of seed book",
      "recommended_title": "New book title",
      "author": "Author of new book",
      "reasoning": "Why user would like this",
      "search_terms": ["term1", "term2", "term3"]
    }}
  ]
}}

Return ONLY valid JSON, no other text."""
```

**Update `from_db()` method** to load new fields:

```python
@classmethod
def from_db(cls, session: Session) -> "AIConfig | None":
    """Load config from database."""
    provider = indexer_configuration_cache.get(session, "ai_provider")
    if not provider or provider not in ["ollama", "openai"]:
        return None

    model = indexer_configuration_cache.get(session, "ai_model")
    endpoint = indexer_configuration_cache.get(session, "ai_endpoint")
    if not model or not endpoint:
        return None

    api_key = indexer_configuration_cache.get(session, "ai_api_key")
    cache_ttl = indexer_configuration_cache.get(session, "ai_cache_ttl_days")

    # NEW: Load additional fields with defaults
    enabled_str = indexer_configuration_cache.get(session, "ai_enabled")
    enabled = enabled_str != "false" if enabled_str else True

    show_cat_str = indexer_configuration_cache.get(session, "ai_show_categories")
    show_categories = show_cat_str != "false" if show_cat_str else True

    show_rec_str = indexer_configuration_cache.get(session, "ai_show_recommendations")
    show_recommendations = show_rec_str != "false" if show_rec_str else True

    max_cat_str = indexer_configuration_cache.get(session, "ai_max_categories")
    max_categories = int(max_cat_str) if max_cat_str else 3

    temp_str = indexer_configuration_cache.get(session, "ai_temperature")
    temperature = float(temp_str) if temp_str else 0.2

    max_terms_str = indexer_configuration_cache.get(session, "ai_max_search_terms")
    max_search_terms = int(max_terms_str) if max_terms_str else 8

    return cls(
        provider=provider,
        model=model,
        endpoint=endpoint,
        api_key=api_key,
        cache_ttl_days=int(cache_ttl) if cache_ttl else 3,
        enabled=enabled,
        show_categories=show_categories,
        show_recommendations=show_recommendations,
        max_categories=max_categories,
        category_system_prompt=indexer_configuration_cache.get(session, "ai_category_system_prompt"),
        category_user_prompt_template=indexer_configuration_cache.get(session, "ai_category_user_prompt"),
        recommendation_system_prompt=indexer_configuration_cache.get(session, "ai_recommendation_system_prompt"),
        recommendation_user_prompt_template=indexer_configuration_cache.get(session, "ai_recommendation_user_prompt"),
        temperature=temperature,
        max_search_terms_per_category=max_search_terms,
    )
```

### 1.2 Update AI Client to Use Custom Prompts

**File**: `app/internal/ai/client.py`

**Find `fetch_ai_categories()` function** (around line 100-150):

**Current code** has hardcoded prompts like:

```python
system_prompt = "You are an assistant that suggests discovery categories for audiobooks."
user_prompt = f"""Based on this user's audiobook history:
...
"""
```

**Replace with**:

```python
async def fetch_ai_categories(
    ai_config: AIConfig,
    http_session: ClientSession,
    session: Session,
    username: str,
    count: int = 3,
) -> list[dict]:
    """Fetch AI-generated discovery categories."""

    # Use custom prompts if provided, otherwise use defaults
    system_prompt = ai_config.category_system_prompt or DEFAULT_CATEGORY_SYSTEM_PROMPT
    user_prompt_template = ai_config.category_user_prompt_template or DEFAULT_CATEGORY_USER_PROMPT_TEMPLATE

    # Build user profile
    profile = await build_user_profile(session, username)
    recent_str = "\n".join([f"- {r['title']} by {', '.join(r['authors'])}" for r in profile["recent_requests"][:10]])
    top_authors = ", ".join(profile["top_authors"][:8])
    top_narrators = ", ".join(profile["top_narrators"][:8])

    # Format user prompt with template
    user_prompt = user_prompt_template.format(
        recent_requests=recent_str,
        top_authors=top_authors,
        top_narrators=top_narrators,
        count=count,
    )

    # Rest of function remains the same, but use ai_config.temperature
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response = await call_llm(
        ai_config.provider,
        ai_config.endpoint,
        ai_config.model,
        messages,
        ai_config.api_key,
        http_session,
        temperature=ai_config.temperature,  # Use config temperature
    )
    # ... rest of function
```

**Do the same for `fetch_ai_book_recommendations()`**.

### 1.3 Update AI Settings Page UI

**File**: `templates/settings/ai.html`

**Current structure**: Form with provider, model, endpoint, API key, cache TTL

**Add these sections** after the existing fields:

```html
<!-- After existing cache_ttl field, add these sections -->

<!-- Enable/Disable Section -->
<div class="divider">Display Settings</div>

<div class="form-control">
  <label class="label cursor-pointer justify-start gap-4">
    <input type="checkbox" name="enabled" class="toggle toggle-primary"
           {% if config.enabled %}checked{% endif %}>
    <div>
      <span class="label-text font-semibold">Enable AI Features</span>
      <p class="text-xs opacity-70">Turn all AI recommendations on/off globally</p>
    </div>
  </label>
</div>

<div class="form-control">
  <label class="label cursor-pointer justify-start gap-4">
    <input type="checkbox" name="show_categories" class="toggle toggle-primary"
           {% if config.show_categories %}checked{% endif %}>
    <div>
      <span class="label-text font-semibold">Show AI Category Sections</span>
      <p class="text-xs opacity-70">Display AI-generated discovery categories on homepage</p>
    </div>
  </label>
</div>

<div class="form-control">
  <label class="label cursor-pointer justify-start gap-4">
    <input type="checkbox" name="show_recommendations" class="toggle toggle-primary"
           {% if config.show_recommendations %}checked{% endif %}>
    <div>
      <span class="label-text font-semibold">Show "Because You Liked" Recommendations</span>
      <p class="text-xs opacity-70">Display AI book recommendations with reasoning</p>
    </div>
  </label>
</div>

<div class="form-control">
  <label class="label">
    <span class="label-text">Max Category Sections on Homepage</span>
  </label>
  <input type="number" name="max_categories" min="1" max="5"
         value="{{ config.max_categories or 3 }}"
         class="input input-bordered w-24">
  <label class="label">
    <span class="label-text-alt opacity-70">Limit AI category sections (1-5)</span>
  </label>
</div>

<!-- Generation Controls Section -->
<div class="divider">Generation Controls</div>

<div class="form-control">
  <label class="label">
    <span class="label-text">Temperature (Creativity)</span>
  </label>
  <div class="flex items-center gap-4">
    <input type="range" name="temperature" min="0" max="1" step="0.1"
           value="{{ config.temperature or 0.2 }}"
           class="range range-primary" id="temp-slider">
    <span id="temp-value" class="badge badge-neutral">{{ config.temperature or 0.2 }}</span>
  </div>
  <label class="label">
    <span class="label-text-alt opacity-70">0 = deterministic, 1 = very creative</span>
  </label>
</div>

<script>
// Update temperature display
const tempSlider = document.getElementById('temp-slider');
const tempValue = document.getElementById('temp-value');
if (tempSlider && tempValue) {
  tempSlider.addEventListener('input', (e) => {
    tempValue.textContent = e.target.value;
  });
}
</script>

<div class="form-control">
  <label class="label">
    <span class="label-text">Max Search Terms per Category</span>
  </label>
  <input type="number" name="max_search_terms" min="3" max="12"
         value="{{ config.max_search_terms_per_category or 8 }}"
         class="input input-bordered w-24">
  <label class="label">
    <span class="label-text-alt opacity-70">More terms = broader search (3-12)</span>
  </label>
</div>

<!-- Prompt Customization Section -->
<div class="divider">Advanced: Prompt Templates</div>

<div class="alert alert-info">
  <svg class="w-6 h-6 stroke-current" fill="none" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
  </svg>
  <span class="text-sm">Leave blank to use defaults. Use <code>{recent_requests}</code>, <code>{top_authors}</code>, <code>{count}</code> placeholders.</span>
</div>

<details class="collapse collapse-arrow bg-base-200">
  <summary class="collapse-title font-medium">
    Category Generation Prompts
  </summary>
  <div class="collapse-content space-y-4">
    <div class="form-control">
      <label class="label">
        <span class="label-text">System Prompt</span>
      </label>
      <textarea name="category_system_prompt" rows="3"
                class="textarea textarea-bordered font-mono text-xs"
                placeholder="Leave blank for default...">{{ config.category_system_prompt or '' }}</textarea>
    </div>

    <div class="form-control">
      <label class="label">
        <span class="label-text">User Prompt Template</span>
      </label>
      <textarea name="category_user_prompt" rows="8"
                class="textarea textarea-bordered font-mono text-xs"
                placeholder="Leave blank for default...">{{ config.category_user_prompt_template or '' }}</textarea>
    </div>
  </div>
</details>

<details class="collapse collapse-arrow bg-base-200">
  <summary class="collapse-title font-medium">
    Book Recommendation Prompts
  </summary>
  <div class="collapse-content space-y-4">
    <div class="form-control">
      <label class="label">
        <span class="label-text">System Prompt</span>
      </label>
      <textarea name="recommendation_system_prompt" rows="3"
                class="textarea textarea-bordered font-mono text-xs"
                placeholder="Leave blank for default...">{{ config.recommendation_system_prompt or '' }}</textarea>
    </div>

    <div class="form-control">
      <label class="label">
        <span class="label-text">User Prompt Template</span>
      </label>
      <textarea name="recommendation_user_prompt" rows="8"
                class="textarea textarea-bordered font-mono text-xs"
                placeholder="Leave blank for default...">{{ config.recommendation_user_prompt_template or '' }}</textarea>
    </div>
  </div>
</details>

<!-- Keep existing Save button -->
```

### 1.4 Update AI Settings Route Handler

**File**: `app/routers/settings/ai.py`

**Find the `save_ai_settings()` function** (POST handler):

**Add these lines** to save the new fields:

```python
@router.post("/settings/ai")
async def save_ai_settings(
    request: Request,
    provider: str = Form(...),
    model: str = Form(...),
    endpoint: str = Form(...),
    api_key: str = Form(None),
    cache_ttl_days: int = Form(3),
    # NEW: Add new form fields
    enabled: bool = Form(False),
    show_categories: bool = Form(False),
    show_recommendations: bool = Form(False),
    max_categories: int = Form(3),
    temperature: float = Form(0.2),
    max_search_terms: int = Form(8),
    category_system_prompt: str = Form(None),
    category_user_prompt: str = Form(None),
    recommendation_system_prompt: str = Form(None),
    recommendation_user_prompt: str = Form(None),
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    # Save existing fields
    indexer_configuration_cache.set(session, "ai_provider", provider)
    indexer_configuration_cache.set(session, "ai_model", model)
    indexer_configuration_cache.set(session, "ai_endpoint", endpoint)
    indexer_configuration_cache.set(session, "ai_api_key", api_key or "")
    indexer_configuration_cache.set(session, "ai_cache_ttl_days", str(cache_ttl_days))

    # NEW: Save new fields
    indexer_configuration_cache.set(session, "ai_enabled", "true" if enabled else "false")
    indexer_configuration_cache.set(session, "ai_show_categories", "true" if show_categories else "false")
    indexer_configuration_cache.set(session, "ai_show_recommendations", "true" if show_recommendations else "false")
    indexer_configuration_cache.set(session, "ai_max_categories", str(max_categories))
    indexer_configuration_cache.set(session, "ai_temperature", str(temperature))
    indexer_configuration_cache.set(session, "ai_max_search_terms", str(max_search_terms))

    # Save custom prompts (only if not empty)
    if category_system_prompt and category_system_prompt.strip():
        indexer_configuration_cache.set(session, "ai_category_system_prompt", category_system_prompt.strip())
    else:
        indexer_configuration_cache.delete(session, "ai_category_system_prompt")

    if category_user_prompt and category_user_prompt.strip():
        indexer_configuration_cache.set(session, "ai_category_user_prompt", category_user_prompt.strip())
    else:
        indexer_configuration_cache.delete(session, "ai_category_user_prompt")

    if recommendation_system_prompt and recommendation_system_prompt.strip():
        indexer_configuration_cache.set(session, "ai_recommendation_system_prompt", recommendation_system_prompt.strip())
    else:
        indexer_configuration_cache.delete(session, "ai_recommendation_system_prompt")

    if recommendation_user_prompt and recommendation_user_prompt.strip():
        indexer_configuration_cache.set(session, "ai_recommendation_user_prompt", recommendation_user_prompt.strip())
    else:
        indexer_configuration_cache.delete(session, "ai_recommendation_user_prompt")

    session.commit()

    # Redirect back with success message
    return RedirectResponse(
        url=f"{request.url_for('ai_settings')}?saved=true",
        status_code=303,
    )
```

### 1.5 Respect Enable/Disable Settings on Homepage

**File**: `app/routers/root.py`

**Find the homepage route** (`read_root()` function):

**Modify AI loading logic**:

```python
@router.get("/")
async def read_root(
    request: Request,
    user: DetailedUser = Security(ABRAuth()),
):
    # ... existing code for ABS, recommendations, etc.

    # Load AI config
    ai_config = AIConfig.from_db(session)

    # NEW: Only include AI sections if enabled
    context = {
        "user": user,
        "base_url": settings.base_url,
        "recommendations": recommendations,
        "abs_library": abs_books,
        # NEW: Pass AI config flags to template
        "ai_enabled": ai_config.enabled if ai_config else False,
        "ai_show_categories": ai_config.show_categories if ai_config else False,
        "ai_show_recommendations": ai_config.show_recommendations if ai_config else False,
    }

    return template_response("root.html", request, user, context)
```

**File**: `templates/root.html`

**Find the HTMX AI loading sections** (search for `hx-get="/recommendations/ai`):

**Wrap them in conditionals**:

```html
<!-- AI Categories Section -->
{% if ai_enabled and ai_show_categories %}
  <div class="mb-8"
       hx-get="{{ base_url }}/recommendations/ai/home-fragment"
       hx-trigger="load"
       hx-swap="outerHTML">
    <div class="skeleton h-64 w-full"></div>
  </div>
{% endif %}

<!-- AI Book Recommendations Section -->
{% if ai_enabled and ai_show_recommendations %}
  <div class="mb-8"
       hx-get="{{ base_url }}/recommendations/ai/book-recs-fragment"
       hx-trigger="load"
       hx-swap="outerHTML">
    <div class="skeleton h-64 w-full"></div>
  </div>
{% endif %}
```

---

## Feature 2: Homepage Category Management

### 2.1 Create Database Model

**File**: `app/internal/models.py`

**Add this model** (after existing models, before end of file):

```python
class HomepageCategory(BaseModel, table=True):
    """User-configurable homepage discovery categories."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(unique=True, index=True)  # e.g., "trending", "custom_fantasy"
    title: str  # Display name, e.g., "Trending This Week"
    description: Optional[str] = None  # Optional description
    search_terms: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    emoji: Optional[str] = None  # Optional emoji icon
    enabled: bool = True
    priority: int = 0  # Lower = appears first
    is_default: bool = False  # True for built-in categories (can't delete)
    created_by: str = Field(foreign_key="user.username", ondelete="CASCADE")
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            onupdate=func.now(),
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )

    class Config:
        arbitrary_types_allowed = True
```

### 2.2 Create Database Migration

**Create new migration file**: `alembic/versions/XXXXXX_add_homepage_categories.py`

Run this command to generate:

```bash
alembic revision -m "add_homepage_categories"
```

**Then edit the generated file**:

```python
"""add_homepage_categories

Revision ID: <generated_id>
Revises: <previous_revision>
Create Date: <generated_date>

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite
import uuid

# revision identifiers
revision = '<generated_id>'
down_revision = '<previous_revision>'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create homepagecategory table
    op.create_table(
        'homepagecategory',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('search_terms', sa.JSON(), nullable=True),
        sa.Column('emoji', sa.String(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['created_by'], ['user.username'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug')
    )
    op.create_index('ix_homepagecategory_slug', 'homepagecategory', ['slug'])

    # Insert default categories
    # You'll need to replace 'admin' with an actual admin username from your database
    # Or run this as a data migration after deployment
    from datetime import datetime
    now = datetime.utcnow()

    default_categories = [
        {
            'id': str(uuid.uuid4()),
            'slug': 'trending',
            'title': 'Trending This Week',
            'search_terms': '["trending", "viral", "popular now", "hot"]',
            'emoji': '🔥',
            'enabled': 1,
            'priority': 10,
            'is_default': 1,
            'created_by': 'admin',  # UPDATE THIS
            'updated_at': now,
        },
        {
            'id': str(uuid.uuid4()),
            'slug': 'business',
            'title': 'Business & Leadership',
            'search_terms': '["business", "entrepreneurship", "leadership", "productivity", "success"]',
            'emoji': '💼',
            'enabled': 1,
            'priority': 20,
            'is_default': 1,
            'created_by': 'admin',
            'updated_at': now,
        },
        {
            'id': str(uuid.uuid4()),
            'slug': 'fiction',
            'title': 'Fiction & Literature',
            'search_terms': '["fiction", "novel", "literature", "story", "fantasy", "mystery"]',
            'emoji': '📖',
            'enabled': 1,
            'priority': 30,
            'is_default': 1,
            'created_by': 'admin',
            'updated_at': now,
        },
        {
            'id': str(uuid.uuid4()),
            'slug': 'biography',
            'title': 'Biography & Memoir',
            'search_terms': '["biography", "memoir", "autobiography", "life story", "history"]',
            'emoji': '👤',
            'enabled': 1,
            'priority': 40,
            'is_default': 1,
            'created_by': 'admin',
            'updated_at': now,
        },
        {
            'id': str(uuid.uuid4()),
            'slug': 'science',
            'title': 'Science & Technology',
            'search_terms': '["science", "technology", "physics", "psychology", "innovation"]',
            'emoji': '🔬',
            'enabled': 1,
            'priority': 50,
            'is_default': 1,
            'created_by': 'admin',
            'updated_at': now,
        },
        {
            'id': str(uuid.uuid4()),
            'slug': 'recent_releases',
            'title': 'Recent Releases',
            'search_terms': '["2024", "2025", "new release", "latest", "just released"]',
            'emoji': '🆕',
            'enabled': 1,
            'priority': 60,
            'is_default': 1,
            'created_by': 'admin',
            'updated_at': now,
        },
    ]

    for cat in default_categories:
        op.execute(
            f"""INSERT INTO homepagecategory
            (id, slug, title, search_terms, emoji, enabled, priority, is_default, created_by, updated_at)
            VALUES
            ('{cat['id']}', '{cat['slug']}', '{cat['title']}', '{cat['search_terms']}', '{cat['emoji']}',
             {cat['enabled']}, {cat['priority']}, {cat['is_default']}, '{cat['created_by']}', '{cat['updated_at']}')
            """
        )


def downgrade() -> None:
    op.drop_index('ix_homepagecategory_slug', 'homepagecategory')
    op.drop_table('homepagecategory')
```

**Run the migration**:

```bash
alembic upgrade head
```

### 2.3 Modify Recommendations Logic

**File**: `app/util/recommendations.py`

**Find `get_homepage_recommendations_async()` function** (around line 150-250):

**Current code** has hardcoded categories dict:

```python
categories = {
    "trending": ["trending", "viral", "popular now", "hot"],
    "business": ["business", "entrepreneurship", ...],
    # etc.
}
```

**Replace with database loading**:

```python
async def get_homepage_recommendations_async(
    audible_api,
    session: Session,
    username: str,
    abs_seeds: list[str] | None = None,
) -> dict[str, list[BookSearchResult]]:
    """Get all homepage recommendation sections."""
    from app.internal.models import HomepageCategory
    from sqlmodel import select

    # Load categories from database instead of hardcoding
    db_categories = session.exec(
        select(HomepageCategory)
        .where(HomepageCategory.enabled == True)
        .order_by(HomepageCategory.priority)
    ).all()

    # Convert to dict format expected by rest of function
    categories = {}
    for cat in db_categories:
        categories[cat.slug] = cat.search_terms

    # Rest of function continues as before
    results = {}

    # For each category, fetch books
    for slug, search_terms in categories.items():
        books = await get_category_books(audible_api, search_terms)
        if books:
            results[slug] = books

    # Add personalized sections (for_you, because_you_read, etc.)
    # ... existing code continues

    return results
```

### 2.4 Update Homepage Template to Use Database Categories

**File**: `templates/root.html`

**Find the section** where categories are rendered (search for `{% for category in recommendations`):

**Current code** might look like:

```html
{% if recommendations.trending %}
  <div class="mb-8">
    <h2>🔥 Trending This Week</h2>
    <!-- render books -->
  </div>
{% endif %}

{% if recommendations.business %}
  <div class="mb-8">
    <h2>💼 Business & Leadership</h2>
    <!-- render books -->
  </div>
{% endif %}
```

**Replace with dynamic rendering**:

```html
{# Render database-driven categories #}
{% for category in homepage_categories %}
  {% if recommendations.get(category.slug) %}
    <div class="mb-8">
      <div class="flex items-center gap-2 mb-4">
        {% if category.emoji %}
          <span class="text-2xl">{{ category.emoji }}</span>
        {% endif %}
        <h2 class="text-2xl font-bold">{{ category.title }}</h2>
      </div>

      {% if category.description %}
        <p class="text-sm opacity-70 mb-4">{{ category.description }}</p>
      {% endif %}

      <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
        {% for book in recommendations[category.slug][:12] %}
          {% include "components/book_card.html" %}
        {% endfor %}
      </div>
    </div>
  {% endif %}
{% endfor %}
```

**Update the route handler** to pass categories:

**File**: `app/routers/root.py`

```python
@router.get("/")
async def read_root(
    request: Request,
    user: DetailedUser = Security(ABRAuth()),
):
    with open_session() as session:
        # ... existing code

        # NEW: Load homepage categories for template
        from app.internal.models import HomepageCategory
        homepage_categories = session.exec(
            select(HomepageCategory)
            .where(HomepageCategory.enabled == True)
            .order_by(HomepageCategory.priority)
        ).all()

        context = {
            "user": user,
            "base_url": settings.base_url,
            "recommendations": recommendations,
            "homepage_categories": homepage_categories,  # NEW
            "abs_library": abs_books,
            # ... other context
        }

        return template_response("root.html", request, user, context)
```

### 2.5 Create Homepage Settings Page

**File**: `app/routers/settings/homepage.py` (NEW)

```python
import uuid
from fastapi import APIRouter, Depends, Request, Security, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import HomepageCategory
from app.util.db import get_session
from app.util.templates import template_response

router = APIRouter()


@router.get("/settings/homepage")
async def homepage_settings(
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    """Homepage category management page."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    categories = session.exec(
        select(HomepageCategory).order_by(HomepageCategory.priority)
    ).all()

    return template_response(
        "settings/homepage.html",
        request,
        user,
        {"categories": categories},
    )


@router.get("/settings/homepage/fragment")
async def homepage_categories_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    """Category list fragment for HTMX updates."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    categories = session.exec(
        select(HomepageCategory).order_by(HomepageCategory.priority)
    ).all()

    return template_response(
        "components/category_manager.html",
        request,
        user,
        {"categories": categories},
    )


@router.post("/settings/homepage/categories")
async def create_category(
    request: Request,
    title: str = Form(...),
    description: str = Form(None),
    search_terms: str = Form(...),
    emoji: str = Form(None),
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    """Create a new custom category."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    # Generate slug from title
    slug = title.lower().replace(" ", "_").replace("&", "and")
    slug = "".join(c for c in slug if c.isalnum() or c == "_")
    slug = f"custom_{slug}"

    # Check if slug exists
    existing = session.exec(
        select(HomepageCategory).where(HomepageCategory.slug == slug)
    ).first()
    if existing:
        slug = f"{slug}_{uuid.uuid4().hex[:6]}"

    # Parse search terms (comma-separated)
    terms = [t.strip() for t in search_terms.split(",") if t.strip()]
    if not terms:
        raise HTTPException(status_code=400, detail="At least one search term required")

    # Get max priority and add 10
    max_priority = session.exec(
        select(HomepageCategory.priority).order_by(HomepageCategory.priority.desc())
    ).first()
    priority = (max_priority or 0) + 10

    # Create category
    category = HomepageCategory(
        slug=slug,
        title=title,
        description=description if description else None,
        search_terms=terms,
        emoji=emoji if emoji else None,
        enabled=True,
        priority=priority,
        is_default=False,
        created_by=user.username,
    )

    session.add(category)
    session.commit()
    session.refresh(category)

    # Return updated list
    categories = session.exec(
        select(HomepageCategory).order_by(HomepageCategory.priority)
    ).all()

    return template_response(
        "components/category_manager.html",
        request,
        user,
        {"categories": categories},
    )


@router.patch("/settings/homepage/categories/{category_id}")
async def update_category(
    category_id: str,
    request: Request,
    title: str = Form(None),
    description: str = Form(None),
    search_terms: str = Form(None),
    emoji: str = Form(None),
    enabled: bool = Form(None),
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    """Update a category."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        cat_uuid = uuid.UUID(category_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Category not found")

    category = session.get(HomepageCategory, cat_uuid)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    # Update fields if provided
    if title is not None:
        category.title = title
    if description is not None:
        category.description = description if description else None
    if search_terms is not None:
        terms = [t.strip() for t in search_terms.split(",") if t.strip()]
        if terms:
            category.search_terms = terms
    if emoji is not None:
        category.emoji = emoji if emoji else None
    if enabled is not None:
        category.enabled = enabled

    session.add(category)
    session.commit()

    # Return updated list
    categories = session.exec(
        select(HomepageCategory).order_by(HomepageCategory.priority)
    ).all()

    return template_response(
        "components/category_manager.html",
        request,
        user,
        {"categories": categories},
    )


@router.delete("/settings/homepage/categories/{category_id}")
async def delete_category(
    category_id: str,
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    """Delete a category (only non-default ones)."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        cat_uuid = uuid.UUID(category_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Category not found")

    category = session.get(HomepageCategory, cat_uuid)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    if category.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete default categories")

    session.delete(category)
    session.commit()

    # Return updated list
    categories = session.exec(
        select(HomepageCategory).order_by(HomepageCategory.priority)
    ).all()

    return template_response(
        "components/category_manager.html",
        request,
        user,
        {"categories": categories},
    )


@router.post("/settings/homepage/categories/reorder")
async def reorder_categories(
    request: Request,
    category_ids: str = Form(...),  # Comma-separated IDs in new order
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    """Reorder categories by priority."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    ids = [id.strip() for id in category_ids.split(",") if id.strip()]

    for index, cat_id in enumerate(ids):
        try:
            cat_uuid = uuid.UUID(cat_id)
            category = session.get(HomepageCategory, cat_uuid)
            if category:
                category.priority = (index + 1) * 10
                session.add(category)
        except ValueError:
            continue

    session.commit()

    # Return updated list
    categories = session.exec(
        select(HomepageCategory).order_by(HomepageCategory.priority)
    ).all()

    return template_response(
        "components/category_manager.html",
        request,
        user,
        {"categories": categories},
    )
```

**Register the router** in `app/main.py`:

```python
from app.routers.settings import homepage as settings_homepage

app.include_router(settings_homepage.router)
```

### 2.6 Create Homepage Settings Template

**File**: `templates/settings/homepage.html` (NEW)

```html
{% extends "base.html" %}
{% block head %}
  <title>Homepage Settings</title>
  <script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
{% endblock head %}
{% block body %}
  <div class="w-screen flex flex-col items-center justify-center p-6 sm:p-8 overflow-x-hidden gap-4">
    <div class="w-full max-w-4xl">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-3xl font-bold">Homepage Categories</h1>
          <p class="text-sm opacity-70">Manage discovery categories shown on the homepage</p>
        </div>
        <a href="{{ base_url }}/settings" class="btn btn-ghost">Back to Settings</a>
      </div>

      <!-- Add Category Form -->
      <div class="card bg-base-200 shadow-xl mb-6">
        <div class="card-body">
          <h2 class="card-title">Add New Category</h2>
          <form hx-post="{{ base_url }}/settings/homepage/categories"
                hx-target="#category-list"
                hx-swap="outerHTML">

            <div class="form-control">
              <label class="label">
                <span class="label-text">Category Title</span>
              </label>
              <input type="text" name="title" required
                     placeholder="e.g., Sci-Fi & Fantasy"
                     class="input input-bordered">
            </div>

            <div class="form-control">
              <label class="label">
                <span class="label-text">Description (Optional)</span>
              </label>
              <input type="text" name="description"
                     placeholder="Brief description..."
                     class="input input-bordered">
            </div>

            <div class="form-control">
              <label class="label">
                <span class="label-text">Search Terms (comma-separated)</span>
              </label>
              <input type="text" name="search_terms" required
                     placeholder="science fiction, space opera, cyberpunk"
                     class="input input-bordered">
              <label class="label">
                <span class="label-text-alt">These terms are used to search Audible for books</span>
              </label>
            </div>

            <div class="form-control">
              <label class="label">
                <span class="label-text">Emoji Icon (Optional)</span>
              </label>
              <input type="text" name="emoji"
                     placeholder="🚀"
                     maxlength="2"
                     class="input input-bordered w-24">
            </div>

            <div class="card-actions justify-end mt-4">
              <button type="submit" class="btn btn-primary">Add Category</button>
            </div>
          </form>
        </div>
      </div>

      <!-- Category List -->
      <div class="card bg-base-200 shadow-xl">
        <div class="card-body">
          <h2 class="card-title">Existing Categories</h2>
          <p class="text-sm opacity-70 mb-4">Drag to reorder, toggle to enable/disable</p>

          <div id="category-list"
               hx-get="{{ base_url }}/settings/homepage/fragment"
               hx-trigger="load"
               hx-swap="outerHTML">
            <div class="loading">Loading...</div>
          </div>
        </div>
      </div>
    </div>
  </div>
{% endblock body %}
```

### 2.7 Create Category Manager Component

**File**: `templates/components/category_manager.html` (NEW)

```html
<div id="category-list" class="space-y-2">
  {% if not categories or not categories|length %}
    <div class="text-sm opacity-70 p-4">No categories yet.</div>
  {% else %}
    <div id="sortable-categories">
      {% for category in categories %}
        <div class="card bg-base-300 shadow mb-2" data-id="{{ category.id }}">
          <div class="card-body p-4">
            <div class="flex items-center justify-between">
              <!-- Drag Handle + Info -->
              <div class="flex items-center gap-4 flex-grow">
                <div class="cursor-move opacity-50 hover:opacity-100">
                  <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 20 20">
                    <path d="M10 6a2 2 0 110-4 2 2 0 010 4zM10 12a2 2 0 110-4 2 2 0 010 4zM10 18a2 2 0 110-4 2 2 0 010 4z"/>
                  </svg>
                </div>

                <div class="flex items-center gap-3 flex-grow">
                  {% if category.emoji %}
                    <span class="text-2xl">{{ category.emoji }}</span>
                  {% endif %}

                  <div class="flex-grow">
                    <div class="font-bold text-lg">{{ category.title }}</div>
                    <div class="text-xs opacity-70">
                      {{ category.search_terms|join(", ") }}
                    </div>
                    {% if category.description %}
                      <div class="text-sm opacity-60 mt-1">{{ category.description }}</div>
                    {% endif %}
                  </div>
                </div>
              </div>

              <!-- Controls -->
              <div class="flex items-center gap-2">
                <!-- Enable/Disable Toggle -->
                <input type="checkbox"
                       class="toggle toggle-success"
                       {% if category.enabled %}checked{% endif %}
                       hx-patch="{{ base_url }}/settings/homepage/categories/{{ category.id }}"
                       hx-vals='{"enabled": {{ "false" if category.enabled else "true" }}}'
                       hx-target="#category-list"
                       hx-swap="outerHTML">

                <!-- Edit Button -->
                <button class="btn btn-square btn-sm btn-ghost"
                        onclick="editCategory{{ loop.index }}Modal.showModal()">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>
                  </svg>
                </button>

                <!-- Delete Button (only for custom categories) -->
                {% if not category.is_default %}
                  <button class="btn btn-square btn-sm btn-error btn-outline"
                          hx-delete="{{ base_url }}/settings/homepage/categories/{{ category.id }}"
                          hx-target="#category-list"
                          hx-swap="outerHTML"
                          hx-confirm="Delete {{ category.title }}?">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                    </svg>
                  </button>
                {% else %}
                  <span class="badge badge-neutral badge-sm">Default</span>
                {% endif %}
              </div>
            </div>
          </div>
        </div>

        <!-- Edit Modal -->
        <dialog id="editCategory{{ loop.index }}Modal" class="modal">
          <div class="modal-box">
            <h3 class="font-bold text-lg mb-4">Edit {{ category.title }}</h3>
            <form hx-patch="{{ base_url }}/settings/homepage/categories/{{ category.id }}"
                  hx-target="#category-list"
                  hx-swap="outerHTML"
                  onsubmit="editCategory{{ loop.index }}Modal.close()">

              <div class="form-control">
                <label class="label"><span class="label-text">Title</span></label>
                <input type="text" name="title" value="{{ category.title }}" class="input input-bordered">
              </div>

              <div class="form-control">
                <label class="label"><span class="label-text">Description</span></label>
                <input type="text" name="description" value="{{ category.description or '' }}" class="input input-bordered">
              </div>

              <div class="form-control">
                <label class="label"><span class="label-text">Search Terms</span></label>
                <input type="text" name="search_terms" value="{{ category.search_terms|join(', ') }}" class="input input-bordered">
              </div>

              <div class="form-control">
                <label class="label"><span class="label-text">Emoji</span></label>
                <input type="text" name="emoji" value="{{ category.emoji or '' }}" maxlength="2" class="input input-bordered w-24">
              </div>

              <div class="modal-action">
                <button type="button" class="btn" onclick="editCategory{{ loop.index }}Modal.close()">Cancel</button>
                <button type="submit" class="btn btn-primary">Save</button>
              </div>
            </form>
          </div>
          <form method="dialog" class="modal-backdrop">
            <button>close</button>
          </form>
        </dialog>
      {% endfor %}
    </div>

    <!-- Initialize drag-and-drop -->
    <script>
      const sortable = Sortable.create(document.getElementById('sortable-categories'), {
        animation: 150,
        handle: '.cursor-move',
        onEnd: function(evt) {
          // Get new order
          const items = document.querySelectorAll('#sortable-categories > div');
          const ids = Array.from(items).map(item => item.dataset.id).join(',');

          // Send to server
          fetch('{{ base_url }}/settings/homepage/categories/reorder', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: 'category_ids=' + encodeURIComponent(ids),
          }).then(response => response.text())
            .then(html => {
              document.getElementById('category-list').outerHTML = html;
              // Re-initialize sortable after update
              setTimeout(() => {
                Sortable.create(document.getElementById('sortable-categories'), {
                  animation: 150,
                  handle: '.cursor-move',
                  onEnd: arguments.callee
                });
              }, 100);
            });
        }
      });
    </script>
  {% endif %}
</div>
```

---

## Implementation Order

Follow this sequence to minimize issues:

### Phase 1: AI Feature Tuning (2-3 hours)

1. **Update Models**:
   - [ ] Add new fields to `AIConfig` dataclass in `app/internal/ai/config.py`
   - [ ] Add default prompt constants
   - [ ] Update `from_db()` method to load new fields

2. **Update AI Client**:
   - [ ] Modify `fetch_ai_categories()` to use custom prompts
   - [ ] Modify `fetch_ai_book_recommendations()` to use custom prompts
   - [ ] Use `temperature` from config

3. **Update UI**:
   - [ ] Extend `templates/settings/ai.html` with new form fields
   - [ ] Add JavaScript for temperature slider
   - [ ] Add collapsible sections for prompts

4. **Update Route Handler**:
   - [ ] Update `save_ai_settings()` in `app/routers/settings/ai.py`
   - [ ] Add new form parameters
   - [ ] Save to database

5. **Update Homepage**:
   - [ ] Modify `app/routers/root.py` to pass AI config flags
   - [ ] Update `templates/root.html` to conditionally show AI sections

6. **Test**:
   - [ ] Toggle AI on/off
   - [ ] Toggle individual sections
   - [ ] Adjust temperature and verify
   - [ ] Edit prompts and verify changes

### Phase 2: Homepage Categories (3-4 hours)

7. **Database**:
   - [ ] Add `HomepageCategory` model to `app/internal/models.py`
   - [ ] Create migration: `alembic revision -m "add_homepage_categories"`
   - [ ] Edit migration to create table and insert defaults
   - [ ] Run migration: `alembic upgrade head`

8. **Backend Logic**:
   - [ ] Modify `get_homepage_recommendations_async()` in `app/util/recommendations.py`
   - [ ] Load categories from database instead of hardcoding

9. **New Router**:
   - [ ] Create `app/routers/settings/homepage.py`
   - [ ] Implement all endpoints (GET, POST, PATCH, DELETE, reorder)
   - [ ] Register router in `app/main.py`

10. **Templates**:
    - [ ] Create `templates/settings/homepage.html`
    - [ ] Create `templates/components/category_manager.html`
    - [ ] Update `templates/root.html` to use database categories

11. **Update Homepage Route**:
    - [ ] Modify `app/routers/root.py` to load and pass categories
    - [ ] Ensure categories are passed to template

12. **Test**:
    - [ ] Add custom category
    - [ ] Edit category search terms
    - [ ] Enable/disable categories
    - [ ] Reorder with drag-and-drop
    - [ ] Delete custom category
    - [ ] Verify homepage displays correctly

### Phase 3: Polish & Documentation (1 hour)

13. **Add Links**:
    - [ ] Add link to homepage settings in main settings page
    - [ ] Add link to AI settings from homepage settings

14. **Error Handling**:
    - [ ] Add validation for search terms (at least 1 required)
    - [ ] Handle duplicate slugs gracefully
    - [ ] Show success/error messages

15. **Documentation**:
    - [ ] Update README with new features
    - [ ] Add screenshots/examples
    - [ ] Document AI prompt customization

---

## Testing Guide

### AI Feature Testing

**Test Enable/Disable**:
1. Go to `/settings/ai`
2. Uncheck "Enable AI Features"
3. Save
4. Visit homepage - no AI sections should appear
5. Re-enable and verify sections return

**Test Section Visibility**:
1. Disable "Show AI Category Sections"
2. Homepage should still show "Because You Liked" but not category sections
3. Disable "Show 'Because You Liked' Recommendations"
4. No AI sections should appear

**Test Temperature**:
1. Set temperature to 0.0 (very deterministic)
2. Clear AI cache: `DELETE FROM config WHERE key LIKE 'ai_cache%'`
3. Refresh homepage multiple times - recommendations should be identical
4. Set temperature to 1.0 (very creative)
5. Clear cache again
6. Refresh - recommendations should vary more

**Test Custom Prompts**:
1. Edit category system prompt to: "You suggest book categories focused on mysteries."
2. Save and clear cache
3. Homepage AI sections should lean heavily toward mystery/thriller genres

### Homepage Category Testing

**Test Add Category**:
1. Go to `/settings/homepage`
2. Add category "Space Opera" with terms: "space opera, galactic empire, space battles"
3. Add emoji: 🚀
4. Save
5. Visit homepage - new section should appear with relevant books

**Test Edit Category**:
1. Edit "Trending This Week"
2. Change search terms to: "2025, january 2025, new this year"
3. Save
4. Homepage should show different books

**Test Disable Category**:
1. Toggle off "Biography & Memoir"
2. Homepage should not show biography section
3. Toggle back on - section reappears

**Test Reorder**:
1. Drag "Science & Technology" to top of list
2. Homepage should show science section first (after personalized sections)

**Test Delete**:
1. Create a test category
2. Delete it
3. Homepage should not show it
4. Try to delete "Trending This Week" (default) - should fail with error

**Test Edge Cases**:
1. Add category with no search terms - should show error
2. Add category with 50 search terms - should work but may be slow
3. Add category with special characters in title - should sanitize slug
4. Disable all categories - homepage should only show personalized sections

---

## Troubleshooting

### Issue: AI settings not saving

**Cause**: Form field names don't match route parameters

**Fix**: Check that HTML `name=""` attributes match `Form()` parameters in `save_ai_settings()`

### Issue: Homepage shows no categories

**Cause**: Migration didn't insert default categories or all disabled

**Solution**:
```sql
-- Check if categories exist
SELECT * FROM homepagecategory;

-- Re-enable all
UPDATE homepagecategory SET enabled = 1;
```

### Issue: Drag-and-drop not working

**Cause**: SortableJS not loaded

**Fix**: Verify CDN link in `templates/settings/homepage.html`:
```html
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
```

### Issue: Custom categories not showing on homepage

**Cause**: `get_homepage_recommendations_async()` still using hardcoded dict

**Fix**: Ensure database loading code is in place:
```python
db_categories = session.exec(
    select(HomepageCategory).where(HomepageCategory.enabled == True)...
```

### Issue: AI prompts reverting to default

**Cause**: Empty strings being saved instead of NULL

**Fix**: Use the delete logic:
```python
if category_system_prompt and category_system_prompt.strip():
    indexer_configuration_cache.set(...)
else:
    indexer_configuration_cache.delete(...)
```

### Issue: Migration fails with "table already exists"

**Cause**: Table was manually created or migration ran twice

**Solution**:
```bash
# Check current migration state
alembic current

# Downgrade and re-run
alembic downgrade -1
alembic upgrade head
```

### Issue: Books not showing for custom category

**Cause**: Search terms too specific or Audible API not returning results

**Debug**:
```python
# Test search terms directly
from app.util.recommendations import get_category_books
books = await get_category_books(audible_api, ["your", "search", "terms"])
print(f"Found {len(books)} books")
```

---

## Code Review Checklist

Before marking as complete:

- [ ] All database fields are properly indexed (slug on HomepageCategory)
- [ ] Foreign key constraints are set (created_by references user.username)
- [ ] All forms have CSRF protection (FastAPI handles this automatically)
- [ ] Admin-only routes check `user.is_admin()`
- [ ] Default categories have `is_default=True` to prevent deletion
- [ ] Custom prompts handle empty/None values gracefully
- [ ] Temperature is validated (0.0 - 1.0 range)
- [ ] Search terms are validated (at least 1 required)
- [ ] Emoji field has maxlength to prevent UI breaking
- [ ] All HTMX endpoints return proper fragments
- [ ] Drag-and-drop re-initializes after HTMX swap
- [ ] Success messages shown after save operations
- [ ] Error messages shown for validation failures

---

## Additional Notes

### Performance Considerations

- Category list is loaded on every homepage request - consider caching with 5-minute TTL
- AI generation is async and non-blocking - no impact on initial page load
- Audible searches for each category are parallel - limited by Audible API rate limits

### Future Enhancements

1. **Per-User Category Preferences**: Allow users to hide categories they don't like
2. **Category Analytics**: Track which categories get most clicks
3. **A/B Testing**: Test different search terms to optimize results
4. **Schedule-Based Categories**: "New This Month" updates search terms automatically
5. **AI-Powered Categories**: Let AI generate custom categories for each user

### Security Notes

- All admin routes protected by `user.is_admin()` check
- SQL injection prevented by SQLModel parameterized queries
- XSS prevented by Jinja2 auto-escaping
- No file uploads, so no file security concerns

---

## Questions?

If you encounter issues during implementation:

1. Check logs: `docker logs audiobookrequest` or check console output
2. Verify database state: `sqlite3 config/db.sqlite`
3. Test API endpoints directly with curl/Postman
4. Check browser console for HTMX errors
5. Verify all imports are present

**Common Import Issues**:
```python
# Make sure these are imported where needed
from app.internal.models import HomepageCategory
from sqlmodel import select
import uuid
```

Good luck with the implementation! 🚀
