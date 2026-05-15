```json id="o5d4y3"
{
  "name": "ghana-goethe-booking",
  "version": "1.0.0",
  "description": "Enterprise Goethe Booking Automation System",
  "main": "main.py",
  "scripts": {
    "prestart": "python scripts/check_environment.py && python scripts/install_dependencies.py && python scripts/prepare_browser.py",
    "start": "python main.py",
    "dev": "python main.py --dev",
    "build": "pyinstaller main.spec",
    "lint": "ruff check .",
    "format": "black .",
    "test": "pytest"
  }
}
```
