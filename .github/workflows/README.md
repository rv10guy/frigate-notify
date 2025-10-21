# GitHub Actions Workflows

This directory contains automated CI/CD workflows for Frigate Notify.

## Workflows

### 1. `release.yml` - Automated Releases

**Trigger:** Automatically runs when code is merged to `main` branch

**What it does:**
1. Analyzes commit messages to determine version bump type
2. Creates a new git tag with the bumped version
3. Generates a changelog from commit messages
4. Creates a GitHub Release

**Version Bumping Rules:**
- Commit message starts with `feat:` or `feature:` → **Minor** version bump (v2.0.0 → v2.1.0)
- Commit message starts with `fix:` or `bugfix:` → **Patch** version bump (v2.0.0 → v2.0.1)
- Commit message contains `BREAKING CHANGE:` → **Major** version bump (v2.0.0 → v3.0.0)
- No prefix → Patch version bump (default)

**Examples:**
```bash
# Minor version bump (v2.0.0 → v2.1.0)
git commit -m "feat: Add new notification feature"

# Patch version bump (v2.0.0 → v2.0.1)
git commit -m "fix: Resolve MQTT connection issue"

# Major version bump (v2.0.0 → v3.0.0)
git commit -m "feat: Redesign API

BREAKING CHANGE: API endpoints have changed"
```

---

### 2. `docker-build.yml` - Docker Image Build & Push

**Trigger:** Automatically runs when a new release is published

**What it does:**
1. Builds Docker image for linux/amd64 platform
2. Tags the image with multiple tags:
   - Specific version (e.g., `v2.1.0`)
   - Major.minor version (e.g., `v2.1`)
   - Major version (e.g., `v2`)
   - `latest` (for the most recent release)
3. Pushes to GitHub Container Registry (ghcr.io)

**Image Location:**
```
ghcr.io/rv10guy/frigate-notify:latest
ghcr.io/rv10guy/frigate-notify:v2
ghcr.io/rv10guy/frigate-notify:v2.1
ghcr.io/rv10guy/frigate-notify:v2.1.0
```

**Manual Trigger:**
You can also manually trigger the Docker build from the Actions tab in GitHub.

---

### 3. `pr-checks.yml` - Pull Request Validation

**Trigger:** Runs when a PR is opened or updated

**What it does:**
1. Validates Python syntax in `frigatenotify.py`
2. Checks that all required files are present
3. Validates Dockerfile configuration
4. Provides feedback on PR status

**Purpose:** Ensures code quality before merge

---

## How the Release Process Works

```
Developer creates PR → PR Checks run → Review & Approve → Merge to main
                                                                ↓
                                                        release.yml triggers
                                                                ↓
                                                    Analyzes commit messages
                                                                ↓
                                                    Creates new version tag
                                                                ↓
                                                    Creates GitHub Release
                                                                ↓
                                                    docker-build.yml triggers
                                                                ↓
                                                    Builds Docker image
                                                                ↓
                                                    Pushes to ghcr.io
                                                                ↓
                                            Users can pull: ghcr.io/rv10guy/frigate-notify:latest
```

---

## First-Time Setup

### Enable GitHub Container Registry

The workflows are already configured! GitHub will automatically:
1. Build the Docker image
2. Push it to `ghcr.io/rv10guy/frigate-notify`
3. Make it available for pulling

### Make Package Public (Recommended)

After the first workflow run:

1. Go to: https://github.com/rv10guy?tab=packages
2. Click on `frigate-notify` package
3. Click "Package settings" (right sidebar)
4. Scroll to "Danger Zone"
5. Click "Change visibility" → "Public"

This allows anyone to pull the image without authentication.

---

## Using the Published Docker Images

### Pull the Latest Version

```bash
docker pull ghcr.io/rv10guy/frigate-notify:latest
```

### Update docker-compose.yml

Instead of building locally, use the published image:

```yaml
services:
  frigate-notify:
    image: ghcr.io/rv10guy/frigate-notify:latest  # Use published image
    # Remove the 'build: .' line
    container_name: frigate-notify
    restart: unless-stopped
    # ... rest of config
```

### Pin to Specific Version

For production stability, pin to a specific version:

```yaml
services:
  frigate-notify:
    image: ghcr.io/rv10guy/frigate-notify:v2.1.0  # Pin to specific version
    # ... rest of config
```

### Version Tag Options

- `latest` - Always the newest release (auto-updates)
- `v2` - Latest v2.x.x release (gets minor/patch updates)
- `v2.1` - Latest v2.1.x release (gets patch updates only)
- `v2.1.0` - Specific version (never changes)

---

## Monitoring Workflows

### View Workflow Runs

1. Go to the "Actions" tab in GitHub
2. See all workflow runs and their status
3. Click on a run to see detailed logs

### Workflow Status Badges

Add these to your README.md:

```markdown
![Release](https://github.com/rv10guy/frigate-notify/actions/workflows/release.yml/badge.svg)
![Docker Build](https://github.com/rv10guy/frigate-notify/actions/workflows/docker-build.yml/badge.svg)
![PR Checks](https://github.com/rv10guy/frigate-notify/actions/workflows/pr-checks.yml/badge.svg)
```

---

## Troubleshooting

### Workflow Failed

Check the Actions tab for error logs. Common issues:

1. **Docker build fails**: Check Dockerfile syntax
2. **Permission denied**: Ensure repository has `packages: write` permission
3. **Version conflict**: Delete problematic tag and re-run

### Manual Release

If you need to manually create a release:

```bash
# Create and push a tag
git tag v2.1.0
git push origin v2.1.0

# This will trigger the docker-build workflow
```

### Rebuild Docker Image

To rebuild the Docker image for an existing release:

1. Go to Actions tab
2. Click "Build and Push Docker Image" workflow
3. Click "Run workflow"
4. Select the branch/tag
5. Click "Run workflow" button

---

## Security

- GitHub automatically provides `GITHUB_TOKEN` for authentication
- No manual secrets needed for ghcr.io
- Images are scanned for vulnerabilities (if you enable it in settings)

---

## Best Practices

### Commit Message Format

Use conventional commit format for automatic versioning:

```bash
# Feature (minor bump)
git commit -m "feat: add multi-camera support"

# Bug fix (patch bump)
git commit -m "fix: resolve database connection issue"

# Breaking change (major bump)
git commit -m "feat: redesign configuration format

BREAKING CHANGE: config.yaml format has changed"

# Other prefixes
git commit -m "docs: update README"
git commit -m "chore: update dependencies"
git commit -m "refactor: reorganize code structure"
```

### Testing Before Release

1. Create PR with your changes
2. PR checks run automatically
3. Review and test locally
4. Merge when ready → automatic release!

---

## What Happens on This PR

When you merge this PR to main:

1. ✅ PR checks validate the code
2. ✅ Merge to main triggers release workflow
3. ✅ Creates release v2.0.0 (initial version)
4. ✅ Triggers Docker build
5. ✅ Pushes image to ghcr.io/rv10guy/frigate-notify:v2.0.0
6. ✅ Tags as :latest
7. ✅ Ready to use!

Then you can update your deployment to use the published image instead of building locally.
