"""Compare POISK patent IDs against a pinned NaturalAntibody family snapshot.

Downloads are public, cached and hashed. Family absence is never inferred from a
failed lookup. This compares document/family metadata, not sequence novelty.
Install duckdb into a task-local directory to read the downloaded Parquet files.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "runs/patent_family_overlap_20261030"
PAD_FAMILY_FOLDER = "1TPkAgKEoRxO7bvZNFC_9lpKPyDWXZwXc"
PAD_DELTA_URL = "https://drive.google.com/uc?export=download&id=1q_4vjgG5StAkwVnbTqukq7Bln9DVHXds"
POISK_BASE = "https://poisk-explorer.github.io/"
POISK_INDEX = "data/md5-f0c1f098abf1756e4b9891aa4dc028ee-c400/search.json.gz"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(url, path, expected_size=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if expected_size is not None and path.stat().st_size != expected_size:
            raise ValueError(f"Cached size mismatch: {path}")
        return path
    errors = []
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SDA-family-comparison/1.0"})
            tmp = path.with_name(path.name + ".tmp")
            with urllib.request.urlopen(req, timeout=60) as response, tmp.open("wb") as f:
                while block := response.read(1024 * 1024):
                    f.write(block)
                headers = {k: v for k, v in response.headers.items()
                           if k.lower() in {"content-type", "content-length", "last-modified", "etag"}}
                final_url = response.url
            if expected_size is not None and tmp.stat().st_size != expected_size:
                raise ValueError(f"Downloaded size {tmp.stat().st_size}, expected {expected_size}")
            if path.suffix == ".parquet":
                with tmp.open("rb") as f:
                    start = f.read(4)
                    f.seek(-4, 2)
                    end = f.read(4)
                if start != b"PAR1" or end != b"PAR1":
                    raise ValueError("Download is not a Parquet file")
            tmp.replace(path)
            write_json(path.with_name(path.name + ".download.json"), {
                "url": url, "final_url": final_url, "retrieved_at_utc": utc_now(),
                "bytes": path.stat().st_size, "sha256": sha256(path), "headers": headers,
            })
            return path
        except Exception as exc:
            errors.append(str(exc))
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"Failed to download {url}: {errors}")


def parse_drive_listing(html):
    match = re.search(r"window\['_DRIVE_ivd'\] = '(.*?)';", html)
    if not match:
        raise ValueError("Public Drive listing is unavailable")
    payload = json.loads(ast.literal_eval("'" + match[1] + "'"))
    return [{"id": row[0], "name": row[2], "mime": row[3],
             "bytes": row[13] if len(row) > 13 else None} for row in payload[0]]


def fetch(run):
    source = run / "sources"
    html = download("https://drive.google.com/drive/folders/" + PAD_FAMILY_FOLDER,
                    source / "patent_family_listing.html")
    listing = parse_drive_listing(html.read_text())
    by_name = {row["name"]: row for row in listing}
    delta = download(PAD_DELTA_URL, source / "pad_family_delta_v0.json")
    actions = [json.loads(line) for line in delta.read_text().splitlines()]
    # Pin the complete version-0 transaction, not a glob over arbitrary files.
    active = {}
    for action in actions:
        if "add" in action:
            active[action["add"]["path"]] = action["add"]
        if "remove" in action:
            active.pop(action["remove"]["path"], None)
    files = []
    for name, add in active.items():
        if name not in by_name:
            raise ValueError(f"Drive listing incomplete: active Delta file missing: {name}")
        entry = by_name[name]
        if entry["bytes"] != add["size"]:
            raise ValueError(f"Delta/Drive size disagreement: {name}")
        files.append({**entry, "expected_rows": json.loads(add["stats"])["numRecords"]})
    def get_file(row):
        p = download("https://drive.google.com/uc?export=download&id=" + row["id"],
                     source / "pad_family" / row["name"], row["bytes"])
        print(f"Downloaded {p.name}: {p.stat().st_size:,} bytes", flush=True)
        return {**row, "sha256": sha256(p)}
    with ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(get_file, files))
    download(POISK_BASE + POISK_INDEX, source / "poisk_search.json.gz")
    commits = [x["commitInfo"] for x in actions if "commitInfo" in x]
    write_json(source / "snapshot.json", {
        "pad_family_folder": PAD_FAMILY_FOLDER, "pad_delta_version": 0,
        "delta_commits": commits, "files": files,
        "expected_family_rows": sum(x["expected_rows"] for x in files),
        "poisk_index_url": POISK_BASE + POISK_INDEX,
        "poisk_index_sha256": sha256(source / "poisk_search.json.gz"),
    })


def normalize_us_number(value):
    """Normalize only explicit US document numbers; never confuse A1 with grants."""
    value = re.sub(r"[\s,.-]", "", str(value).upper())
    match = re.fullmatch(r"(?:US)?(\d+)(?:[A-Z]\d*)?", value)
    if not match:
        return None
    return match[1].lstrip("0") or "0"


def us_doc_number(doc):
    country = (doc.get("country") or "").upper()
    number = str(doc.get("doc-number") or "").upper()
    if country == "US" or number.startswith("US"):
        return normalize_us_number(number)
    return None


def csv_write(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_poisk(path):
    index = json.loads(gzip.decompress(Path(path).read_bytes()))
    fields = index["fields"]
    pat_idx = fields.index("patent_id")
    record_idx = fields.index("unique_record_id")
    records = defaultdict(list)
    seen = set()
    for row in index["rows"]:
        if len(row) != len(fields) or row[record_idx] in seen:
            raise ValueError("Malformed or duplicate POISK index row")
        seen.add(row[record_idx])
        patent = normalize_us_number(row[pat_idx])
        if patent is None:
            raise ValueError(f"Unrecognized POISK patent ID: {row[pat_idx]}")
        records[patent].append(row[record_idx])
    return records


def load_pad(run):
    sys.path.insert(0, str(run / "dependencies"))
    import duckdb
    snapshot = json.loads((run / "sources/snapshot.json").read_text())
    paths = [str(run / "sources/pad_family" / row["name"]) for row in snapshot["files"]]
    for path, row in zip(paths, snapshot["files"]):
        if sha256(path) != row["sha256"]:
            raise ValueError(f"PAD source checksum mismatch: {path}")
    if sha256(run / "sources/poisk_search.json.gz") != snapshot["poisk_index_sha256"]:
        raise ValueError("POISK source checksum mismatch")
    db = duckdb.connect()
    # Do not materialize sequences/CDR maps, which are irrelevant to family overlap.
    rows = db.read_parquet(paths).select("family_id, docs, meta").fetchall()
    assert len(rows) == snapshot["expected_family_rows"], "Delta row count mismatch"
    families = {}
    member_index = defaultdict(set)
    for family_id, docs, meta in rows:
        family_id = str(family_id)
        if family_id in families:
            raise ValueError(f"Duplicate PAD family ID: {family_id}")
        docs = (docs or {}).get("data") or []
        meta = meta or {}
        us_numbers = set()
        for doc in docs:
            number = us_doc_number(doc)
            if number:
                us_numbers.add(number)
        representative = meta.get("name") or ""
        if representative.upper().startswith("US"):
            number = normalize_us_number(representative)
            if number:
                us_numbers.add(number)
        for number in us_numbers:
            member_index[number].add(family_id)
        dates = [int(doc["date"]) for doc in docs if doc.get("date")]
        families[family_id] = {
            "family_id": family_id, "representative": representative,
            "title": (meta.get("title") or {}).get("_VALUE") or "",
            "docs_count": len(docs), "us_numbers": sorted(us_numbers),
            "earliest_listed_publication": min(dates) if dates else "",
            "latest_listed_publication": max(dates) if dates else "",
            "listed_countries": sorted({doc["country"] for doc in docs if doc.get("country")}),
        }
    db.close()
    return families, member_index


def parse_google_evidence(text):
    """Read only explicit family headings bound to the requested patent URL.

    A cited patent in the body cannot become the requested document. SEQ ID NOs,
    priority claims, related-child applications and country-status links cannot
    become family IDs. Retain conflicts instead of choosing the last result.
    """
    records = []
    for section in text.split("--------------------------------------------------------------------------------"):
        source = re.search(r'Source: (?:find|open)\(\{"ref_id":"([^"]+)"', section)
        if not source:
            continue
        url = source[1]
        patent = re.search(r"/patent/(US\d+[A-Z]\d*)(?:/|$)", url)
        if not patent:
            continue
        family_ids = set(re.findall(r"^L\d+:\s*##\s+ID\s*=\s*(\d+)\s*$",
                                    section, flags=re.MULTILINE))
        if len(family_ids) == 1:
            records.append({"poisk_patent_id": normalize_us_number(patent[1]),
                            "family_id": next(iter(family_ids)), "source_url": url,
                            "publication": patent[1]})
    return records


def load_google(run):
    results = defaultdict(list)
    for path in sorted((run / "sources").glob("google_family_*.txt")):
        for row in parse_google_evidence(path.read_text()):
            results[row["poisk_patent_id"]].append({**row, "evidence_file": path.name})
    return results


def analyze(run):
    families, member_index = load_pad(run)
    poisk = load_poisk(run / "sources/poisk_search.json.gz")
    google = load_google(run)
    mappings = []
    matched_families = set()
    outside_families = set()
    family_patents = defaultdict(set)
    status_counts = Counter()
    for patent in sorted(poisk, key=int):
        candidates = sorted(member_index.get(patent, set()))
        evidence = google.get(patent, [])
        google_ids = sorted({row["family_id"] for row in evidence})
        status = "matched_pad_member" if len(candidates) == 1 else (
            "ambiguous_pad_member" if candidates else "not_found_in_pad_members")
        resolved = None
        if len(candidates) == 1:
            if google_ids and google_ids != candidates:
                status = "family_id_disagreement"
            else:
                resolved = candidates[0]
        elif not candidates and len(google_ids) == 1:
            resolved = google_ids[0]
            status = "matched_google_family_id" if resolved in families else "google_family_outside_pad"
        elif len(google_ids) > 1:
            status = "conflicting_google_family_ids"
        if resolved:
            family_patents[resolved].add(patent)
            (matched_families if resolved in families else outside_families).add(resolved)
        status_counts[status] += 1
        mappings.append({"poisk_patent_id": patent, "status": status,
                         "pad_family_ids": "|".join(candidates),
                         "resolved_family_id": resolved or "",
                         "google_family_ids": "|".join(google_ids),
                         "google_source_urls": "|".join(sorted({x["source_url"] for x in evidence})),
                         "google_evidence_files": "|".join(sorted({x["evidence_file"] for x in evidence})),
                         "poisk_record_count": len(poisk[patent])})
    family_rows = []
    for family_id, row in sorted(families.items(), key=lambda kv: int(kv[0])):
        hits = sorted(family_patents.get(family_id, set()), key=int)
        family_rows.append({
            **{k: v for k, v in row.items() if k not in {"us_numbers", "listed_countries"}},
            "listed_countries": "|".join(row["listed_countries"]),
            "us_numbers": "|".join(row["us_numbers"]),
            "poisk_patent_ids": "|".join(hits),
            "status": "confirmed_overlap" if family_id in matched_families else "not_confirmed_overlap",
        })
    csv_write(run / "poisk_patent_mapping.csv", mappings, list(mappings[0]))
    fields = list(family_rows[0])
    csv_write(run / "pad_families.csv", family_rows, fields)
    csv_write(run / "pad_no_member_match.csv",
              [x for x in family_rows if x["status"] == "no_match_in_snapshot_members"], fields)
    unresolved = [x for x in mappings if not x["resolved_family_id"]]
    csv_write(run / "poisk_unmatched.csv", unresolved,
              list(mappings[0]))
    outside_rows = [{"family_id": fid, "poisk_patent_ids": "|".join(sorted(family_patents[fid], key=int))}
                    for fid in sorted(outside_families, key=int)]
    csv_write(run / "poisk_families_outside_pad.csv", outside_rows, ["family_id", "poisk_patent_ids"])
    overlap_lower = len(matched_families)
    overlap_upper = min(len(families), overlap_lower + len(unresolved))
    summary = {
        "generated_at_utc": utc_now(), "stage": "membership_and_google_evidence",
        "pad_families": len(families), "poisk_patents": len(poisk),
        "poisk_records": sum(map(len, poisk.values())),
        "poisk_mapping_status": dict(status_counts),
        "confirmed_overlap_families": len(matched_families),
        "confirmed_overlap_fraction_of_pad": len(matched_families) / len(families),
        "poisk_resolved_families": len(family_patents),
        "poisk_total_family_bounds": [len(family_patents), len(family_patents) + len(unresolved)],
        "poisk_families_outside_pad_snapshot": len(outside_families),
        "poisk_outside_pad_family_bounds": [len(outside_families), len(outside_families) + len(unresolved)],
        "unresolved_poisk_patents": len(unresolved),
        "overlap_families_lower_bound": overlap_lower,
        "overlap_families_upper_bound": overlap_upper,
        "overlap_fraction_of_pad_bounds": [overlap_lower / len(families), overlap_upper / len(families)],
        "pad_families_without_poisk_bounds": [len(families) - overlap_upper, len(families) - overlap_lower],
        "bounds_assumption": "Each unresolved POISK patent maps to at most one family in the fixed PAD family-ID system.",
        "pad_with_no_docs": sum(not row["docs_count"] for row in families.values()),
        "pad_with_no_us_number": sum(not row["us_numbers"] for row in families.values()),
        "pad_listed_publication_date_range": [
            min(x["earliest_listed_publication"] for x in families.values() if x["earliest_listed_publication"]),
            max(x["latest_listed_publication"] for x in families.values() if x["latest_listed_publication"]),
        ],
        "limits": [
            "No-match is not proven family absence: PAD member lists are a historical snapshot.",
            "Ambiguous patent-to-family mappings are not silently resolved or counted as unique matches.",
            "This compares patent/family metadata, not sequence or experiment coverage.",
        ],
    }
    write_json(run / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def report(run):
    summary = json.loads((run / "summary.json").read_text())
    snapshot = json.loads((run / "sources/snapshot.json").read_text())
    google = load_google(run)
    controls = json.loads((run / "sources/google_control_plan.json").read_text())
    audit = []
    for control in controls:
        patent = control["poisk_patent_id"]
        actual = sorted({x["family_id"] for x in google.get(patent, [])})
        audit.append({"poisk_patent_id": patent, "pad_family_id": control["pad_family_ids"],
                      "google_family_ids": actual, "status": "not_retrieved" if not actual else
                      "agree" if actual == [control["pad_family_ids"]] else "disagree"})
    write_json(run / "control_audit.json", audit)
    status = Counter(x["status"] for x in audit)
    if status["disagree"]:
        raise ValueError("Control family IDs disagree; review the identifier system before reporting")
    lo, hi = summary["overlap_fraction_of_pad_bounds"]
    lower, upper = summary["pad_families_without_poisk_bounds"]
    total_lo, total_hi = summary["poisk_total_family_bounds"]
    outside_lo, outside_hi = summary["poisk_outside_pad_family_bounds"]
    commit = snapshot["delta_commits"][0]
    commit_time = datetime.fromtimestamp(commit["timestamp"] / 1000, timezone.utc).isoformat()
    counts = summary["poisk_mapping_status"]
    text = f"""PAD / NaturalAntibody 与 POISK 的专利家族覆盖对照
