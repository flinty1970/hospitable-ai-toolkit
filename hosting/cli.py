"""Docker exec commands load mounted credentials, never shell-source them."""
import argparse
import os
import sys
from hosting.container_runtime import prepare


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "reindex", "review", "inbox"])
    parser.add_argument("property_id", nargs="?")
    args = parser.parse_args()
    aid, account, _ = prepare()
    if args.action == "inbox":
        os.execv(sys.executable, [sys.executable, "-m", "hosting.inbox_admin", "list", "--account", aid])
    if args.property_id not in account["properties"]:
        parser.error("Specify a selected property ID")
    os.execv(sys.executable, [sys.executable, "-m", "hosting.manage", args.action, aid, args.property_id])


if __name__ == "__main__":
    main()
