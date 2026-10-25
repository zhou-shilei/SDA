// Keep matching rules in sync with scripts/patent_identity.py.
const PATENT_NUMBER_PATTERN = /(?:^|[^A-Z0-9])((?:IN[\s_/-]*\d{1,6}[\s_/-]*(?:DEL|MUM|CHE|KOL|CHN|MAS|BOM|CAL)(?:[\s_/-]*NP)?[\s_/-]*(?:19|20)\d{2}|(?:WO|US|EP|CN|JP|KR|IN)[\s_/-]*(?:\d{4}[\s_/-]+\d{4,8}|\d{5,12}))(?:[\s_/-]*[A-Z]\d{0,2})?)(?=$|[^A-Z0-9])/g;

export function extractPatentNumber(value) {
  const text = String(value || "").normalize("NFKC").toUpperCase().replace(/[‐‑‒–—−]/g, "-");
  const numbers = new Set([...text.matchAll(PATENT_NUMBER_PATTERN)].map(match => match[1].replace(/[\s_/-]/g, "")));
  return numbers.size === 1 ? [...numbers][0] : "";
}

export function patentNumber(patent) {
  return extractPatentNumber(patent?.patent_number)
    || extractPatentNumber(patent?.filename)
    || extractPatentNumber(patent?.document_id);
}

export function patentDisplayName(patent) {
  return patentNumber(patent) || String(patent?.document_id || "");
}

export function deriveDocumentId(filename) {
  const number = extractPatentNumber(filename);
  if (number) return number;
  const stem = String(filename || "").replace(/\.[^.]+$/, "");
  const safe = stem.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[._-]+|[._-]+$/g, "").slice(0, 80);
  return safe.length >= 3 ? safe : `PATENT-${Date.now()}`;
}
