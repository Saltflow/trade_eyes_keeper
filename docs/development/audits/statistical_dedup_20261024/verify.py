"""Verify persisted edit records independently and account for actual model-body tokens."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from tokenizers import Tokenizer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OUT = HERE / "run"
STAGE2 = ROOT / "data/fundamental_data/stage2_model_context_20261024_125223_final"
tokenizer = Tokenizer.from_file(str(ROOT / "data/fundamental_quant/tokenizer/tokenizer.json"))
records = {r["id"]: r for r in json.loads((OUT / "corpus.json").read_text(encoding="utf8"))}


def load(name):
    return json.loads((OUT / name).read_text(encoding="utf8"))


def tokens(text):
    return len(tokenizer.encode(text, add_special_tokens=False).ids)


def reconstruct(text, edits):
    chunks = re.findall(r"[^。；\n]*[。；\n]|[^。；\n]+$", text)
    # Apply saved edits backwards, independently of the producer's forward builder.
    for edit in reversed(edits):
        chunks[edit["base_start"]:edit["base_end"]] = edit["replacement"]
    return "".join(chunks)


candidates = load("candidates.json")
for item in candidates:
    assert reconstruct(item["text_b"], item["edits"]) == item["text_a"]
selected = load("lossless_diff_examples.json")
for item in selected:
    assert reconstruct(records[item["base_source"]]["text"], item["edits"]) == records[item["source"]]["text"]

# Decode saved human-readable previews rather than trusting producer booleans.
assert all("\n" not in r["text"] for r in records.values())
preview_restored, dictionary_restored = set(), set()
for code in ("600039", "600820", "000568"):
    preview = (OUT / (code + "_policies_preview.md")).read_text(encoding="utf8")
    headings = list(re.finditer(r"(?m)^### (P\d+)｜C\d+｜主题 [^\n]*｜版本 ([^\n]+)\n", preview))
    anchors = {}
    for index, heading in enumerate(headings):
        body = preview[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(preview)].strip("\n")
        if body.startswith("原问题："):
            body = body.split("\n", 1)[1]
        if body.startswith("以 P"):
            instruction, delta = body.split("\n", 1)
            base_id = re.match(r"以 (P\d+) 原文为基础", instruction)[1]
            edits = []
            for line in [] if delta == "逐字相同" else delta.splitlines():
                replacement = re.fullmatch(r"原第(\d+)—(\d+)句替换：(.*)", line)
                insertion = re.fullmatch(r"在第(\d+)句后插入：(.*)", line)
                assert replacement or insertion
                start = int(replacement[1]) - 1 if replacement else int(insertion[1])
                end = int(replacement[2]) if replacement else start
                value = replacement[3] if replacement else insertion[2]
                edits.append({"base_start": start, "base_end": end, "replacement": [] if value == "（删除）" else [value]})
            restored = reconstruct(anchors[base_id], edits)
        else:
            restored = body
            anchors[heading[1]] = body
        for source in heading[2].split(","):
            assert restored == records[source]["text"], source
            preview_restored.add(source)

    packed = (OUT / (code + "_sentence_dictionary_preview.md")).read_text(encoding="utf8")
    policy_heads = list(re.finditer(r"(?m)^(\d{6}_\d{4}_p\d+)\n", packed))
    library = packed[packed.index("\n") + 1:policy_heads[0].start()]
    assert library.endswith("\n\n")
    library = library[:-2]
    markers = list(re.finditer(r"(?m)^(S\d+)：", library))
    dictionary = {m[1]: library[m.end():markers[k + 1].start() - 1 if k + 1 < len(markers) else len(library)] for k, m in enumerate(markers)}
    for k, heading in enumerate(policy_heads):
        segment = packed[heading.end():policy_heads[k + 1].start() if k + 1 < len(policy_heads) else len(packed)]
        assert segment.endswith("\n")
        restored = re.sub(r"〔(S\d+)〕", lambda m: dictionary[m[1]], segment[:-1])
        assert restored == records[heading[1]]["text"], heading[1]
        dictionary_restored.add(heading[1])
assert preview_restored == dictionary_restored == set(records)

accounting = []
for code in ("600039", "600820", "000568"):
    text = (STAGE2 / code / "模型正文.md").read_text(encoding="utf8")
    pieces = re.split(r"(?m)^(## [^\n]+)\n", text)
    counts = Counter(line for line in text.splitlines() if line.strip())
    repeated = sorted(((line, n) for line, n in counts.items() if n > 1), key=lambda p: -tokens(p[0]) * (p[1] - 1))
    accounting.append({"code": code, "body_tokens": tokens(text), "table_and_intro_tokens": tokens(pieces[0]),
                       "narrative_sections": [{"title": pieces[i][3:], "tokens": tokens(pieces[i + 1])} for i in range(1, len(pieces), 2)],
                       "exact_extra_line_tokens_diagnostic_only": sum(tokens(line) * (n - 1) for line, n in repeated),
                       "top_repeated_lines": [{"line": line, "occurrences": n, "extra_tokens": tokens(line) * (n - 1)} for line, n in repeated[:8]]})
hashes = load("input_hashes.json")
changed = [name for name, digest in hashes.items() if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
assert not changed
earlier = json.loads((ROOT / ".tmp/research_retirement_20261024_131928/before.json").read_text(encoding="utf8"))["protected"]
earlier_changed = [name for name, digest in earlier.items() if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest]
assert not earlier_changed
result = {"persisted_candidate_edit_roundtrips": len(candidates), "persisted_selected_edit_roundtrips": len(selected),
          "independent_preview_policy_text_roundtrips": len(preview_restored), "independent_dictionary_text_roundtrips": len(dictionary_restored),
          "earlier_protected_files": len(earlier), "earlier_changed_inputs": earlier_changed,
          "protected_files": len(hashes), "changed_inputs": changed, "body_accounting": accounting,
          "limitation": "Edit reconstruction validates original policy strings; it does not establish semantic equivalence, financial correctness or model reading quality."}
with (OUT / "verification_complete.json").open("x", encoding="utf8") as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2)
print(json.dumps({"roundtrips": len(candidates) + len(selected), "changed_inputs": changed,
                  "body_tokens": sum(p["body_tokens"] for p in accounting), "table_and_intro_tokens": sum(p["table_and_intro_tokens"] for p in accounting)}, ensure_ascii=False))
