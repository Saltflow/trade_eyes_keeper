"""Isolated offline investigation; never changes the production pipeline or inputs."""

import hashlib
import json
import re
import socket
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import sklearn
from sklearn.decomposition import LatentDirichletAllocation
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from threadpoolctl import threadpool_limits
from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent / "run"
OUT.mkdir(parents=True, exist_ok=False)
SOURCE = ROOT / "data/fundamental_data/three_companies_fixed4_20261024_115306"
STAGE2 = ROOT / "data/fundamental_data/stage2_model_context_20261024_125223_final"
sys.path.insert(0, str(ROOT))
from src.fundamental_data.model_context import describe


def denied(*args, **kwargs):
    raise RuntimeError("offline_research_network_forbidden")


socket.socket.connect = denied
socket.create_connection = denied


def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save(name, value):
    path = OUT / name
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=lambda v: v.item())
    with path.open("x", encoding="utf8", newline="\n") as stream:
        stream.write(content)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sentences(text):
    # Keep every original character, punctuation, whitespace and order.
    return re.findall(r"[^。；\n]*[。；\n]|[^。；\n]+$", text)


def patch(base, current):
    a, b = sentences(base), sentences(current)
    assert "".join(a) == base and "".join(b) == current
    edits, text, rebuilt, cursor = [], [], [], 0
    for tag, i, j, k, m in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        edits.append({"base_start": i, "base_end": j, "replacement": b[k:m]})
        location = f"在第{i}句后插入" if i == j else f"原第{i + 1}—{j}句替换"
        text.append(location + "：" + ("".join(b[k:m]) if k != m else "（删除）"))
        rebuilt.extend(a[cursor:i])
        rebuilt.extend(b[k:m])
        cursor = j
    rebuilt.extend(a[cursor:])
    assert "".join(rebuilt) == current
    return "\n".join(text) or "逐字相同", edits


protected = list((ROOT / "src/fundamental_data").rglob("*.py"))
protected += [ROOT / "config/fundamental_data_v2.json", ROOT / "scripts/build_fundamental_data_v2.py", ROOT / "scripts/build_financial_context.py"]
protected += list(SOURCE.rglob("*")) + list(STAGE2.rglob("*"))
protected = [p for p in protected if p.is_file() and "__pycache__" not in p.parts]
hashes = {str(p.relative_to(ROOT)): digest(p) for p in protected}
save("input_hashes.json", hashes)
tokenizer = Tokenizer.from_file(str(ROOT / "data/fundamental_quant/tokenizer/tokenizer.json"))


def tokens(text):
    return len(tokenizer.encode(text, add_special_tokens=False).ids)


records = []
for path in sorted((SOURCE / "stage1").glob("*/data.json")):
    code, year = path.parent.name.split("_")
    data = json.loads(path.read_text(encoding="utf8"))
    for index, item in enumerate(data.get("policies", [])):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            records.append({"id": f"{code}_{year}_p{index + 1}", "code": code, "year": year, "index": index, **item})
save("corpus.json", records)
texts = [p["text"] for p in records]
started = time.perf_counter()
vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 5), lowercase=False, dtype=np.float64)
matrix = vectorizer.fit_transform(texts)
similarity = cosine_similarity(matrix)
tf_seconds = time.perf_counter() - started
pairs = [(i, j) for i in range(len(records)) for j in range(i) if records[i]["code"] == records[j]["code"]]


def context(item):
    return stable({"code": item["code"], "scope": item.get("scope"), "accounting_basis": item.get("accounting_basis")})


def pair_info(i, j):
    a, b = records[i], records[j]
    delta, edits = patch(b["text"], a["text"])
    sa, sb = set(a["text"][k:k + 5] for k in range(max(0, len(a["text"]) - 4))), set(b["text"][k:k + 5] for k in range(max(0, len(b["text"]) - 4)))
    return {"a": a["id"], "b": b["id"], "cosine": float(similarity[i, j]), "char5_jaccard": len(sa & sb) / max(1, len(sa | sb)),
            "exact_text": a["text"] == b["text"], "same_context": context(a) == context(b),
            "topics": [a.get("topic"), b.get("topic")], "text_a": a["text"], "text_b": b["text"], "delta": delta, "edits": edits}


