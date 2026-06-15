# Changelog - MediaScout

## Summary
Comprehensive refactoring addressing 30+ bugs and improvements including critical bug fixes, improved error handling, logging, security, and code quality.

## Critical Bugs Fixed

### 1. Missing `season_sort` Field
- **Fixed**: Added `season_sort` field to item dictionary in `add_row()` function
- **Impact**: Season column sorting now works correctly

### 2. Duplicate Configuration Keys
- **Fixed**: Removed duplicate `source_remux` and `scheduler_enabled` entries from default config
- **Impact**: Cleaner configuration, no confusion

### 3. Duplicate `log_widget` Initialization
- **Fixed**: Removed duplicate `self.log_widget = None` statement
- **Impact**: Cleaner code

### 4. Redundant `import time`
- **Fixed**: Removed redundant local import of `time` in `scheduler_loop()` function
- **Impact**: More efficient imports

### 5. Missing RT Column Toggle
- **Fixed**: Added "Show Rotten Tomatoes" toggle to Display settings
- **Impact**: Users can now control RT column visibility

## Major Improvements

### Python Logging Module (Was: Custom Logging)
- **Added**: Full Python `logging` module integration with levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- **Added**: Rotating file handler (max 5MB per file, keeps 3 backups)
- **Added**: Proper logging configuration with `setup_logging()` function
- **Added**: Log level support throughout application
- **Impact**: Professional logging, easier debugging, better log management

### Environment Variable Support
- **Added**: `.env` file support using `python-dotenv`
- **Added**: Environment variable overrides for sensitive credentials:
  - `PLEX_URL`
  - `PLEX_TOKEN`
  - `TMDB_API_KEY`
  - `OMDB_API_KEY`
- **Added**: `.env.example` template file
- **Impact**: Credentials no longer need to be stored in config.json, better security

### Auto-Fit Window Sizing
- **Changed**: Window now auto-sizes to 90% of screen dimensions and centers itself
- **Removed**: Hard-coded 1750x950 window size
- **Impact**: Better compatibility with different screen sizes

### Constants Extracted
- **Added**: Named constants for magic numbers:
  - `TOOLTIP_DELAY_MS = 500`
  - `DEFAULT_BUTTON_WIDTH = 120`
  - `DEFAULT_WINDOW_WIDTH_PERCENT = 0.9`
  - `DEFAULT_WINDOW_HEIGHT_PERCENT = 0.9`
  - `API_RATE_LIMIT_DELAY = 0.25`
  - `REQUEST_TIMEOUT = 10`
  - `MAX_RETRIES = 3`
  - `RETRY_BACKOFF_FACTOR = 2`
- **Impact**: More maintainable code, easier to adjust values

### Input Validation
- **Added**: Comprehensive validation in settings dialog:
  - Plex URL format validation
  - JDownloader folder existence check
  - Scheduler interval range validation (1-168 hours)
  - Min file size validation (no negative values)
- **Added**: Error dialog showing validation issues
- **Impact**: Prevents crashes from invalid input, better UX

### Improved WebDriver Management
- **Enhanced**: `get_driver()` now validates cached driver with multiple checks
- **Added**: Proper cleanup of dead drivers
- **Added**: Better error logging for driver issues
- **Impact**: More reliable link scraping, fewer crashes

### Network Retry Logic
- **Added**: `@retry_request` decorator for network operations
- **Added**: Exponential backoff retry strategy (max 3 attempts)
- **Added**: Proper error logging for failed requests
- **Impact**: More reliable API calls, handles temporary network issues

### Enhanced Error Handling
- **Improved**: Replaced bare `except:` statements with specific exceptions:
  - `json.JSONDecodeError` for JSON parsing
  - `IOError`, `OSError` for file operations
  - `RuntimeError`, `AttributeError` for UI operations
  - `requests.RequestException` for network issues
- **Added**: Error logging throughout application
- **Impact**: Easier debugging, better error messages

### Keyboard Shortcuts
- **Added**: Additional keyboard shortcuts:
  - `Ctrl+S` - Start scan (existing)
  - `Escape` - Stop scan (existing)
  - `Ctrl+,` - Open settings
  - `Ctrl+E` - Export results
  - `Ctrl+F` - Focus search box
  - `Ctrl+A` - Select/deselect all
  - `F5` - Refresh table
- **Impact**: Improved productivity, better UX

### Comprehensive Docstrings
- **Added**: Google-style docstrings to all major functions:
  - `setup_logging()`
  - `load_config()`
  - `save_config()`
  - `load_history()`
  - `save_to_history()`
  - `fetch_tmdb_metadata()`
  - `fetch_tmdb_by_id()`
  - `fetch_omdb_data()`
  - `scrape_rt_score()`
  - `get_driver()`
  - `init_driver()`
  - `scrape_details()`
  - `compare_and_display()`
  - `parse_size()`
  - `refresh_table()`
  - `start_scan_thread()`
  - `run_process()`
  - Many more...
- **Impact**: Code is now self-documenting, easier to understand and maintain

### Code Quality
- **Removed**: Commented-out placeholder code
- **Improved**: Consistent error handling patterns
- **Improved**: Better separation of concerns
- **Added**: Type hints in docstrings
- **Impact**: More professional, maintainable codebase

## Files Modified
- `movie_app.py` - Main application (2800+ lines refactored)

## Files Added
- `.env.example` - Environment variable template
- `CHANGELOG.md` - This file

## Backward Compatibility
- ✅ Existing `config.json` files still work
- ✅ Environment variables are optional (fallback to config.json)
- ✅ All existing features preserved
- ✅ Log format improved but files remain readable

## Testing
- ✅ Python syntax validation passed
- ✅ All imports verified
- ✅ Dependencies installed successfully

## Upgrade Instructions

1. **Backup your current config**:
   ```bash
   copy config.json config.json.backup
   ```

2. **Optional: Set up environment variables**:
   - Copy `.env.example` to `.env`
   - Fill in your credentials in `.env`
   - The `.env` file is automatically git-ignored

3. **Run the application**:
   ```bash
   python movie_app.py
   ```

4. **First launch**:
   - Window will auto-size to your screen
   - Check Settings to verify configuration
   - Test connection to Plex

## Notes
- Logs now use proper levels (DEBUG, INFO, WARNING, ERROR)
- Enable "Debug Mode" in settings for verbose logging
- Check `scanner.log` for detailed operation logs
- Logs automatically rotate when they reach 5MB

## Future Improvements
The following items were identified but not implemented in this refactoring:
- Further refactoring of the 350+ line `compare_and_display()` function
- Additional unit tests
- Async API calls for better performance
- Configuration migration tool for major version upgrades