核查日期：2026-10-30（Asia/Shanghai）

本次结果
以 NaturalAntibody 官网所列的 25,019 个家族为固定比较集合：
POISK 已确认覆盖 {summary['confirmed_overlap_families']:,} 个家族，占 {lo:.2%}。
还有 {summary['unresolved_poisk_patents']:,} 个 POISK 专利号未解析；即使每个都补入一个此前未匹配的 PAD 家族，
交集也最多为 {summary['overlap_families_upper_bound']:,} 个家族，占 {hi:.2%}。
因此，这两个已发布集合在 PAD 分母下的家族交集为 {lo:.2%}–{hi:.2%}。
这个区间是未解析项产生的确定性上下界，不是抽样置信区间。

规模与去重
PAD 家族数：{summary['pad_families']:,}；POISK 专利号：{summary['poisk_patents']:,}；POISK 记录数：{summary['poisk_records']:,}。
POISK 直接通过 PAD 成员表匹配的专利号：{counts.get('matched_pad_member', 0):,}。
Google 家族编号补查后确认落入 PAD 的专利号：{counts.get('matched_google_family_id', 0):,}。
Google 家族编号补查后落在这份 PAD 集合之外的专利号：{counts.get('google_family_outside_pad', 0):,}。
POISK 已解析的不同家族：{summary['poisk_resolved_families']:,}；含未解析项的总家族数界限：{total_lo:,}–{total_hi:,}。
PAD 中尚未确认被 POISK 覆盖的候选家族：{summary['pad_families'] - summary['confirmed_overlap_families']:,}。
在本次固定家族编号口径下，PAD 中未被 POISK 覆盖的家族总数界限：{lower:,}–{upper:,}。
POISK 家族中落在这份 PAD 集合之外的数量界限：{outside_lo:,}–{outside_hi:,}。
尚未匹配的 PAD 候选名单不能逐条直接标成已证实独有，因为未解析专利可能补入其中部分家族。

