# ReviveBot - Implementation Plan

## Goal
Python CLI tool that reads images from a folder, creates Revive Ad Server entities
(advertiser, campaign, banners), and links banners to matching zones by size.

## Workflow
1. Read config from `.env` (API URL, credentials, agency ID)
2. Scan image folder for supported formats (jpg, png, gif, webp)
3. Detect each image's dimensions via Pillow
4. Create advertiser via REST API (or use existing ID)
5. Create campaign under that advertiser
6. For each image: create a banner with base64-encoded image data
7. Fetch all zones (via publishers) and build a size map
8. Link each banner to all zones matching its dimensions

## REST API Endpoints (reviveadserverrestapi.com plugin)
- Auth: HTTP Basic Auth (base64 user:password)
- `POST /adv/new` - create advertiser
- `POST /cam/new` - create campaign
- `POST /ban/new` - create banner (with aImage)
- `GET /pub/list/{agencyId}` - list publishers
- `GET /zon/list/{publisherId}` - list zones for a publisher
- `POST /zon/{zoneId}/ban/{bannerId}` - link banner to zone

## Banner Creation Payload
```json
{
  "campaignId": 1,
  "bannerName": "my-banner",
  "storageType": "sql",
  "width": 728,
  "height": 90,
  "aImage": {
    "filename": "banner.jpg",
    "content": "<base64 encoded>"
  },
  "weight": 1
}
```

## Files
- `.env.example` - config template
- `requirements.txt` - dependencies
- `revivebot.py` - main script

## Status
- [ ] Scaffold project files
- [ ] Implement ReviveClient class
- [ ] Implement image scanning
- [ ] Implement CLI interface
- [ ] Test and iterate
