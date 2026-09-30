"""Docker exec commands load mounted credentials, never shell-source them."""
import argparse
import os
import sys
from hosting.container_runtime import prepare


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "reindex", "review", "inbox", "convert-pdfs", "approve-pdf"])
    parser.add_argument("property_id", nargs="?")
    parser.add_argument("filename", nargs="?")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    aid, account, _ = prepare()
    if args.action == "inbox":
        os.execv(sys.executable, [sys.executable, "-m", "hosting.inbox_admin", "list", "--account", aid])
    if args.property_id not in account["properties"]:
        parser.error("Specify a selected property ID")
    if args.action in {"convert-pdfs", "approve-pdf"}:
        from pathlib import Path
        os.environ["TOOLKIT_PROPERTY_DIR"] = account["properties"][args.property_id]["runtime_dir"]
        command = [sys.executable, "-m", "hosting.pdf_ingestion", "convert" if args.action == "convert-pdfs" else "approve"]
        if args.filename:
            command.append(args.filename)
        if args.replace:
            command.append("--replace")
        if args.allow_incomplete:
            command.append("--allow-incomplete")
        os.execv(sys.executable, command)
    os.execv(sys.executable, [sys.executable, "-m", "hosting.manage", args.action, aid, args.property_id])


if __name__ == "__main__":
    main()
