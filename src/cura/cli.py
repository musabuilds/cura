"""`cura` command line.

cura collect osm            refresh a scripted source into sources/staging/
cura merge                  staging → data/*.jsonl (+ data/manifest.json)
cura validate               schema + integrity checks (CI gate)
cura export                 dist/: JSON Schema, GeoJSON, CSV, SQLite
cura api                    run the HTTP API
cura db upgrade             apply pending schema migrations
cura db seed                load data/*.jsonl into the API database
cura db export-changes      API edits → sources/staging/cura_api.jsonl, merges → sources/curation/
cura embed sync             embed records whose version moved (--watch to keep running)
"""

from __future__ import annotations

import argparse
import json
import sys

from cura.paths import DATA_DIR

COLLECTORS = ("osm", "wikidata")


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cura", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_collect = sub.add_parser("collect", help="run a scripted collector")
    p_collect.add_argument("source", choices=COLLECTORS)
    sub.add_parser("merge", help="build the canonical dataset from staging")
    sub.add_parser("validate", help="validate the canonical dataset")
    sub.add_parser("export", help="build release artefacts in dist/")
    p_api = sub.add_parser("api", help="serve the API")
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8000)
    p_api.add_argument("--reload", action="store_true")
    p_db = sub.add_parser("db", help="API database tasks")
    p_db.add_argument("task", choices=("init", "upgrade", "revision", "seed", "export-changes", "create-admin"))
    p_db.add_argument("-m", "--message", help="revision: what the migration does")
    p_db.add_argument("--email")
    p_db.add_argument("--password")
    p_embed = sub.add_parser("embed", help="semantic index")
    p_embed.add_argument("task", choices=("sync",))
    p_embed.add_argument("--batch", type=int, default=32)
    p_embed.add_argument("--limit", type=int)
    p_embed.add_argument("--rebuild", action="store_true", help="recheck every record, not only changed ones")
    p_embed.add_argument("--watch", type=float, metavar="SECONDS", help="keep syncing at this interval")
    args = parser.parse_args(argv)

    if args.cmd == "collect":
        module = __import__(f"cura.collectors.{args.source}", fromlist=["run"])
        print(f"{args.source}: {module.run()} records")
    elif args.cmd == "merge":
        from cura.pipeline import merge, stats

        report = merge.run()
        (DATA_DIR / "manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (DATA_DIR / "STATS.md").write_text(stats.markdown(report), encoding="utf-8")
        _print({k: v for k, v in report.items() if k != "sources"})
    elif args.cmd == "validate":
        from cura.pipeline import validate

        rep = validate.run()
        for w in rep.warnings[:50]:
            print("warning:", w)
        for e in rep.errors[:200]:
            print("ERROR:", e)
        _print(rep.stats)
        return 0 if rep.ok else 1
    elif args.cmd == "export":
        from cura.pipeline import export

        _print(export.run())
    elif args.cmd == "api":
        import uvicorn

        uvicorn.run("cura.api.main:app", host=args.host, port=args.port, reload=args.reload)
    elif args.cmd == "db":
        from cura.api import admin

        _print(admin.run(args.task, email=args.email, password=args.password, message=args.message))
    elif args.cmd == "embed":
        _embed(args)
    return 0


def _embed(args) -> None:
    import time

    from cura.api import db
    from cura.search import embedder, index

    model = embedder.from_settings()
    if model is None:
        raise SystemExit("Set CURA_EMBED_PROVIDER (openai or hashing) first; see docs/search.md")
    db.init()
    while True:
        with next(db.get_session()) as session:
            counts = index.sync(session, model, batch=args.batch, limit=args.limit, rebuild=args.rebuild)
        if args.watch is None:
            _print(counts | {"model": model.model})
            return
        if counts["embedded"] or counts["removed"]:
            _print(counts | {"model": model.model})
        args.rebuild = False
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
