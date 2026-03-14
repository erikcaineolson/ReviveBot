# ReviveBot

A Python CLI tool that automates [Revive Ad Server](https://www.revive-adserver.com/) management via browser automation. Bulk-create websites, zones, advertisers, campaigns, and banners from a folder of images, then link campaigns to zones matching their banner dimensions.

Works with the hosted edition (console.revive-adserver.net) — no plugins or API access required.

## Requirements

- Python 3.10+
- Revive Ad Server v4.x, v5.x, or v6.x (self-hosted or hosted)

## Installation

```bash
git clone <repo-url>
cd revivebot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `REVIVE_URL` | Base URL of your Revive admin console (e.g. `https://console.revive-adserver.net`) |
| `REVIVE_USERNAME` | Revive admin username |
| `REVIVE_PASSWORD` | Revive admin password |

## Usage

### Create Banners

```bash
python revivebot.py <image_folder> [options]
```

Scans a folder for images, creates an advertiser and one campaign per banner size, uploads each image as a banner, and links campaigns to matching zones.

```bash
# Basic - creates advertiser, campaigns, banners, and links zones
python revivebot.py ./banners --advertiser "My Client" --click-url "https://example.com" --alt-text "Visit us"

# Preview what would happen
python revivebot.py ./banners --advertiser "My Client" --dry-run

# Use existing advertiser, single campaign for all sizes
python revivebot.py ./banners --advertiser-id 17192 --campaign-id 62155

# Create banners without linking to zones
python revivebot.py ./banners --advertiser "My Client" --skip-zone-link

# Watch the browser work
python revivebot.py ./banners --advertiser "My Client" --headed
```

All banners are created with `target=_blank` so ad clicks open in a new tab.

### Update Existing Banners

Update the click URL, alt text, target, or weight on all banners in an existing campaign.

```bash
# Update URL and alt text on all banners in a campaign
python revivebot.py --update-banners --advertiser-id 17193 --campaign-id 62169 \
  --click-url "https://example.com" --alt-text "Check out our latest deals"

# Preview first
python revivebot.py --update-banners --advertiser-id 17193 --campaign-id 62169 \
  --click-url "https://example.com" --dry-run
```

Always sets `target=_blank` on every banner it touches.

### Create Websites

Create a website with the default set of zones (160x600, 300x250, 728x90).

```bash
# Single website
python revivebot.py --create-website "https://example.com" --website-name "Example Site"

# Batch from a file
python revivebot.py --create-websites sites.csv
```

The sites file is one entry per line: `url` or `url,name`. Lines starting with `#` are comments.

```
# sites.csv
https://example.com,Example Site
https://another-site.org,Another Site
https://bare-url.com
```

### Setup Zones

Check all existing websites and add any missing default zones. Non-destructive — only creates zones, never removes them.

```bash
# Ensure every website has 160x600, 300x250, and 728x90 zones
python revivebot.py --setup-zones

# Preview first
python revivebot.py --setup-zones --dry-run
```

### Strip Quotes

Remove stray double-quote characters from all website names and contacts.

```bash
python revivebot.py --strip-quotes
```

Apostrophes are preserved — only `"` characters are removed.

## CLI Reference

### Banner Creation Flags

| Flag | Description |
|---|---|
| `image_folder` | Path to folder containing banner images |
| `--advertiser NAME` | Advertiser name to create (default: `ReviveBot Advertiser`) |
| `--advertiser-id ID` | Use an existing advertiser instead of creating one |
| `--campaign NAME` | Campaign name (default: auto-named per size, e.g. `728x90 Banners`) |
| `--campaign-id ID` | Use an existing campaign instead of creating one |
| `--click-url URL` | Click-through URL for all banners |
| `--alt-text TEXT` | Alt text for all banners |
| `--weight N` | Banner weight/priority (default: `1`) |
| `--skip-zone-link` | Create banners but skip zone linking |

### Banner Update Flags

| Flag | Description |
|---|---|
| `--update-banners` | Update existing banners (requires `--advertiser-id` and `--campaign-id`) |
| `--click-url URL` | New click-through URL |
| `--alt-text TEXT` | New alt text |
| `--weight N` | New weight |

### Website & Zone Flags

| Flag | Description |
|---|---|
| `--create-website URL` | Create a single website with default zones |
| `--create-websites FILE` | Create websites from a CSV file |
| `--website-name NAME` | Display name for `--create-website` (default: derived from URL) |
| `--setup-zones` | Add missing default zones to all existing websites |
| `--strip-quotes` | Remove double-quote characters from all website names and contacts |

### General Flags

| Flag | Description |
|---|---|
| `--dry-run` | Preview what would happen without making changes |
| `--headed` | Run browser visibly for debugging |

## Supported Image Formats

`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`

Images that cannot be opened are skipped with a warning and do not halt the run.

## How It Works

ReviveBot uses [Playwright](https://playwright.dev/) to automate the Revive Ad Server admin UI. It logs in, navigates forms, fills fields, uploads files, and clicks buttons — just like a human would, but faster.

**Banner creation** groups images by pixel dimensions and creates one campaign per size group (e.g. `728x90 Banners`, `300x250 Banners`). This keeps zone linking clean.

**Zone linking** uses the campaign's "Linked Zones" page (`campaign-zone.php`). It selects matching zones from the "Available Zones" panel and clicks "Link".

**Default zones** are the three standard IAB sizes: Wide Skyscraper (160x600), Medium Rectangle (300x250), and Leaderboard (728x90).
