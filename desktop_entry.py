"""PyInstaller entry point, kept outside the package for absolute imports."""
from folder_organizer.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
