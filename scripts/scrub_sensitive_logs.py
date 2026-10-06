from pathlib import Path

from app.logging import redact_log_message


def scrub_logs(roots: list[Path]) -> tuple[int, int]:
    scanned = 0
    changed = 0
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.log"):
            if not path.is_file():
                continue
            scanned += 1
            original = path.read_text(encoding="utf-8", errors="replace")
            redacted = redact_log_message(original)
            if redacted != original:
                path.write_text(redacted, encoding="utf-8")
                changed += 1
    return scanned, changed


def main() -> None:
    scanned, changed = scrub_logs([Path("logs"), Path("data")])
    print(f"scanned_log_files={scanned} redacted_log_files={changed}")


if __name__ == "__main__":
    main()
