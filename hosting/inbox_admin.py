"""Local operator inbox inspection/retry. Never exposes webhook secrets."""
import argparse
import json
import os
from hosting.gateway import Inbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["list", "retry"])
    parser.add_argument("--account", required=True)
    parser.add_argument("--id", type=int)
    args = parser.parse_args()
    inbox = Inbox(os.environ["TOOLKIT_INBOX_DB"])
    with inbox.connect() as db:
        if args.action == "list":
            rows = db.execute("SELECT id,account,state,property_id,attempts,reason,received FROM inbox WHERE account=? ORDER BY id DESC LIMIT 100", (args.account,))
            for row in rows:
                print(json.dumps(dict(row)))
        else:
            if args.id is None:
                parser.error("retry requires --id")
            cursor = db.execute("UPDATE inbox SET state='pending',next_attempt=0,reason=NULL WHERE id=? AND account=? AND state='review'", (args.id, args.account))
            print(f"Requeued {cursor.rowcount} review event(s)")


if __name__ == "__main__":
    main()
