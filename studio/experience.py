"""Portable, prepared cases alongside the normal upload and review workspace."""
import gzip
import hashlib
import json
from pathlib import Path

from studio.sda_data import Workspace, read_json


class ExperienceWorkspace(Workspace):
    def __init__(self, root):
        super().__init__(root)
        self.experience = read_json(self.root / "demo/manifest.json", {})
        self.cases = {case["document_id"]: case for case in self.experience.get("cases", [])}
        self._seed_cases()

    def case_directory(self, identity):
        directory = (self.root / "demo/cases" / identity).resolve()
        if not directory.is_relative_to((self.root / "demo/cases").resolve()):
            raise ValueError("Invalid case directory")
        return directory

    def builtin_locations(self):
        locations = super().builtin_locations()
        for identity in self.cases:
            directory = self.case_directory(identity)
            locations[identity] = (directory, directory)
        return locations

    def _prepared_bundle(self, identity):
        directory = self.case_directory(identity)
        with gzip.open(directory / "bundle.json.gz", "rt", encoding="utf-8") as stream:
            bundle = json.load(stream)
        bundle["pdf"] = directory / "source.pdf"
        bundle["crops"] = {key: self._case_asset(directory, value) for key, value in bundle["crops"].items()}
        bundle["structured_files"] = {}
        return bundle

    def _case_asset(self, directory, name):
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError("Missing case asset")
        return path

    def _build(self, identity, result_dir, input_dir):
        if identity in self.cases and result_dir == self.case_directory(identity):
            return self._prepared_bundle(identity)
        return super()._build(identity, result_dir, input_dir)

    def _seed_cases(self):
        if not self.cases:
            return
        package = self.experience["package_id"]
        with self.lock, self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS experience_packages (package_id TEXT PRIMARY KEY)")
            if db.execute("SELECT 1 FROM experience_packages WHERE package_id=?", (package,)).fetchone():
                return
            for identity in self.cases:
                bundle = self._prepared_bundle(identity)
                for record in bundle["records"]:
                    db.execute("INSERT OR IGNORE INTO publications VALUES (?, ?, ?, ?)",
                               ("SDA", identity, record["record_id"], json.dumps(record, ensure_ascii=False)))
            db.execute("UPDATE folders SET name=? WHERE dataset_id='SDA'", ("体验案例",))
            db.execute("INSERT INTO experience_packages VALUES (?)", (package,))

    def patents(self):
        patents = super().patents()
        for patent in patents:
            case = self.cases.get(patent["document_id"])
            if case:
                patent.update(case_title=case["title"], filename=case["filename"])
        return patents

    def validate_cases(self):
        import pymupdf
        for identity, case in self.cases.items():
            directory = self.case_directory(identity)
            for name, expected in case["files"].items():
                path = self._case_asset(directory, name)
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Case asset checksum mismatch: {identity}/{name}")
            bundle = self._prepared_bundle(identity)
            records = bundle["records"]
            if len(records) != case["record_count"] or len({row["record_id"] for row in records}) != len(records):
                raise ValueError(f"Case record counts or identities differ: {identity}")
            if any(row.get("document_id") != identity for row in records):
                raise ValueError(f"Case contains another document's records: {identity}")
            with pymupdf.open(bundle["pdf"]) as pdf:
                if len(pdf) != case["page_count"] or len(pdf) != bundle["pages"]["page_count"]:
                    raise ValueError(f"Case PDF page count differs: {identity}")
        return {"cases": len(self.cases), "records": sum(case["record_count"] for case in self.cases.values())}
