"""Three data modules: offline operations or an explicitly paid PDF pipeline."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.process_lock import exclusive_process_lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["run", "extract", "check", "history", "metadata"]
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--max-chars", type=int)
    parser.add_argument("--as-of")
    parser.add_argument("--issuer")
    parser.add_argument(
        "--paid",
        action="store_true",
        help="authorize the frozen API run; omitted means offline preflight",
    )
    args = parser.parse_args()
    if args.action == "run":
        from src.fundamental_data.api_pipeline import run

        result = run(
            args.source, args.output, paid=args.paid, max_chars=args.max_chars or 20000
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "needs_review" else 0
    from src.fundamental_data.disclosure import (
        audit_all,
        build_all,
        minimum_document,
        read_json,
        verified_pack,
        write_json,
    )
    from src.fundamental_data.history import assemble_history
    from src.fundamental_data.metadata import build_metadata

    with exclusive_process_lock(args.output / ".data.lock") as acquired:
        if not acquired:
            return 3
        if args.action == "extract":
            result = build_all(args.source, args.output)
            for entry in result["entries"]:
                if entry.get("pack_key"):
                    pack, _ = verified_pack(args.output / "packs" / entry["pack_key"])
                    write_json(
                        args.output / "minimum" / (entry["pack_key"] + ".json"),
                        minimum_document(pack),
                    )
            passed = bool(result["entries"]) and all(
                "pack_key" in entry for entry in result["entries"]
            )
        elif args.action == "check":
            result = audit_all(args.source)
            passed = result["passed"]
        elif args.action == "history":
            if not args.max_chars or not args.as_of or not args.issuer:
                parser.error("history requires --max-chars, --as-of and --issuer")
            context = args.output / "context.md"
            context.unlink(missing_ok=True)
            result = assemble_history(
                args.source,
                max_chars=args.max_chars,
                as_of=args.as_of,
                issuer_id=args.issuer,
            )
            passed = result["status"] == "ready"
            # Remove an older accepted selection when the new selection fails.
            if passed:
                context.write_text(result["context"], encoding="utf8")
            else:
                context.unlink(missing_ok=True)
        else:
            if not args.manifest:
                parser.error("metadata requires --manifest")
            result = build_metadata(read_json(args.manifest), args.source)
            passed = True  # Missing metadata is reported, never fabricated.
        write_json(args.output / (args.action + ".json"), result)
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                not in {
                    "context",
                    "blocks",
                    "companies",
                    "peer_pairs",
                    "entries",
                    "checks",
                }
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
