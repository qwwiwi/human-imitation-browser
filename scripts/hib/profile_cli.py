"""Command line of `scripts/profiles.py`: list, show and delete browser profiles."""

import argparse
import json
import logging
import sys
from pathlib import Path

from hib.env import DEFAULT_ENV_FILE, load_settings
from hib.errors import HibError
from hib.profiles import ProfileStore, profiles_root, validate_profile_name

logger = logging.getLogger("profile")


def _confirmed(name: str) -> bool:
    """Ask the person to type the profile name; anything else (or no terminal) means no."""
    try:
        answer = input(
            f"Delete profile '{name}' with all its cookies and history? "
            "The site will then see a new visitor. Type the name to confirm: "
        )
    except EOFError:
        return False
    return answer.strip() == name


def main(argv: list[str]) -> int:
    """Run the command.

    Args:
        argv: Arguments without the program name.

    Returns:
        0 done, 1 delete not confirmed, 2 wrong arguments, 10 profile in use, 11 profile problem.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description="List, show and delete browser profiles (one profile = one returning visitor).")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="settings file (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list profiles with visit counts")
    show = sub.add_parser("show", help="show one profile (no secrets)")
    show.add_argument("name")
    delete = sub.add_parser("delete", help="delete a profile: cookies, history, fingerprint and proxy session")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    try:
        store = ProfileStore(profiles_root(load_settings(args.env_file).profiles_dir))
        if args.command == "list":
            for name in store.names():
                info = store.load(name).describe()
                sys.stdout.write(f"{name}\tvisits={info['visits']}\tlast_used={info['last_used'] or '-'}\n")
            return 0
        validate_profile_name(args.name)
        if args.command == "show":
            for key, value in store.load(args.name).describe().items():
                sys.stdout.write(f"{key}: {json.dumps(value, ensure_ascii=False)}\n")
            return 0
        if not args.yes and not _confirmed(args.name):
            logger.error("not deleted: the name was not confirmed")
            return 1
        store.delete(args.name)
        sys.stdout.write(f"deleted {args.name}\n")
        return 0
    except HibError as exc:
        logger.error("%s", exc)
        sys.stdout.write(json.dumps({"error": str(exc), "exit_code": exc.exit_code}, ensure_ascii=False) + "\n")
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
