# Scanning Portal

Upload portal for FLP volunteer scanners. Single-app Django project where `scanning/` is both the project package and the only app.

## Quick Reference

```bash
# Run tests
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py test scanning.tests -v 2

# Run a single test class
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py test scanning.tests.TestScanUpload -v 2

# Generate migrations
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py makemigrations scanning

# Start dev environment
docker compose -f docker/scanning/docker-compose.yml up --build

# Install dependencies
uv sync --all-extras
```

## Project Structure

- `scanning/` is both the Django project (settings, urls, asgi, wsgi) and the single app (models, views, forms, admin)
- Settings are split into modules: `settings/django.py`, `settings/project/`, `settings/third_party/`
- Templates live in two places:
  - `scanning/assets/templates/` for base layout and cotton components
  - `scanning/templates/scanning/` for app-specific templates (login, upload, list, detail, etc.)
- Error pages (404.html, etc.) go in `scanning/assets/templates/` (the template root)

## Testing

- Use `django.test.TestCase`, NOT pytest style classes
- All tests live in `scanning/tests.py`
- Test classes inherit from `ScanningTestCase` which provides `make_user()`, `make_staff_user()`, `make_pdf()`, `make_image()` helpers
- Use `ScanFactory` and `UserFactory` from `scanning/factories.py` for test data
- Factory docstrings describe default declarations as a prose list, not `:param:` entries (they are class attributes, not `__init__` parameters)
- Use `skip_postgeneration_save = True` on factories and explicitly save changed fields in `@factory.post_generation` hooks

## Views

- All views are function-based with `@login_required`
- Auth views wrap Django's built-in `LoginView`/`LogoutView`
- Never pass `next` query strings directly into templates (open redirect risk). Let Django's auth views handle redirect validation internally.
- All authenticated users see all scans (no per-user filtering). Staff-only distinction is the review form on the detail page.

## Tailwind CSS

- Config: `scanning/assets/tailwind/tailwind.config.js`
- Input: `scanning/assets/tailwind/input.css`
- Output: `scanning/assets/static-global/css/tailwind_styles.css` (gitignored, built by npm)
- Component classes defined in `input.css`: `.btn-primary`, `.btn-outline`, `.btn-danger`, `.btn-ghost`, `.card`, `.input-text`, `.alert-*`, `.badge-*`
- Templates use cotton components: `<c-header />`, `<c-footer />`

## Environment

- `DEVELOPMENT=True` enables debug toolbar, local filesystem storage, dev S3 buckets
- `TESTING=True` is auto-detected from `sys.argv`, switches to LocMemCache, MD5 password hasher, disables debug toolbar URLs
- The `DB_SSL_MODE=prefer` env var is needed when running locally outside Docker (avoids SSL connection errors)
