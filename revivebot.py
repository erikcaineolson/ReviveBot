#!/usr/bin/env python3
"""
ReviveBot - Bulk banner creation and zone linking for Revive Ad Server.

Reads images from a folder, automates the Revive Ad Server admin UI
via Playwright to create an advertiser, campaign(s), banners, and link
each banner to all zones matching its pixel dimensions.
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from PIL import Image
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

DEFAULT_ZONES = [
    {'name': 'Wide Skyscraper (160x600)', 'size': 'IAB Wide Skyscraper (160 x 600)'},
    {'name': 'Medium Rectangle (300x250)', 'size': 'IAB Medium Rectangle (300 x 250)'},
    {'name': 'Leaderboard (728x90)', 'size': 'IAB Leaderboard (728 x 90)'},
]


def scan_images(folder: Path) -> list[dict]:
    """Scan a folder for image files and return metadata including dimensions."""
    images = []
    for filepath in sorted(folder.iterdir()):
        if not filepath.is_file():
            continue
        if filepath.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            with Image.open(filepath) as img:
                width, height = img.size
        except Exception as e:
            print(f"  WARNING: Could not read {filepath.name}: {e}")
            continue
        images.append({
            'path': filepath,
            'name': filepath.stem,
            'width': width,
            'height': height,
        })
    return images


def parse_size_text(size_text: str) -> tuple[int, int] | None:
    """Parse zone size from text like 'Leaderboard (728x90)' or 'IAB Leaderboard (728 x 90)'."""
    match = re.search(r'\((\d+)\s*x\s*(\d+)\)', size_text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def extract_id_from_url(url: str, param: str) -> int | None:
    """Extract an integer ID parameter from a URL."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    values = params.get(param, [])
    if values:
        return int(values[0])
    return None