candidates = [pair_info(i, j) for i, j in pairs if similarity[i, j] >= .75]
save("candidates.json", sorted(candidates, key=lambda p: -p["cosine"]))
thresholds = []
for threshold in (.75, .85, .90, .95, .98):
    selected = [p for p in candidates if p["cosine"] >= threshold]
    thresholds.append({"threshold": threshold, "pairs": len(selected), "exact_text": sum(p["exact_text"] for p in selected),
                       "changed_text": sum(not p["exact_text"] for p in selected), "context_differs": sum(not p["same_context"] for p in selected)})

# LDA is a topic-space comparison baseline, not a deletion policy.
counts = CountVectorizer(analyzer="char", ngram_range=(2, 4), min_df=2, max_features=20000, lowercase=False).fit_transform(texts)
lda_results, lda_examples = [], []
with threadpool_limits(limits=1):
    for k, seed in ((6, 0), (12, 0), (12, 1)):
        started = time.perf_counter()
        model = LatentDirichletAllocation(n_components=k, max_iter=20, learning_method="batch", random_state=seed, n_jobs=1)
        topic = model.fit_transform(counts)
        scores = cosine_similarity(topic)
        selected = [(i, j) for i, j in pairs if scores[i, j] >= .98]
        lda_results.append({"topics": k, "seed": seed, "seconds": time.perf_counter() - started, "pairs_above_098": len(selected),
                            "pairs_with_tfidf_below_050": sum(similarity[i, j] < .50 for i, j in selected)})
        if seed == 0 and k == 12:
            lda_examples = [{"a": records[i]["id"], "b": records[j]["id"], "lda_cosine": float(scores[i, j]), "tfidf_cosine": float(similarity[i, j]),
                             "text_a": texts[i], "text_b": texts[j]} for i, j in selected if similarity[i, j] < .20][:8]
save("lda_counterexamples.json", lda_examples)

# Contrasts are explicitly synthetic probes, never financial source data.
probes = []
base = next(t for t in texts if len(t) > 250 and "确认" in t)
probes.append({"name": "否定词改变", "a": base, "b": base.replace("确认", "不确认", 1), "equivalent": False})
base = next(t for t in texts if len(t) > 200 and re.search(r"\d", t))
probes.append({"name": "数字改变", "a": base, "b": re.sub(r"\d", lambda m: str((int(m[0]) + 1) % 10), base, count=1), "equivalent": False})
base = next(t for t in texts if len(t) > 150 and "万元" in t)
probes.append({"name": "单位改变", "a": base, "b": base.replace("万元", "百万元", 1), "equivalent": False})
base = next(t for t in texts if len(t) > 150 and "本公司" in t)
probes.append({"name": "主体范围改变", "a": base, "b": base.replace("本公司", "本集团", 1), "equivalent": False})
for p in probes:
    v = vectorizer.transform([p["a"], p["b"]])
    p["tfidf_cosine"] = float(cosine_similarity(v)[0, 1])
    p["delta"], edits = patch(p["a"], p["b"])
    p["roundtrip_passed"] = True
save("synthetic_contrasts.json", probes)