固定输入与时间范围
PAD：官方 Drive 根目录下 patent_family 的 Delta 版本 0，共 9 个活动 Parquet 文件。
Delta 事务写入时间（UTC）：{commit_time}。
事务报告行数与实际读取行数均为 25,019；全部文件字节数及 SHA-256 保存在 sources/snapshot.json。
成员表中列出的最早/最晚公开日期：1984-04-05 / 2025-04-11。
该范围不是所有相关专利的最早/最晚公开日期，也不是实际源数据完整截止日。
官方根目录另含 PatentsDB 1.1 子目录；本次固定比较官网 25,019 家族对应的根目录快照，未混入另一版本。
POISK：v1.1，使用公开 Explorer 的全量 93,193 行搜索索引及其 10,010 个专利号。
索引地址内的源 CSV MD5 为 f0c1f098abf1756e4b9891aa4dc028ee，与本地保存的 v1.1 官方清单一致。
这里验证的是索引及清单的版本对应关系，没有重新下载 234 MB CSV 并逐行比对。

匹配方法
1. 从 Delta 活动文件读取 family_id、docs 和 meta，建立美国文献号到家族编号的索引。
2. 仅规范空白、标点和数字前导零；申请公开文献号与授权文献号不互相替换。
3. 同一篇专利只能对应一个家族；多个 POISK 专利号落入同一家族只计一个。
4. 对直接匹配失败的专利，通过 Google Patents 的 Family 区域读取明确的 ID。
   不把优先权、分案/延续申请列表、引证文献或正文中的 SEQ ID 当成家族编号。