class ReviveBot:
    """Automates the Revive Ad Server admin UI via Playwright."""

    def __init__(self, base_url: str, username: str, password: str, headless: bool = True):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.page = None

    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.page = self.browser.new_page()

    def stop(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def login(self):
        self.page.goto(self._url('/'))
        self.page.wait_for_load_state('networkidle')
        self.page.locator('#username').fill(self.username)
        self.page.locator('#password').fill(self.password)
        self.page.get_by_role('button', name='Login').click()
        self.page.wait_for_load_state('networkidle')

        if 'advertiser-index.php' not in self.page.url and 'plugins/apDashboard' not in self.page.url:
            raise RuntimeError(f"Login may have failed. Current URL: {self.page.url}")

    # -- Advertiser -----------------------------------------------------------

    def create_advertiser(self, name: str) -> int:
        self.page.goto(self._url('advertiser-edit.php'))
        self.page.wait_for_load_state('networkidle')

        self.page.locator('input[name="clientname"]').fill(name)
        self.page.get_by_role('button', name='Save changes').click()
        self.page.wait_for_load_state('networkidle')

        client_id = extract_id_from_url(self.page.url, 'clientid')
        if not client_id:
            raise RuntimeError(f"Could not extract clientid after creating advertiser. URL: {self.page.url}")
        return client_id

    # -- Campaign -------------------------------------------------------------

    def create_campaign(self, client_id: int, name: str) -> int:
        self.page.goto(self._url(f'campaign-edit.php?clientid={client_id}'))
        self.page.wait_for_load_state('networkidle')

        self.page.locator('input[name="campaignname"]').fill(name)
        self.page.get_by_role('button', name='Save changes').click()
        self.page.wait_for_load_state('networkidle')

        campaign_id = extract_id_from_url(self.page.url, 'campaignid')
        if not campaign_id:
            raise RuntimeError(f"Could not extract campaignid after creating campaign. URL: {self.page.url}")
        return campaign_id

    # -- Banner ---------------------------------------------------------------

    def create_banner(
        self,
        client_id: int,
        campaign_id: int,
        name: str,
        image_path: Path,
        click_url: str = '',
        alt_text: str = '',
        weight: int = 1,
    ) -> int:
        self.page.goto(self._url(
            f'banner-edit.php?clientid={client_id}&campaignid={campaign_id}'
        ))
        self.page.wait_for_load_state('networkidle')

        # Fill banner name
        self.page.locator('input[name="description"]').fill(name)

        # Upload image file
        with self.page.expect_file_chooser() as fc_info:
            self.page.get_by_role('button', name='Choose File').click()
        file_chooser = fc_info.value
        file_chooser.set_files(str(image_path))

        # Set click-through URL
        if click_url:
            url_input = self.page.locator('input[name="url"]')
            url_input.clear()
            url_input.fill(click_url)

        # Set alt text
        if alt_text:
            alt_input = self.page.locator('input[name="alt"]')
            alt_input.clear()
            alt_input.fill(alt_text)

        # Always set target to _blank
        target_input = self.page.locator('input[name="target"]')
        target_input.clear()
        target_input.fill('_blank')

        # Set weight
        if weight != 1:
            weight_input = self.page.locator('input[name="weight"]')
            weight_input.clear()
            weight_input.fill(str(weight))

        # Submit
        self.page.get_by_role('button', name='Save changes').click()
        self.page.wait_for_load_state('networkidle')

        banner_id = extract_id_from_url(self.page.url, 'bannerid')
        if not banner_id:
            raise RuntimeError(f"Could not extract bannerid after creating banner. URL: {self.page.url}")
        return banner_id

    def get_banners_in_campaign(self, client_id: int, campaign_id: int) -> list[dict]:
        """Get all banners in a campaign with their IDs."""
        self.page.goto(self._url(
            f'campaign-banners.php?clientid={client_id}&campaignid={campaign_id}'
        ))
        self.page.wait_for_load_state('networkidle')

        banners = []
        rows = self.page.locator('table tbody:nth-child(2) tr').all()
        for row in rows:
            cells = row.locator('td').all()
            if not cells:
                continue
            first_cell_text = cells[0].inner_text().strip()
            banner_id_match = re.search(r'\[(\d+)\]', first_cell_text)
            if not banner_id_match:
                continue
            banner_id = int(banner_id_match.group(1))
            banner_name = re.sub(r'\s*\[\d+\]', '', first_cell_text).strip()
            banners.append({
                'bannerid': banner_id,
                'name': banner_name,
            })
        return banners

    def update_banner(
        self,
        client_id: int,
        campaign_id: int,
        banner_id: int,
        click_url: str | None = None,
        alt_text: str | None = None,
        target: str | None = None,
        weight: int | None = None,
    ) -> bool:
        """Update an existing banner's properties."""
        self.page.goto(self._url(
            f'banner-edit.php?clientid={client_id}&campaignid={campaign_id}&bannerid={banner_id}'
        ))
        self.page.wait_for_load_state('networkidle')

        changed = False

        if click_url is not None:
            url_input = self.page.locator('input[name="url"]')
            url_input.clear()
            url_input.fill(click_url)
            changed = True

        if alt_text is not None:
            alt_input = self.page.locator('input[name="alt"]')
            alt_input.clear()
            alt_input.fill(alt_text)
            changed = True

        if target is not None:
            target_input = self.page.locator('input[name="target"]')
            target_input.clear()
            target_input.fill(target)
            changed = True

        if weight is not None:
            weight_input = self.page.locator('input[name="weight"]')
            weight_input.clear()
            weight_input.fill(str(weight))
            changed = True

        if changed:
            self.page.get_by_role('button', name='Save changes').click()
            self.page.wait_for_load_state('networkidle')

        return changed

    # -- Zones ----------------------------------------------------------------

    def create_website(self, url: str, name: str = '', contact: str = '', email: str = '') -> int:
        """Create a new website (publisher). Returns the affiliate ID."""
        self.page.goto(self._url('affiliate-edit.php'))
        self.page.wait_for_load_state('networkidle')

        url_input = self.page.locator('input[name="website"]')
        url_input.clear()
        url_input.fill(url)

        # Clear and fill name (JS may auto-populate from URL)
        name_input = self.page.locator('input[name="name"]')
        name_input.clear()
        if name:
            name_input.fill(name)

        # Contact and email are required by the form
        self.page.locator('input[name="contact"]').fill(contact or name or url)
        self.page.locator('input[name="email"]').fill(email or 'admin@' + url.replace('http://', '').replace('https://', '').split('/')[0])

        self.page.get_by_role('button', name='Save changes').click()
        self.page.wait_for_load_state('networkidle')

        # Try extracting from URL first (edit page redirect)
        affiliate_id = extract_id_from_url(self.page.url, 'affiliateid')
        if affiliate_id:
            return affiliate_id

        # If redirected to website list, find the newly created website
        display_name = name or url.replace('http://', '').replace('https://', '').rstrip('/')
        rows = self.page.locator('table tbody:nth-child(2) tr').all()
        for row in rows:
            first_cell = row.locator('td').first
            cell_text = first_cell.inner_text().strip()
            if display_name in cell_text:
                id_match = re.search(r'\[(\d+)\]', cell_text)
                if id_match:
                    return int(id_match.group(1))

        raise RuntimeError(f"Could not find affiliateid after creating website. URL: {self.page.url}")

    def create_zone(self, affiliate_id: int, name: str, size_label: str) -> int:
        """Create a new zone for a website. size_label should match an IAB dropdown option."""
        self.page.goto(self._url(f'zone-edit.php?affiliateid={affiliate_id}'))
        self.page.wait_for_load_state('networkidle')

        # Fill zone name
        name_input = self.page.locator('input[name="zonename"]')
        name_input.clear()
        name_input.fill(name)

        # Select IAB size from dropdown
        size_select = self.page.locator('select[name="size"]')
        size_select.select_option(label=size_label)

        self.page.get_by_role('button', name='Save Changes').click()
        self.page.wait_for_load_state('networkidle')

        # Try extracting from URL first (edit page redirect)
        zone_id = extract_id_from_url(self.page.url, 'zoneid')
        if zone_id:
            return zone_id

        # If redirected to zone list, find the newly created zone
        rows = self.page.locator('table tbody:nth-child(2) tr').all()
        for row in rows:
            first_cell = row.locator('td').first
            cell_text = first_cell.inner_text().strip()
            if name in cell_text:
                id_match = re.search(r'\[(\d+)\]', cell_text)
                if id_match:
                    return int(id_match.group(1))

        raise RuntimeError(f"Could not find zoneid after creating zone. URL: {self.page.url}")

    def create_website_with_default_zones(self, website_url: str, name: str = '') -> dict:
        """Create a website and its standard set of zones (160x600, 300x250, 728x90)."""
        display_name = name or website_url.replace('http://', '').replace('https://', '').rstrip('/')

        print(f"\n  Creating website: {display_name}")
        affiliate_id = self.create_website(website_url, name=display_name)
        print(f"    Created website ID: {affiliate_id}")

        zones = []
        for zone_def in DEFAULT_ZONES:
            zone_name = f"{display_name} - {zone_def['name']}"
            print(f"    Creating zone: {zone_name}")
            zone_id = self.create_zone(affiliate_id, zone_name, zone_def['size'])
            print(f"      Created zone ID: {zone_id}")
            zones.append({'zoneid': zone_id, 'name': zone_name})

        return {'affiliateid': affiliate_id, 'name': display_name, 'zones': zones}

    def strip_quotes_on_website(self, affiliate_id: int) -> bool:
        """Navigate to a website's edit page and strip quotes from name and contact fields."""
        self.page.goto(self._url(f'affiliate-edit.php?affiliateid={affiliate_id}'))
        self.page.wait_for_load_state('networkidle')

        changed = False
        for field_name in ('name', 'contact'):
            field = self.page.locator(f'input[name="{field_name}"]')
            value = field.input_value()
            cleaned = value.replace('"', '')
            if cleaned != value:
                field.clear()
                field.fill(cleaned)
                print(f"    {field_name}: \"{value}\" -> \"{cleaned}\"")
                changed = True

        if changed:
            self.page.get_by_role('button', name='Save changes').click()
            self.page.wait_for_load_state('networkidle')

        return changed

    def setup_zones_for_website(self, affiliate_id: int, website_name: str) -> list[dict]:
        """Ensure default zones exist for a website. Only creates missing ones."""
        existing_zones = self.get_zones(affiliate_id)
        existing_sizes = set()
        for z in existing_zones:
            existing_sizes.add((z['width'], z['height']))

        created = []
        for zone_def in DEFAULT_ZONES:
            size = parse_size_text(zone_def['size'])
            if size and size in existing_sizes:
                print(f"    {zone_def['name']} - already exists, skipping")
                continue

            zone_name = f"{website_name} - {zone_def['name']}"
            print(f"    Creating zone: {zone_name}")
            zone_id = self.create_zone(affiliate_id, zone_name, zone_def['size'])
            print(f"      Created zone ID: {zone_id}")
            created.append({'zoneid': zone_id, 'name': zone_name})

        return created

    def get_websites(self) -> list[dict]:
        """Get all websites (publishers) and their affiliate IDs."""
        self.page.goto(self._url('website-index.php'))
        self.page.wait_for_load_state('networkidle')

        websites = []
        rows = self.page.locator('table tbody tr').all()
        for row in rows:
            link = row.locator('a').first
            href = link.get_attribute('href') or ''
            affiliate_id = extract_id_from_url(self.base_url + '/' + href, 'affiliateid')
            if affiliate_id:
                name = link.inner_text().strip()
                websites.append({'affiliateid': affiliate_id, 'name': name})
        return websites

    def get_zones(self, affiliate_id: int) -> list[dict]:
        """Get all zones for a website, including their sizes."""
        self.page.goto(self._url(f'affiliate-zones.php?affiliateid={affiliate_id}'))
        self.page.wait_for_load_state('networkidle')

        zones = []
        data_rows = self.page.locator('table tbody:nth-child(2) tr').all()

        for row in data_rows:
            cells = row.locator('td').all()
            if len(cells) < 2:
                continue

            first_cell_text = cells[0].inner_text().strip()
            zone_id_match = re.search(r'\[(\d+)\]', first_cell_text)
            if not zone_id_match:
                continue
            zone_id = int(zone_id_match.group(1))
            zone_name = re.sub(r'\s*\[\d+\]', '', first_cell_text).strip()

            size_text = cells[1].inner_text().strip()
            size = parse_size_text(size_text)
            if not size:
                continue

            zones.append({
                'zoneid': zone_id,
                'name': zone_name,
                'width': size[0],
                'height': size[1],
                'affiliateid': affiliate_id,
            })

        return zones

    def get_all_zones(self) -> list[dict]:
        """Get all zones across all websites."""
        websites = self.get_websites()
        all_zones = []
        for ws in websites:
            zones = self.get_zones(ws['affiliateid'])
            all_zones.extend(zones)
        return all_zones

    def link_campaign_to_zones(
        self,
        client_id: int,
        campaign_id: int,
        target_sizes: set[tuple[int, int]] | None = None,
    ) -> int:
        """Link a campaign to matching zones via campaign-zone.php.

        Uses the Available/Linked Zones two-panel UI on the campaign page.
        If target_sizes is provided, only zones matching those (w, h) are linked.
        If None, all available zones are linked.

        Returns the number of zones linked.
        """
        self.page.goto(self._url(
            f'campaign-zone.php?clientid={client_id}&campaignid={campaign_id}'
        ))
        self.page.wait_for_load_state('networkidle')

        # The page has two tables side by side:
        #   Left:  Available Zones (first table with checkboxes)
        #   Right: Linked Zones (second table with checkboxes)
        # Below them: "Link" and "Unlink" buttons

        # Find all checkboxes in the Available Zones panel (left side).
        # Available zones are in the first table within the two-panel container.
        # Each zone row has a checkbox and a label like "Leaderboard (728x90)".
        # Website rows also have checkboxes but no size in parentheses.

        # The available zones table is the first one; we identify zone checkboxes
        # by their label containing a size pattern like "(WxH)" or "(W x H)".
        available_table = self.page.locator('table').first
        available_checkboxes = available_table.get_by_role('checkbox').all()

        linked_count = 0
        checked_any = False

        for cb in available_checkboxes:
            # Get the label text - it's in a sibling text node or parent element
            parent = cb.locator('..')
            label_text = parent.inner_text().strip()

            # Skip "Select / Unselect All" checkbox
            if 'select' in label_text.lower() and 'all' in label_text.lower():
                continue

            # Skip website-level checkboxes (no size in parens)
            size = parse_size_text(label_text)

            if target_sizes is not None:
                # Only check zones matching our target sizes
                if size and size in target_sizes:
                    cb.check()
                    checked_any = True
                    linked_count += 1
                    print(f"    Selecting: {label_text}")
            else:
                # Link all available zones
                if size:
                    cb.check()
                    checked_any = True
                    linked_count += 1
                    print(f"    Selecting: {label_text}")

        if checked_any:
            link_button = self.page.get_by_role('button', name='Link')
            link_button.click()
            self.page.wait_for_load_state('networkidle')
            print(f"    Clicked Link.")
        else:
            print(f"    No matching available zones to link.")

        return linked_count


def run_create(args, bot, revive_url):
    """Create advertisers, campaigns, and banners from images."""
    if not args.image_folder or not args.image_folder.is_dir():
        print(f"ERROR: {args.image_folder} is not a directory")
        sys.exit(1)

    # -- Scan images ----------------------------------------------------------
    print(f"\nScanning images in: {args.image_folder}")
    images = scan_images(args.image_folder)

    if not images:
        print("No supported images found. Supported formats:", ', '.join(SUPPORTED_EXTENSIONS))
        sys.exit(0)

    print(f"Found {len(images)} image(s):\n")

    size_groups = defaultdict(list)
    for img in images:
        size_groups[(img['width'], img['height'])].append(img)
        print(f"  {img['path'].name:40s}  {img['width']}x{img['height']}")

    print(f"\nSize groups: {len(size_groups)}")
    for (w, h), group in size_groups.items():
        print(f"  {w}x{h}: {len(group)} image(s)")

    if args.dry_run:
        print("\n[DRY RUN] No changes will be made.")
        if args.advertiser_id:
            print(f"  Would use existing advertiser ID: {args.advertiser_id}")
        else:
            print(f"  Would create advertiser: {args.advertiser}")
        if args.campaign_id:
            print(f"  Would use existing campaign ID: {args.campaign_id}")
        else:
            for (w, h) in size_groups:
                name = args.campaign or f"{w}x{h} Banners"
                print(f"  Would create campaign: {name}")
        print(f"  Would create {len(images)} banner(s) (target=_blank)")
        if not args.skip_zone_link:
            print(f"  Would link campaigns to zones matching their dimensions")
        sys.exit(0)

    # -- Login ----------------------------------------------------------------
    print(f"\nLogging in to {revive_url}...")
    bot.login()
    print("  Logged in successfully.")

    # -- Create or use advertiser ---------------------------------------------
    if args.advertiser_id:
        client_id = args.advertiser_id
        print(f"\nUsing existing advertiser ID: {client_id}")
    else:
        print(f"\nCreating advertiser: {args.advertiser}")
        client_id = bot.create_advertiser(args.advertiser)
        print(f"  Created advertiser ID: {client_id}")

    # -- Create campaigns -----------------------------------------------------
    created_banners = []
    campaign_by_size = {}

    if args.campaign_id:
        for (w, h) in size_groups:
            campaign_by_size[(w, h)] = args.campaign_id
        print(f"Using existing campaign ID: {args.campaign_id}")
    else:
        if args.campaign and len(size_groups) == 1:
            (w, h) = list(size_groups.keys())[0]
            print(f"\nCreating campaign: {args.campaign}")
            campaign_id = bot.create_campaign(client_id, args.campaign)
            campaign_by_size[(w, h)] = campaign_id
            print(f"  Created campaign ID: {campaign_id}")
        else:
            for (w, h) in size_groups:
                name = args.campaign or f"{w}x{h} Banners"
                if len(size_groups) > 1:
                    name = f"{w}x{h} Banners"
                print(f"\nCreating campaign: {name}")
                campaign_id = bot.create_campaign(client_id, name)
                campaign_by_size[(w, h)] = campaign_id
                print(f"  Created campaign ID: {campaign_id}")

    # -- Create banners -------------------------------------------------------
    print(f"\nCreating {len(images)} banner(s)...")
    for img in images:
        size_key = (img['width'], img['height'])
        campaign_id = campaign_by_size[size_key]
        print(f"\n  [{img['path'].name}] {img['width']}x{img['height']}")

        try:
            banner_id = bot.create_banner(
                client_id=client_id,
                campaign_id=campaign_id,
                name=img['name'],
                image_path=img['path'],
                click_url=args.click_url,
                alt_text=args.alt_text,
                weight=args.weight,
            )
            print(f"    Created banner ID: {banner_id}")
            created_banners.append({
                'banner_id': banner_id,
                'name': img['name'],
                'width': img['width'],
                'height': img['height'],
                'campaign_id': campaign_id,
            })
        except Exception as e:
            print(f"    ERROR creating banner: {e}")
            continue

    # -- Link campaigns to zones ----------------------------------------------
    total_links = 0
    if not args.skip_zone_link:
        print("\nLinking campaigns to matching zones...")

        for (w, h), campaign_id in campaign_by_size.items():
            print(f"\n  Campaign {campaign_id} ({w}x{h}):")
            try:
                count = bot.link_campaign_to_zones(
                    client_id=client_id,
                    campaign_id=campaign_id,
                    target_sizes={(w, h)},
                )
                total_links += count
            except Exception as e:
                print(f"    ERROR linking: {e}")

    # -- Summary --------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Advertiser ID:   {client_id}")
    print(f"  Campaigns:       {len(campaign_by_size)}")
    for (w, h), cid in campaign_by_size.items():
        print(f"    {w}x{h} -> Campaign ID {cid}")
    print(f"  Banners created: {len(created_banners)}")
    if not args.skip_zone_link:
        print(f"  Zone links made: {total_links}")
    print()


def parse_websites_file(filepath: Path) -> list[dict]:
    """Parse a websites file. One site per line: url or url,name. Blank lines and # comments skipped."""
    sites = []
    for line in filepath.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ',' in line:
            url, name = line.split(',', 1)
            sites.append({'url': url.strip(), 'name': name.strip()})
        else:
            sites.append({'url': line, 'name': ''})
    return sites


def run_create_website(args, bot, revive_url):
    """Create website(s) with default zones."""
    # Build site list from --create-website or --create-websites
    sites = []
    if args.create_websites:
        if not args.create_websites.is_file():
            print(f"ERROR: {args.create_websites} is not a file")
            sys.exit(1)
        sites = parse_websites_file(args.create_websites)
    elif args.create_website:
        sites = [{'url': args.create_website, 'name': args.website_name or ''}]

    if not sites:
        print("ERROR: No websites to create.")
        sys.exit(1)

    if args.dry_run:
        print(f"\n[DRY RUN] Would create {len(sites)} website(s):")
        for site in sites:
            label = site['name'] or site['url']
            print(f"\n  {label}")
            for zone_def in DEFAULT_ZONES:
                print(f"    Zone: {zone_def['name']}")
        sys.exit(0)

    print(f"\nLogging in to {revive_url}...")
    bot.login()
    print("  Logged in successfully.")

    results = []
    for site in sites:
        try:
            result = bot.create_website_with_default_zones(site['url'], name=site['name'])
            results.append(result)
        except Exception as e:
            print(f"\n  ERROR creating {site['url']}: {e}")

    print("\n" + "=" * 60)
    print(f"WEBSITES CREATED: {len(results)}/{len(sites)}")
    print("=" * 60)
    for result in results:
        print(f"\n  {result['name']} (Affiliate ID: {result['affiliateid']})")
        for z in result['zones']:
            print(f"    {z['name']} (ID: {z['zoneid']})")
    print()


def run_strip_quotes(args, bot, revive_url):
    """Strip quote characters from all website names and contacts."""
    print(f"\nLogging in to {revive_url}...")
    bot.login()
    print("  Logged in successfully.")

    print("\nFetching websites...")
    websites = bot.get_websites()
    print(f"  Found {len(websites)} website(s)")

    updated = 0
    for ws in websites:
        print(f"\n  {ws['name']} (ID: {ws['affiliateid']}):")
        try:
            changed = bot.strip_quotes_on_website(ws['affiliateid'])
            if changed:
                updated += 1
            else:
                print(f"    No quotes found.")
        except Exception as e:
            print(f"    ERROR: {e}")

    print(f"\n  Updated {updated}/{len(websites)} website(s)")


def run_setup_zones(args, bot, revive_url):
    """Ensure all existing websites have the default zones."""
    print(f"\nLogging in to {revive_url}...")
    bot.login()
    print("  Logged in successfully.")

    print("\nFetching websites...")
    websites = bot.get_websites()
    print(f"  Found {len(websites)} website(s)")

    if args.dry_run:
        print("\n[DRY RUN] Would check each website for missing default zones:")
        for ws in websites:
            print(f"  {ws['name']} (ID: {ws['affiliateid']})")
        sys.exit(0)

    total_created = 0
    for ws in websites:
        print(f"\n  {ws['name']} (ID: {ws['affiliateid']}):")
        try:
            created = bot.setup_zones_for_website(ws['affiliateid'], ws['name'])
            total_created += len(created)
        except Exception as e:
            print(f"    ERROR: {e}")

    print("\n" + "=" * 60)
    print("ZONE SETUP COMPLETE")
    print("=" * 60)
    print(f"  Websites checked: {len(websites)}")
    print(f"  Zones created:    {total_created}")
    print()


def run_update(args, bot, revive_url):
    """Update existing banners in a campaign with new URL/target/weight."""
    if not args.advertiser_id:
        print("ERROR: --advertiser-id is required for --update-banners")
        sys.exit(1)
    if not args.campaign_id:
        print("ERROR: --campaign-id is required for --update-banners")
        sys.exit(1)

    client_id = args.advertiser_id
    campaign_id = args.campaign_id
    click_url = args.click_url or None
    alt_text = args.alt_text or None
    target = '_blank'
    weight = args.weight if args.weight != 1 else None

    parts = []
    if click_url:
        parts.append(f"url={click_url}")
    if alt_text:
        parts.append(f"alt={alt_text}")
    parts.append("target=_blank")
    if weight:
        parts.append(f"weight={weight}")
    print(f"Updating banners in campaign {campaign_id}: {', '.join(parts)}")

    if args.dry_run:
        print("[DRY RUN] No changes will be made.")
        sys.exit(0)

    # -- Login ----------------------------------------------------------------
    print(f"\nLogging in to {revive_url}...")
    bot.login()
    print("  Logged in successfully.")

    # -- Get banners ----------------------------------------------------------
    print(f"\nFetching banners in campaign {campaign_id}...")
    banners = bot.get_banners_in_campaign(client_id, campaign_id)
    print(f"  Found {len(banners)} banner(s)")

    if not banners:
        print("  No banners to update.")
        return

    # -- Update each banner ---------------------------------------------------
    updated = 0
    for banner in banners:
        print(f"\n  [{banner['name']}] (ID: {banner['bannerid']})")
        try:
            changed = bot.update_banner(
                client_id=client_id,
                campaign_id=campaign_id,
                banner_id=banner['bannerid'],
                click_url=click_url,
                alt_text=alt_text,
                target=target,
                weight=weight,
            )
            if changed:
                updated += 1
                print(f"    Updated.")
            else:
                print(f"    No changes needed.")
        except Exception as e:
            print(f"    ERROR: {e}")

    print(f"\n  Updated {updated}/{len(banners)} banner(s)")


def main():
    parser = argparse.ArgumentParser(
        description='Bulk-create or update Revive Ad Server banners from a folder of images.',
    )
    parser.add_argument(
        'image_folder',
        nargs='?',
        type=Path,
        default=None,
        help='Path to folder containing banner images (not needed with --update-banners)',
    )
    parser.add_argument(
        '--advertiser',
        default='ReviveBot Advertiser',
        help='Advertiser name to create (default: "ReviveBot Advertiser")',
    )
    parser.add_argument(
        '--advertiser-id',
        type=int,
        default=None,
        help='Use an existing advertiser ID (clientid) instead of creating one',
    )
    parser.add_argument(
        '--campaign',
        default=None,
        help='Campaign name (default: auto-named per size group, e.g. "728x90 Banners")',
    )
    parser.add_argument(
        '--campaign-id',
        type=int,
        default=None,
        help='Use an existing campaign ID instead of creating one',
    )
    parser.add_argument(
        '--click-url',
        default='',
        help='Click-through URL for all banners',
    )
    parser.add_argument(
        '--alt-text',
        default='',
        help='Alt text for all banners',
    )
    parser.add_argument(
        '--weight',
        type=int,
        default=1,
        help='Banner weight/priority (default: 1)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes',
    )
    parser.add_argument(
        '--skip-zone-link',
        action='store_true',
        help='Create banners but do not link them to zones',
    )
    parser.add_argument(
        '--headed',
        action='store_true',
        help='Run browser in headed mode (visible window) for debugging',
    )
    parser.add_argument(
        '--update-banners',
        action='store_true',
        help='Update existing banners in a campaign (requires --advertiser-id and --campaign-id)',
    )
    parser.add_argument(
        '--create-website',
        metavar='URL',
        default=None,
        help='Create a single website with default zones (160x600, 300x250, 728x90).',
    )
    parser.add_argument(
        '--create-websites',
        metavar='FILE',
        type=Path,
        default=None,
        help='Create websites from a file. One per line: url or url,name',
    )
    parser.add_argument(
        '--website-name',
        default=None,
        help='Display name for --create-website (default: derived from URL)',
    )
    parser.add_argument(
        '--setup-zones',
        action='store_true',
        help='Check all existing websites and add any missing default zones (160x600, 300x250, 728x90)',
    )
    parser.add_argument(
        '--strip-quotes',
        action='store_true',
        help='Remove quote characters from all website names and contacts',
    )

    args = parser.parse_args()
    load_dotenv()

    revive_url = os.getenv('REVIVE_URL')
    username = os.getenv('REVIVE_USERNAME')
    password = os.getenv('REVIVE_PASSWORD')

    if not all([revive_url, username, password]):
        print("ERROR: Missing required .env variables (REVIVE_URL, REVIVE_USERNAME, REVIVE_PASSWORD)")
        print("Copy .env.example to .env and fill in your values.")
        sys.exit(1)

    if not args.update_banners and not args.create_website and not args.create_websites and not args.setup_zones and not args.strip_quotes and not args.image_folder:
        print("ERROR: image_folder is required (or use --update-banners / --create-website / --create-websites / --setup-zones / --strip-quotes)")
        sys.exit(1)

    bot = ReviveBot(revive_url, username, password, headless=not args.headed)
    bot.start()

    try:
        if args.strip_quotes:
            run_strip_quotes(args, bot, revive_url)
        elif args.setup_zones:
            run_setup_zones(args, bot, revive_url)
        elif args.create_website or args.create_websites:
            run_create_website(args, bot, revive_url)
        elif args.update_banners:
            run_update(args, bot, revive_url)
        else:
            run_create(args, bot, revive_url)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        sys.exit(1)
    finally:
        bot.stop()


if __name__ == '__main__':
    main()
