# Development Guide

This document provides guidance for developers working on the MediaScout project.

## Table of Contents
- [Setup](#setup)
- [Code Quality](#code-quality)
- [Testing](#testing)
- [Architecture](#architecture)
- [Contributing](#contributing)

## Setup

### Prerequisites
- Python 3.11+
- Git

### Development Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd MediaScout
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv

   # Windows
   venv\Scripts\activate

   # Linux/Mac
   source venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure the application**
   ```bash
   # Copy config.example.json to config.json and fill in your credentials
   cp config.example.json config.json
   ```

## Code Quality

### Tools

We use several tools to maintain code quality:

- **Black**: Code formatter (enforces consistent style)
- **Flake8**: Linter (catches common errors)
- **MyPy**: Static type checker
- **Pylint**: Additional linting
- **Pytest**: Unit testing framework

### Running Quality Checks

**Windows (PowerShell)**:
```powershell
.\run_checks.ps1
```

**Linux/Mac**:
```bash
./run_checks.sh
```

**Individual Tools**:
```bash
# Format code
black .

# Check formatting without changes
black . --check --diff

# Lint code
flake8 backend/ ui/

# Type check
mypy backend/ ui/ --ignore-missing-imports

# Run tests
pytest tests/ -v
```

### Code Style Guidelines

1. **Line Length**: Maximum 120 characters
2. **Naming Conventions**:
   - Classes: `PascalCase`
   - Functions/Methods: `snake_case`
   - Constants: `UPPER_SNAKE_CASE`
   - Private methods: `_leading_underscore`

3. **Type Hints**: Add type hints to all new functions
   ```python
   def process_item(item: Dict[str, Any], index: int) -> Optional[str]:
       pass
   ```

4. **Docstrings**: Use Google-style docstrings
   ```python
   def example_function(param1: str, param2: int) -> bool:
       """Brief description of function.

       Longer description if needed.

       Args:
           param1: Description of param1
           param2: Description of param2

       Returns:
           Description of return value
       """
       pass
   ```

5. **Constants**: Use class-level constants instead of magic numbers
   ```python
   # Good
   if status == self.STATUS_MISSING:
       pass

   # Bad
   if status == "MISSING":
       pass
   ```

## Testing

### Running Tests

```bash
# Run all tests
python -m unittest discover tests -v

# Run specific test file
python -m unittest tests.test_matching -v

# Run with pytest (more features)
pytest tests/ -v

# Run with coverage report
pytest tests/ --cov=movie_app --cov-report=html
```

### Writing Tests

Tests are located in the `tests/` directory. Follow these guidelines:

1. **Test File Naming**: `test_<module_name>.py`
2. **Test Class Naming**: `Test<FeatureName>`
3. **Test Method Naming**: `test_<what_is_being_tested>`

**Example**:
```python
import unittest
from movie_app import MovieScannerApp

class TestMovieMatching(unittest.TestCase):
    """Tests for movie matching logic"""

    def setUp(self):
        """Set up test fixtures"""
        self.app = object.__new__(MovieScannerApp)
        self.app.config = {'movie_match_threshold': 85}

    def test_find_movie_by_imdb(self):
        """Test finding a movie by IMDb ID"""
        # Arrange
        web = {'imdb_id': 'tt0133093', 'year': 1999}
        plex_index = {'by_imdb': {'tt0133093': [{'title': 'The Matrix'}]}}

        # Act
        matches, uncertain = self.app._find_movie_matches(web, plex_index)

        # Assert
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['title'], 'The Matrix')
```

## Architecture

### Project Structure

```
MediaScout/
├── main.py               # Application entry point
├── backend/
│   ├── config.py         # Configuration types and defaults
│   ├── app_service.py    # Core application service
│   ├── scanner_service.py# Web scraping scan engine
│   ├── file_manager.py   # File processing (TMDB ID, move, rename)
│   ├── database.py       # SQLite database management
│   ├── plex_manager.py   # Plex server integration
│   ├── download_service.py # Download engine (JD, Selenium, clipboard)
│   ├── network.py        # Async HTTP request handling
│   ├── matching.py       # Matching and upgrade detection
│   ├── scrapers.py       # Web scraping logic
│   ├── sources/          # Source plugins (HDEncode, DDLBase, AdiTHD)
│   └── logic/            # Business logic (scanner, matcher)
├── ui/
│   ├── controllers/      # Python ↔ QML bridge controllers
│   ├── models/           # QAbstractListModel subclasses for QML
│   └── qml/              # QML UI files
│       ├── main.qml      # Main window
│       ├── ScannerTab.qml
│       ├── FileManagerTab.qml
│       ├── SettingsDialog.qml
│       ├── style/Theme.qml
│       └── components/   # Reusable QML components
├── requirements.txt      # Dependencies
└── DEVELOPMENT.md        # This file
```

### Key Components

#### 1. AppService (backend/app_service.py)
- Core service orchestrating Plex, scanning, file management
- Scheduler, notification bridge, system tray

#### 2. ScannerService (backend/scanner_service.py)
- Web scraping engine with TMDB/OMDb/RT metadata enrichment
- Plex library comparison and upgrade detection

#### 3. FileManager (backend/file_manager.py)
- Watch folder monitoring and TMDB-based file identification
- File moving/renaming with Plex naming conventions

#### 4. QML UI (ui/qml/)
- PySide6/QML with Material theme (dark/light mode)
- Controllers expose Python services to QML via context properties

### Database Schema

**downloads table**:
- `url` (TEXT PRIMARY KEY)
- `title` (TEXT)
- `date_added` (TIMESTAMP)

**plex_cache table**:
- `key` (TEXT PRIMARY KEY)
- `title`, `original_title`, `year`, `res`, `size`
- `imdb_id`, `rating_key`, `media_id`
- `is_tv`, `season`, `episode_count`
- `dovi`, `hdr`, `last_updated`
- Indexes on: `imdb_id`, `title`, `(is_tv, season)`, `year`, `res`

**app_config table**:
- `key` (TEXT PRIMARY KEY)
- `value` (TEXT)

## Contributing

### Development Workflow

1. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Write code
   - Add tests
   - Update documentation

3. **Run quality checks**
   ```bash
   ./run_checks.ps1  # Windows
   ./run_checks.sh   # Linux/Mac
   ```

4. **Commit your changes**
   ```bash
   git add .
   git commit -m "feat: Add feature description"
   ```

   **Commit Message Format**:
   - `feat:` New feature
   - `fix:` Bug fix
   - `docs:` Documentation changes
   - `refactor:` Code refactoring
   - `test:` Adding tests
   - `chore:` Maintenance tasks

5. **Push and create pull request**
   ```bash
   git push origin feature/your-feature-name
   ```

### Code Review Checklist

Before submitting a pull request, ensure:

- [ ] All tests pass
- [ ] Code is formatted with Black
- [ ] No linting errors from Flake8
- [ ] Type hints added (mypy passes)
- [ ] New features have tests
- [ ] Documentation updated
- [ ] No sensitive data in commits

## Recent Improvements (v2.1)

### Refactoring
- **Extracted compare_and_display method**: Reduced from 385 lines to 54 lines
- **11 new private methods**: Better separation of concerns
- **Type hints added**: Improved IDE support and error detection

### Code Quality
- **Constants for magic values**: No more hard-coded colors/strings
- **Configuration validation**: Automatic validation and correction
- **Enhanced error handling**: Better network error management
- **Database indexes**: Improved query performance

### Development Tools
- **Test infrastructure**: Unit tests for matching and upgrade logic
- **Black configuration**: Consistent code formatting
- **Flake8/Pylint setup**: Automated linting
- **MyPy configuration**: Static type checking
- **Quality check scripts**: Easy one-command validation

## Performance Considerations

1. **Database Queries**: Indexes added for common queries (IMDb, title, year)
2. **Async Operations**: Use `AsyncRequestManager` for network requests
3. **Caching**: Plex library cached to avoid repeated API calls
4. **Thread Pool**: Configurable `scan_threads` for concurrent processing

## Troubleshooting

### Common Issues

**Import Errors**:
```bash
# Reinstall dependencies
pip install -r requirements.txt --force-reinstall
```

**Database Corruption**:
- The app auto-recovers by backing up corrupt DB and creating fresh one
- Manual recovery: Delete `crawler.db` and restart

**Test Failures**:
- Check that all required constants are set in test fixtures

## Resources

- [Python Type Hints](https://docs.python.org/3/library/typing.html)
- [Black Documentation](https://black.readthedocs.io/)
- [Pytest Documentation](https://docs.pytest.org/)
- [PySide6 Documentation](https://doc.qt.io/qtforpython-6/)
- [QML Reference](https://doc.qt.io/qt-6/qmlreference.html)

## License

MIT License - See LICENSE file for details
