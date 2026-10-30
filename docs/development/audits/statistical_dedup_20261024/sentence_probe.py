"""Secondary investigation: exact sentence reuse and conservative text-only diffs."""

import json
import re
import time
from collections import Counter
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from tokenizers import Tokenizer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OUT = HERE / "run"
records = json.loads((OUT / "corpus.json").read_text(encoding="utf8"))
tokenizer = Tokenizer.from_file(str(ROOT / "data/fundamental_quant/tokenizer/tokenizer.json"))


def tokens(text):
    return len(tokenizer.encode(text, add_special_tokens=False).ids)


def split(text):
    parts = re.findall(r"[^。；\n]*[。；\n]|[^。；\n]+$", text)
    assert "".join(parts) == text
    return parts


def write(name, value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    with (OUT / name).open("x", encoding="utf8", newline="\n") as stream:
        stream.write(text)


started = time.perf_counter()
sentences = [{"source": p["id"], "code": p["code"], "index": k + 1, "text": s}
             for p in records for k, s in enumerate(split(p["text"])) if len(s) >= 15]
v = TfidfVectorizer(analyzer="char", ngram_range=(2, 5), lowercase=False)
matrix = v.fit_transform([s["text"] for s in sentences])
scores = cosine_similarity(matrix)
near, totals = [], []
for threshold in (.75, .85, .95):
    counts = Counter()
    for i in range(len(sentences)):
        for j in range(i):
            a, b = sentences[i], sentences[j]
            if a["code"] != b["code"] or a["source"] == b["source"] or scores[i, j] < threshold:
                continue
            counts["pairs"] += 1
            if a["text"] == b["text"]:
                counts["exact"] += 1
            else:
                counts["changed"] += 1
                if threshold == .85:
                    near.append({"a": a, "b": b, "cosine": float(scores[i, j])})
    totals.append({"threshold": threshold, **dict(counts)})
write("sentence_candidates.json", sorted(near, key=lambda p: -p["cosine"]))
companies = []
for code in sorted({p["code"] for p in records}):
    items = [p for p in records if p["code"] == code]
    frequencies = Counter(s for p in items for s in split(p["text"]))
    repeated = {s: "S" + str(i + 1) for i, (s, count) in enumerate(frequencies.items()) if count > 1 and tokens(s) >= 25}
    # Compare identical version headings and text-only payloads, with explicit full texts.
    full = "\n".join(p["id"] + "\n" + p["text"] for p in items)
    packed = "共用句库（仅复用原文，适用范围仍逐项保留）：\n" + "\n".join(k + "：" + s for s, k in repeated.items()) + "\n\n"
    for p in items:
        chunks = split(p["text"])
        replacements = ["〔" + repeated[s] + "〕" if s in repeated else s for s in chunks]
        rebuilt = "".join(s for s in chunks)
        assert rebuilt == p["text"]
        packed += p["id"] + "\n" + "".join(replacements) + "\n"
    companies.append({"code": code, "sentences_total": sum(len(split(p["text"])) for p in items), "exact_repeated_sentences": len(repeated),
                      "plain_text_tokens": tokens(full), "exact_sentence_dictionary_tokens": tokens(packed), "savings": tokens(full) - tokens(packed),
                      "reconstruction_passed": True})
    write(code + "_sentence_dictionary_preview.md", packed)
write("sentence_results.json", {"sentence_count_ge15chars": len(sentences), "seconds": time.perf_counter() - started,
                                "thresholds": totals, "companies": companies,
                                "limits": "Text-only payload comparison, not the whole model body; no semantic deletion or source modification."})
print(json.dumps({"sentences": len(sentences), "thresholds": totals, "companies": companies}, ensure_ascii=False, indent=2))
