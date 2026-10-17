"""Print effective configuration using an explicit allowlist; never print the API key."""
import json

from config import Settings


def main() -> None:
    settings = Settings.load()
    summary = settings.public_summary()
    summary["request_log_file"] = str(settings.request_log_file)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
