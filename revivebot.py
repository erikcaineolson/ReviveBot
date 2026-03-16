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

    def create_advertiser(self, name: str, contact: str = '', email: str = '') -> int:
        self.page.goto(self._url('advertiser-edit.php'))
        self.page.wait_for_load_state('networkidle')

        self.page.locator('input[name="clientname"]').fill(name)

        # Fill contact and email if the fields exist (required on some setups)
        contact_field = self.page.locator('input[name="contact"]')
        if contact_field.count() > 0:
            contact_field.fill(contact or name)
        email_field = self.page.locator('input[name="email"]')
        if email_field.count() > 0:
            email_field.fill(email or f'{name.lower().replace(" ", "")}@example.com')

        self.page.get_by_role('button', name='Save changes').click()
        self.page.wait_for_load_state('networkidle')

        # Try extracting from URL first
        client_id = extract_id_from_url(self.page.url, 'clientid')
        if client_id:
            return client_id

        # If redirected to advertiser list, find the newly created advertiser
        rows = self.page.locator('table tbody:nth-child(2) tr').all()
        for row in rows:
            first_cell = row.locator('td').first
            cell_text = first_cell.inner_text().strip()
            if name in cell_text:
                id_match = re.search(r'\[(\d+)\]', cell_text)
                if id_match:
                    return int(id_match.group(1))

        raise RuntimeError(f"Could not find clientid after creating advertiser. URL: {self.page.url}")

    # -- Campaign -------------------------------------------------------------

    def create_campaign(self, client_id: int, name: str) -> int:
        self.page.goto(self._url(f'campaign-edit.php?clientid={client_id}'))
        self.page.wait_for_load_state('networkidle')

        self.page.locator('input[name="campaignname"]').fill(name)

        # Select campaign type (required) - default to Remnant (value="1")
        remnant_radio = self.page.locator('input[name="campaign_type"][value="1"]')
        if remnant_radio.count() > 0:
            remnant_radio.check()

        self.page.get_by_role('button', name='Save changes').click()
        self.page.wait_for_load_state('networkidle')

        # Try extracting from URL first
        campaign_id = extract_id_from_url(self.page.url, 'campaignid')
        if campaign_id:
            return campaign_id

        # Navigate to campaign list and find it
        self.page.goto(self._url(f'advertiser-campaigns.php?clientid={client_id}'))
        self.page.wait_for_load_state('networkidle')

        rows = self.page.locator('table tbody:nth-child(2) tr').all()
        for row in rows:
            first_cell = row.locator('td').first
            cell_text = first_cell.inner_text().strip()
            if name in cell_text:
                id_match = re.search(r'\[(\d+)\]', cell_text)
                if id_match:
                    return int(id_match.group(1))

        raise RuntimeError(f"Could not find campaignid after creating campaign.")

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
        if banner_id:
            return banner_id

        # Navigate to banner list and find it
        self.page.goto(self._url(
            f'campaign-banners.php?clientid={client_id}&campaignid={campaign_id}'
        ))
        self.page.wait_for_load_state('networkidle')

        rows = self.page.locator('table tbody:nth-child(2) tr').all()
        for row in rows:
            first_cell = row.locator('td').first
            cell_text = first_cell.inner_text().strip()
            if name in cell_text:
                id_match = re.search(r'\[(\d+)\]', cell_text)
                if id_match:
                    return int(id_match.group(1))

        raise RuntimeError(f"Could not find bannerid after creating banner.")

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

        Uses the search box to filter Available Zones by size, then
        Select All + Link, repeating across paginated results.

        Returns the number of link operations performed.
        """
        self.page.goto(self._url(
            f'campaign-zone.php?clientid={client_id}&campaignid={campaign_id}'
        ))
        self.page.wait_for_load_state('networkidle')

        # Build search terms from target sizes (e.g., "300x250")
        if target_sizes:
            search_terms = [f'{w}x{h}' for w, h in target_sizes]
        else:
            search_terms = ['']  # empty search = show all

        total_linked = 0

        for search_term in search_terms:
            if search_term:
                print(f"    Searching for: {search_term}")

            # Type in the Available Zones search box and press Enter
            search_box = self.page.locator('#quick-search-available')
            search_box.clear()
            if search_term:
                search_box.fill(search_term)
            search_box.press('Enter')
            self.page.wait_for_load_state('networkidle')
            import time
            time.sleep(1)  # wait for AJAX filter

            # Loop: Select All on current page, click Link, repeat until done
            while True:
                # Check if there are any available zone checkboxes
                select_all = self.page.locator('input[name="selectAll"][type="checkbox"]').first
                if select_all.count() == 0:
                    # Try alternative selector
                    select_all = self.page.get_by_role('checkbox', name='Select / Unselect All').first

                if select_all.count() == 0:
                    break

                # Check Select All
                select_all.check()

                # Click Link button
                link_button = self.page.locator('#link-button')
                if link_button.is_disabled():
                    print(f"    Link button disabled - no zones to link.")
                    break

                link_button.click()
                self.page.wait_for_load_state('networkidle')
                time.sleep(1)
                total_linked += 1
                print(f"    Linked a page of zones.")

                # Check if there are still available zones to link
                # The page refreshes after linking - if search is still active,
                # remaining zones on the next page will now be on page 1
                available_text = self.page.locator('text=Available:').first
                if available_text.count() > 0:
                    parent_text = available_text.locator('..').inner_text()
                    if 'Available: 0' in parent_text:
                        break
                else:
                    break

        return total_linked

    def get_zone_invocation_code(self, affiliate_id: int, zone_id: int) -> str:
        """Get the async JS invocation code for a zone with optimal settings."""
        self.page.goto(self._url(
            f'zone-invocation.php?affiliateid={affiliate_id}&zoneid={zone_id}'
        ))
        self.page.wait_for_load_state('networkidle')

        # Set "Don't show the banner again on the same page" to Yes
        unique_radio = self.page.locator('input[name="uniqueid"][value="1"], input[name="block"][value="1"]').first
        if unique_radio.count() > 0:
            unique_radio.check()

        # Set "Don't show a banner from the same campaign again on the same page" to Yes
        no_campaign_radio = self.page.locator('input[name="blockcampaign"][value="1"]').first
        if no_campaign_radio.count() > 0:
            no_campaign_radio.check()

        # Set "Target frame" to "New window"
        target_select = self.page.locator('select[name="target"]')
        if target_select.count() > 0:
            target_select.select_option(label='New window')

        # Click Refresh to regenerate code with new settings
        self.page.get_by_role('button', name='Refresh').click()
        self.page.wait_for_load_state('networkidle')

        # The code is in a textarea/textbox in the Bannercode section
        code_box = self.page.locator('textarea, input[type="text"]').last
        return code_box.input_value()

    def get_all_zone_codes(self) -> list[dict]:
        """Get invocation codes for all zones across all websites."""
        websites = self.get_websites()
        results = []
        for ws in websites:
            zones = self.get_zones(ws['affiliateid'])
            for zone in zones:
                print(f"  Getting code for: {ws['name']} - {zone['name']}")
                code = self.get_zone_invocation_code(zone['affiliateid'], zone['zoneid'])
                results.append({
                    'website': ws['name'],
                    'zone_name': zone['name'],
                    'zone_id': zone['zoneid'],
                    'width': zone['width'],
                    'height': zone['height'],
                    'code': code,
                })
        return results


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
            campaign_name = args.campaign or args.advertiser
            print(f"  Would create campaign: {campaign_name}")
        print(f"  Would create {len(images)} banner(s) (target=_blank)")
        if not args.skip_zone_link:
            sizes_str = ', '.join(f'{w}x{h}' for w, h in size_groups)
            print(f"  Would link campaign to zones matching: {sizes_str}")
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

    # -- Create or use campaign (single campaign for all sizes) ---------------
    created_banners = []

    if args.campaign_id:
        campaign_id = args.campaign_id
        print(f"Using existing campaign ID: {campaign_id}")
    else:
        campaign_name = args.campaign or args.advertiser
        print(f"\nCreating campaign: {campaign_name}")
        campaign_id = bot.create_campaign(client_id, campaign_name)
        print(f"  Created campaign ID: {campaign_id}")

    # -- Create banners -------------------------------------------------------
    print(f"\nCreating {len(images)} banner(s)...")
    for img in images:
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
            })
        except Exception as e:
            print(f"    ERROR creating banner: {e}")
            continue

    # -- Link campaign to zones matching all banner sizes ----------------------
    total_links = 0
    if not args.skip_zone_link:
        all_sizes = set(size_groups.keys())
        sizes_str = ', '.join(f'{w}x{h}' for w, h in all_sizes)
        print(f"\nLinking campaign {campaign_id} to zones matching: {sizes_str}")
        try:
            total_links = bot.link_campaign_to_zones(
                client_id=client_id,
                campaign_id=campaign_id,
                target_sizes=all_sizes,
            )
        except Exception as e:
            print(f"  ERROR linking: {e}")

    # -- Summary --------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Advertiser ID:   {client_id}")
    print(f"  Campaign ID:     {campaign_id}")
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


def run_get_zone_codes(args, bot, revive_url):
    """Pull invocation codes for all zones and save to a file."""
    print(f"\nLogging in to {revive_url}...")
    bot.login()
    print("  Logged in successfully.")

    print("\nFetching zone invocation codes...")
    codes = bot.get_all_zone_codes()

    if not codes:
        print("  No zones found.")
        return

    # Group by size
    by_size = defaultdict(list)
    for entry in codes:
        by_size[(entry['width'], entry['height'])].append(entry)

    # Write to file
    output_file = Path('zone-codes.html')
    with open(output_file, 'w') as f:
        for (w, h), entries in sorted(by_size.items()):
            f.write(f'<!-- ===== {w}x{h} Zones ===== -->\n\n')
            for entry in entries:
                f.write(f'<!-- Website: {entry["website"]} | Zone: {entry["zone_name"]} | ID: {entry["zone_id"]} | Size: {w}x{h} -->\n')
                f.write(f'{entry["code"]}\n\n')

    print(f"\n  Saved {len(codes)} zone code(s) to {output_file}")

    # Also print summary
    print(f"\n  Zones by size:")
    for (w, h), entries in sorted(by_size.items()):
        print(f"    {w}x{h}: {len(entries)} zone(s)")


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
    parser.add_argument(
        '--get-zone-codes',
        action='store_true',
        help='Pull async JS invocation codes for all zones, grouped by size',
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

    if not args.update_banners and not args.create_website and not args.create_websites and not args.setup_zones and not args.strip_quotes and not args.get_zone_codes and not args.image_folder:
        print("ERROR: image_folder is required (or use --update-banners / --create-website / --create-websites / --setup-zones / --get-zone-codes)")
        sys.exit(1)

    bot = ReviveBot(revive_url, username, password, headless=not args.headed)
    bot.start()

    try:
        if args.get_zone_codes:
            run_get_zone_codes(args, bot, revive_url)
        elif args.strip_quotes:
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
