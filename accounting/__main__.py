import argparse
import json
import sqlite3
from pathlib import Path
import sys

from .database import Database
from .ingest import run_import, statistics


def main():
    parser = argparse.ArgumentParser(description="Local statement import; no network or Git operations")
    commands = parser.add_subparsers(dest="command",required=True)
    importer = commands.add_parser("import",help="Import all top-level CSV/XLSX/PDF statements atomically")
    importer.add_argument("--input-dir",type=Path,default=Path("."))
    importer.add_argument("--database",type=Path,default=Path("data/accounting.sqlite3"))
    importer.add_argument("--profile",default="personal",help="Identity scope for exports without an account identifier")
    summary = commands.add_parser("stats")
    summary.add_argument("--database",type=Path,default=Path("data/accounting.sqlite3"))
    web = commands.add_parser("serve",help="Open a local-only bill review interface")
    web.add_argument("--database",type=Path,default=Path("data/accounting.sqlite3"))
    web.add_argument("--port",type=int,default=8765)
    arguments = parser.parse_args()
    try:
        if arguments.command == "import":
            report = run_import(arguments.input_dir,arguments.database,arguments.profile)
            destination = arguments.database.parent / ("import-report-" + report["batch_id"] + ".json")
            with destination.open("x",encoding="utf-8") as stream:
                json.dump(report,stream,ensure_ascii=False,indent=2)
                stream.write("\n")
            print(json.dumps({
                "database":str(arguments.database),"report":str(destination),
                "source_rows":sum(f["source_rows"] for f in report["files"]),
                "new_raw_records":report["new_raw_records"],
                "duplicate_snapshots":report["duplicate_snapshots"],
                "new_bills":report["new_bills"],"statistics":report["statistics"],
                "classification_complete":False,"cross_platform_matching_complete":False,
            },ensure_ascii=False,indent=2))
        elif arguments.command == "serve":
            from .web import serve
            serve(arguments.database,arguments.port)
        else:
            database = Database(arguments.database,readonly=True)
            try:
                print(json.dumps(statistics(database),ensure_ascii=False,indent=2))
            finally:
                database.close()
        return 0
    except (ValueError,RuntimeError,OSError,sqlite3.DatabaseError) as error:
        print(f"{arguments.command} failed: {error}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