company_results, examples = [], []
for code in sorted({p["code"] for p in records}):
    indices = [i for i, p in enumerate(records) if p["code"] == code]
    # Replicate current renderer grouping exactly, including distinctions across topics.
    unique, before_parts = {}, []
    for i in indices:
        fields = {k: v for k, v in records[i].items() if k not in {"id", "code", "year", "index", "reported_in", "issues"}}
        key = stable(fields)
        if key not in unique:
            unique[key] = {"index": i, "sources": [], "issues": []}
        group = unique[key]
        group["sources"].append(records[i]["id"])
        for issue in records[i].get("issues", []):
            if issue not in group["issues"]:
                group["issues"].append(issue)
    # Actual prior stage-two section, for the headline before/after comparison.
    original = (STAGE2 / code / "模型正文.md").read_text(encoding="utf8")
    section = original.split("## 政策与口径版本\n", 1)[1].split("\n## ", 1)[0]
    contexts, full_parts, exact_parts, diff_parts, bases = {}, [], [], [], []
    exact_lookup = {}
    for n, group in enumerate(unique.values(), 1):
        i = group["index"]
        p = records[i]
        ctx = context(p)
        cid = contexts.setdefault(ctx, "C" + str(len(contexts) + 1))
        pid = "P" + str(n)
        header = f"### {pid}｜{cid}｜主题 {p.get('module')}/{p.get('topic')}｜版本 {','.join(group['sources'])}\n"
        if group["issues"]:
            header += "原问题：" + describe(group["issues"]) + "\n"
        full = header + p["text"] + "\n"
        full_parts.append(full)
        exact_key = (ctx, p["text"])
        reference = exact_lookup.get(exact_key)
        exact_parts.append(header + (f"正文逐字同 {reference}。\n" if reference else p["text"] + "\n"))
        exact_lookup.setdefault(exact_key, pid)
        options = [(tokens(full), full, None)]
        # Only compare with full-text anchors: never build a delta reference chain.
        for j, bid in bases:
            if context(records[j]) != ctx or similarity[i, j] < .85:
                continue
            delta, edits = patch(records[j]["text"], p["text"])
            variant = header + f"以 {bid} 原文为基础；句号/分号/换行分句，其他句与顺序不变。\n" + delta + "\n"
            options.append((tokens(variant), variant, {"id": pid, "base": bid, "source": p["id"], "base_source": records[j]["id"], "cosine": float(similarity[i, j]), "delta": delta, "edits": edits}))
        _, chosen, info = min(options, key=lambda x: x[0])
        diff_parts.append(chosen)
        if info:
            examples.append(info)
        else:
            bases.append((i, pid))
    context_text = "\n".join(f"{cid}：" + describe(json.loads(ctx)) for ctx, cid in contexts.items()) + "\n\n"
    prefix = "# " + code + " 政策原文与版本差异（研究样例）\n\n"
    full_text, exact_text, diff_text = [prefix + context_text + "\n".join(p) for p in (full_parts, exact_parts, diff_parts)]
    save(code + "_policies_preview.md", diff_text)
    company_results.append({"code": code, "input_policies": len(indices), "existing_versions": len(unique), "context_count": len(contexts),
                            "existing_policy_section_tokens": tokens(section), "context_factored_full_tokens": tokens(full_text), "exact_reuse_tokens": tokens(exact_text),
                            "tfidf_sentence_diffs_tokens": tokens(diff_text), "delta_versions": sum(e["source"].startswith(code + "_") for e in examples),
                            "all_policy_texts_roundtrip": True})

save("lossless_diff_examples.json", examples)
after = {str(p.relative_to(ROOT)): digest(p) for p in protected}
assert hashes == after
summary = {"paid_api_calls": 0, "network": "blocked", "input_files": len(protected), "input_hashes_unchanged": hashes == after,
           "python": sys.version, "sklearn": sklearn.__version__, "numpy": np.__version__, "tokenizer_sha256": digest(ROOT / "data/fundamental_quant/tokenizer/tokenizer.json"),
           "policy_count": len(records), "within_company_pairs": len(pairs), "tfidf_seconds": tf_seconds, "tfidf_features": len(vectorizer.vocabulary_),
           "tfidf_sparse_bytes": matrix.data.nbytes + matrix.indices.nbytes + matrix.indptr.nbytes, "thresholds": thresholds,
           "lda": lda_results, "companies": company_results, "all_diffs_reconstruct_original": True,
           "limits": ["Policy paragraphs only; tables and failed responses are untouched.", "Thresholds are experimental candidate filters, not semantic equivalence judgments.", "No independent labeled benchmark or statistical accuracy estimate.", "Sentence references are lossless but require separate readability acceptance."]}
save("results.json", summary)
print(json.dumps(summary, ensure_ascii=False, indent=2, default=lambda v: v.item()))
