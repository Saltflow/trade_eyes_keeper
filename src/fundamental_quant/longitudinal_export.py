"""Export relative source manifests and a bounded file list, never copy raw PDFs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from .fr_pack import verified_pack, write_json

VERSION = "longitudinal-source-export/2"


def export_sources(config, as_of, output, discover, *, project_root=None):
    root = Path(project_root or Path.cwd()).resolve()
    output = Path(output).resolve()
    if not output.is_relative_to(root):
        raise ValueError("source_export_outside_project")
    output.mkdir(parents=True, exist_ok=True)
    files = {}

    def register(path, expected=None):
        path = Path(path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(
                "export_dependency_outside_project_or_missing:" + str(path)
            )
        relative = path.relative_to(root).as_posix()
        if not relative.startswith("data/"):
            raise ValueError("export_dependency_not_data:" + relative)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected and expected != sha:
            raise ValueError("export_dependency_hash_mismatch:" + relative)
        files[relative] = {
            "source": str(path),
            "target": relative,
            "sha256": sha,
            "bytes": path.stat().st_size,
        }
        return relative

    portable = copy.deepcopy(config)
    for group in portable["groups"]:
        docs = discover(group, as_of)
        aliases = {}
        for pack_root in group.get("pack_roots", []):
            pack_root = Path(pack_root)
            manifest = json.loads(
                (pack_root / "latest_run.json").read_text(encoding="utf8")
            )
            for entry in manifest["entries"]:
                if entry.get("pack_key"):
                    pack, _ = verified_pack(pack_root / "packs" / entry["pack_key"])
                    aliases[pack["metadata"]["sha256"]] = pack["metadata"]["short_id"]
        manifest_path = output / (group["group_id"] + ".documents.json")
        entries = []
        for doc in docs:
            register(doc["path"], doc["sha256"])
            entries.append(
                {
                    "issuer_id": doc["issuer_id"],
                    "period_end": doc["period_end"],
                    "published_at": doc["published_at"],
                    "pdf_sha256": doc["sha256"],
                    "pdf_file": Path(
                        os.path.relpath(doc["path"], manifest_path.parent)
                    ).as_posix(),
                    "source_url": doc.get("url", ""),
                    **(
                        {"short_id": aliases[doc["sha256"]]}
                        if doc["sha256"] in aliases
                        else {}
                    ),
                }
            )
        write_json(manifest_path, {"schema_version": VERSION, "documents": entries})
        group["document_manifests"] = [register(manifest_path)]
        group.pop("ledger_issuers", None)
        group.pop("pack_roots", None)
        group["supplemental_facts_paths"] = [
            register(path) for path in group.get("supplemental_facts_paths", [])
        ]
        image_paths = []
        for index, path in enumerate(group.get("image_evidence_paths", [])):
            path = Path(path)
            original_bytes = path.read_bytes()
            manifest = json.loads(original_bytes)
            original_receipt = json.loads(
                path.with_suffix(".receipt.json").read_text(encoding="utf8")
            )
            origin_hash = hashlib.sha256(original_bytes).hexdigest()
            if original_receipt["manifest_sha256"] != origin_hash:
                raise ValueError("image_review_manifest_original_receipt_mismatch")
            for page in manifest["pages"]:
                page["image_path"] = register(page["image_path"], page["image_sha256"])
            transformed = output / f"{group['group_id']}.image-review-{index}.json"
            write_json(transformed, manifest)
            write_json(
                transformed.with_suffix(".receipt.json"),
                {
                    **original_receipt,
                    "manifest_sha256": hashlib.sha256(
                        transformed.read_bytes()
                    ).hexdigest(),
                    "relocated_from_manifest_sha256": origin_hash,
                    "relocation": "paths_only_original_pixels_financial_values_unchanged",
                },
            )
            image_paths.append(register(transformed))
            register(transformed.with_suffix(".receipt.json"))
        group["image_evidence_paths"] = image_paths
        # Frozen PIT inputs must never overwrite the production market cache.
        original_store = Path(
            group.get("valuation_store_root", root / "data/point_in_time")
        )
        snapshot_root = output / "valuation_snapshots" / group["group_id"]
        provenance = []
        for company in group["companies"]:
            code = company["issuer_id"].split(":")[-1]
            if not code.isalnum():
                raise ValueError("unsafe_valuation_snapshot_symbol")
            for relative in (
                f"fundamentals/{code}.statements.json",
                f"market/{code}.csv",
                f"market/{code}.meta.json",
            ):
                original = (original_store / relative).resolve()
                if not original.is_file():
                    continue
                if not original.is_relative_to(root / "data"):
                    raise ValueError("valuation_snapshot_outside_project_data")
                payload = original.read_bytes()
                target = snapshot_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                sha = hashlib.sha256(payload).hexdigest()
                register(target, sha)
                provenance.append(
                    {
                        "original_path": str(original),
                        "snapshot_path": relative,
                        "sha256": sha,
                    }
                )
        snapshot_root.mkdir(parents=True, exist_ok=True)
        write_json(
            snapshot_root / "provenance.json",
            {"schema_version": VERSION, "files": provenance},
        )
        register(snapshot_root / "provenance.json")
        group["valuation_store_root"] = snapshot_root.relative_to(root).as_posix()
    tokenizer = root / "data/fundamental_quant/tokenizer/tokenizer.json"
    if tokenizer.is_file():
        register(tokenizer)
    config_path = output / "config.json"
    write_json(config_path, portable)
    register(config_path)
    manifest = {
        "schema_version": VERSION,
        "as_of": as_of,
        "config": config_path.relative_to(root).as_posix(),
        "files": list(files.values()),
        "total_bytes": sum(x["bytes"] for x in files.values()),
        "paid_model_calls": 0,
        "transfer_derived_packets": False,
        "transfer_research_ledgers": False,
        "next_action": "transfer_only_listed_files_verify_hashes_then_prepare_in_production_new_output_root",
    }
    write_json(output / "transfer_manifest.json", manifest)
    return manifest
