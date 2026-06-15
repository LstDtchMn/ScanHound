# MediaScout

A powerful desktop application for comparing your Plex library against online releases, identifying missing content and potential upgrades.

## Features

### Core Functionality
- **Automated Scanning**: Scrape sources for 4K/1080p releases
- **Plex Integration**: Compare against your Plex library
- **Smart Matching**: IMDb-based and fuzzy title matching
- **Upgrade Detection**: Identifies resolution, Dolby Vision, and size upgrades
- **Batch Operations**: Select and download multiple items at once
- **JDownloader Integration**: Automatic download management

### Advanced Features
- **Configurable Rules**: Customize upgrade detection logic
- **Metadata Enrichment**: Fetch ratings from TMDB, OMDB, Rotten Tomatoes
- **Caching**: SQLite-based caching for fast subsequent scans
- **Filtering & Search**: Real-time table filtering
- **Scheduler**: Automatic periodic scans
- **Statistics**: Track library composition and scan results

## Installation

### Prerequisites
- Python 3.11 or higher
- Plex Media Server with API access
- (Optional) JDownloader 2 for downloads
- (Optional) TMDB/OMDB API keys for metadata

### Quick Start

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd MediaScout
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the application**
   - Launch the app: `python main.py`
   - Click Settings (gear icon)
   - Enter your Plex URL and token
   - (Optional) Add TMDB/OMDB API keys
   - Select your Plex libraries

4. **Run your first scan**
   - Click "Start Scan" or press `Ctrl+S`
   - Wait for results to populate
   - Review missing items and upgrades

## Configuration

### Plex Setup

1. **Get your Plex Token**:
   - Sign in to Plex Web App
   - Play any media
   - Click the three dots > "Get Info"
   - View XML > Look for `X-Plex-Token` in the URL

2. **Configure in Settings**:
   - Plex URL: `http://127.0.0.1:32400` (or your server IP)
   - Plex Token: Paste your token
   - Select libraries to scan

### Upgrade Rules

Configure which items should be flagged as upgrades:

- **Resolution Upgrades**: 1080p > 4K
- **Dolby Vision**: Non-DV > DV
- **Size Upgrades**: Larger encodes (configurable threshold)
- **Strict Resolution Mode**: Only match exact resolutions

### API Keys (Optional)

**TMDB API** (Free):
1. Sign up at https://www.themoviedb.org/
2. Go to Settings > API > Request API Key
3. Add to app settings

**OMDB API** (Free tier available):
1. Sign up at http://www.omdbapi.com/apikey.aspx
2. Get your API key from email
3. Add to app settings

## Usage

### Keyboard Shortcuts
- `Ctrl+S`: Start scan
- `Esc`: Stop scan
- `Ctrl+F`: Focus search box
- `Ctrl+,`: Open settings
- `Ctrl+E`: Export results to CSV
- `Ctrl+A`: Select/deselect all
- `F5`: Refresh table

### Status Indicators
- **MISSING**: Not in your library
- **In Library**: Already have this item
- **UPGRADE (4K)**: 4K version available (you have 1080p)
- **DV UPGRADE**: Dolby Vision version available
- **UPGRADE (+X%)**: Larger encode available (better quality)
- **[DL] DOWNLOADED**: Previously downloaded

### Filtering
Use the filter dropdown to show:
- All Results
- Missing Only
- Upgrades Only
- In Library Only
- New Plex Additions (items added in last 7 days)

### Batch Downloads
1. Select items using checkboxes
2. Click "Download Selected"
3. Choose JDownloader method (folder monitor or API)

## Technology Stack
- **GUI**: PySide6 / QML (Qt6 with Material theme)
- **Database**: SQLite3
- **HTTP**: aiohttp (async), cloudscraper, requests
- **Scraping**: BeautifulSoup4, Selenium (fallback)
- **Matching**: TheFuzz (fuzzy string matching)
- **Plex API**: PlexAPI

## Troubleshooting

### Common Issues

**"Plex connection failed"**:
- Verify Plex server is running
- Check Plex URL is correct
- Ensure Plex token is valid
- Check firewall settings

**"No results found"**:
- Verify source toggles are enabled (Settings > Sources)
- Try enabling debug mode for detailed logs

**"Scraping failed" / Timeout errors**:
- Increase timeout in settings
- Check internet connection

**Database corruption**:
- App auto-recovers by creating backup
- Manual fix: Delete `crawler.db` and restart

**High memory usage**:
- Reduce `scan_threads` in settings
- Clear Plex cache periodically

### Debug Mode

Enable in Settings > Debug Mode for detailed logs:
- All HTTP requests/responses
- Matching scores for titles
- Configuration validation warnings
- Performance metrics

Logs saved to `scanner.log`

## Performance Tips

1. **Cache Duration**: Set to 4-8 hours for best balance
2. **Scan Threads**: 5-15 depending on your internet speed
3. **Ignore Keywords**: Add false positives to reduce noise
4. **Library Selection**: Only scan libraries you care about

## FAQ

**Q: Does it download automatically?**
A: No, it only identifies and sends links to JDownloader. You control what downloads.

**Q: Can I use without Plex?**
A: No, Plex integration is core to the comparison functionality.

**Q: What about TV shows?**
A: Supported! The app handles both movies and TV seasons.

**Q: How accurate is the matching?**
A: IMDb-based matching is 100% accurate. Fuzzy matching is ~95% accurate with configurable thresholds.

## License

MIT License - See LICENSE file for details.

## Disclaimer

This tool is for educational and personal use only. Respect content creators and copyright laws. The developers are not responsible for how users employ this software.