5. 没有取到明确家族编号、访问失败或响应不完整的记录继续保留为未解析。
6. 抽取 24 个跨专利号范围的直接匹配记录检查编号兼容性：
   {status['agree']} 个取到 Google 编号且与 PAD 一致；{status['not_retrieved']} 个未取到；{status['disagree']} 个不一致。
   此检查只审计编号兼容性，不是抽取准确率盲评。
7. 上下界假设：每个尚未解析的 POISK 专利号在固定 PAD 家族体系下至多增加一个家族。
   若后续家族体系合并/拆分或来源数据被纠正，需重新计算。

两个可复核的补查例子
US12110324B2 -> Google 家族 88412302；PAD 已有该家族，代表文献为 US20240034780A1。
US12292442B2 -> Google 家族 71102495；PAD 已有该家族，代表文献为 WO2020125653。
两者在旧成员列表中都未直接命中授权号，不能因首次未命中就认为 PAD 没有该家族。

结论边界
这些数字比较的是 PAD 与 POISK 两个发布集合的专利家族文献覆盖。
25,019 不是全球完整抗体专利总数；约 23% 不是 GenBank 对全球抗体序列的覆盖率。
没有测量 VH/VL 序列重合、配对准确率、KD/IC50 关联或整篇专利实验召回率。
PAD 多出的候选家族不一定含可用的成对链或定量实验，也不代表 DatasetAgent 已经能够恢复。
PAD 中 {summary['pad_with_no_docs']:,} 个家族缺 docs；{summary['pad_with_no_us_number']:,} 个家族在现有字段中没有可解析美国文献号。
早期未命中、较新授权和快照成员不完整都可能影响匹配；未解析项保持公开。
Google 批量直连在本环境返回 503，EPO OPS 匿名探测返回 403；补查使用浏览工具，并保存返回证据。
本次没有调用抽取模型，没有发送邮件、申请账号或使用付费专利接口。

