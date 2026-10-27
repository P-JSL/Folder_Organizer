"""Publish actionable JUnit failures as GitHub Checks annotations."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = Path("test-results.xml")
    if not path.is_file():
        return
    for case in ET.parse(path).iter("testcase"):
        for issue in list(case):
            if issue.tag not in {"failure", "error"}:
                continue
            title = f"{case.get('classname', '')}.{case.get('name', '')}"
            detail = (issue.text or issue.get("message", "Test failed"))[-16000:]
            print(f"::error title={escape(title)}::{escape(detail)}")


if __name__ == "__main__":
    main()
