"""Desktop entry point; --cli uses the same local analysis engine."""
import sys


def main() -> int:
    if "--cli" in sys.argv:
        from .cli import main as cli_main
        args = sys.argv[1:]
        args.remove("--cli")
        return cli_main(args)
    from .ui import launch
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