产物
summary.json：汇总、上下界和统计口径。
poisk_patent_mapping.csv：全部 10,010 个专利号、家族编号、匹配状态和 Google 证据文件。
pad_families.csv：全部 25,019 个家族及命中的 POISK 专利号。
pad_no_member_match.csv：未确认交集的 PAD 候选家族，不等于逐条确认独有。
poisk_families_outside_pad.csv：已解析且不在此 PAD 快照中的 POISK 家族。
poisk_unmatched.csv：尚未解析的 POISK 专利号。
control_audit.json：24 项编号兼容性抽查结果。
sources/：原始 Parquet、Delta 事务、POISK 索引、官方网页、许可和浏览补查证据。
provenance.json：来源地址、文件哈希、脚本版本、验证记录和使用许可。

离线重算（保留 sources/ 和任务目录依赖）
python src/patent_family_overlap.py analyze
python src/patent_family_overlap.py report

验证
python -m unittest discover -s tests -p 'test_patent_family_overlap.py' -v
五项测试覆盖同族重复计数、未知项上下界、申请/授权号区分、家族编号冲突，以及误用引证/SEQ ID 的风险。

公开来源
https://naturalantibody.com/patents-database/
https://drive.google.com/drive/folders/1VV32NVwwGRMk8o-22LawDi8L8MKF64zr
https://zenodo.org/records/21775623
https://poisk-explorer.github.io/
https://patents.google.com/patent/US12110324B2/en
https://patents.google.com/patent/US12292442B2/en
PAD 下载目录附 CC BY-NC 4.0 许可；原始 LICENSE.txt 随 sources/ 保留。
"""
    (run / "REPORT.txt").write_text(text)
    source_files = [{"path": str(p.relative_to(run)), "bytes": p.stat().st_size, "sha256": sha256(p)}
                    for p in sorted((run / "sources").rglob("*")) if p.is_file() and p.suffix != ".tmp"]
    initial = json.loads((run / "sources/initial_urls.json").read_text()) if (run / "sources/initial_urls.json").exists() else {}
    write_json(run / "provenance.json", {
        "generated_at_utc": utc_now(), "client_date": "2026-10-30", "client_timezone": "Asia/Shanghai",
        "script": str(Path(__file__).relative_to(ROOT)), "script_sha256": sha256(__file__),
        "initial_source_urls": initial, "pad_folder_url": "https://drive.google.com/drive/folders/" + PAD_FAMILY_FOLDER,
        "pad_delta_url": PAD_DELTA_URL, "pad_delta_version": 0,
        "pad_license": "CC BY-NC 4.0 (source LICENSE.txt)", "poisk_license": "CC BY 4.0",
        "files": source_files, "google_control_counts": dict(status),
        "validation": "5 focused unittest cases passed; source hashes and Delta row counts checked during analyze",
        "note": "Google family requests were made through the browsing tool; saved evidence permits offline replay.",
    })
    print(str(run / "REPORT.txt"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "analyze", "report"])
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    {"fetch": fetch, "analyze": analyze, "report": report}[args.command](args.run_dir)


if __name__ == "__main__":
    main()
