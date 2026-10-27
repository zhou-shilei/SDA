const STAGES = ["layout", "ocr", "assembly", "export", "extraction", "template", "tst", "construct"];
const STORAGE_KEY = "sda.studio.jobTiming.v1";

function timestamp(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : null;
}

function median(values) {
  const ordered = values.filter((value) => Number.isFinite(value) && value > 0).sort((left, right) => left - right);
  return ordered.length ? ordered[Math.floor(ordered.length / 2)] : null;
}

function stagePlan(patent) {
  const operation = patent.timing?.operation || patent.operation;
  if (patent.extraction_engine === "dataagent2") {
    const semantic = ["classify", "template", "reader", "tst", "build"];
    return ["analyze", "compare"].includes(operation) ? semantic : ["layout", "ocr", "assembly", "export", ...semantic];
  }
  if (["analyze", "compare"].includes(operation)) return ["extraction", "template", "tst", "construct"];
  if (["semantic_refresh", "collection_review"].includes(operation)) return ["classify", "template", "sequence_extract"];
  if (operation === "reparse") return ["agent"];
  return STAGES;
}

export class JobEtaEstimator {
  constructor({ storage = null } = {}) {
    this.storage = storage;
    this.jobs = new Map();
    this.history = {};
    this.learned = new Set();
    try {
      const saved = JSON.parse(storage?.getItem(STORAGE_KEY) || "{}");
      this.history = saved.history || {};
      this.learned = new Set(saved.learned || []);
    } catch {}
  }

  persist() {
    try {
      this.storage?.setItem(STORAGE_KEY, JSON.stringify({ history: this.history, learned: [...this.learned].slice(-200) }));
    } catch {}
  }

  profile(patent, stage) {
    const profile = patent.timing?.profile || {};
    if (stage === "layout") return profile.layout || "layout-unknown";
    if (["classify", "template", "sequence_extract", "reader", "tst", "build"].includes(stage)) return `${patent.extraction_engine || "dataagent1"}:${profile.model || "unknown"}:${profile.effort || "unknown"}`;
    return "cpu";
  }

  historyKey(patent, stage) {
    return `${patent.timing?.operation || patent.operation || "pipeline"}:${stage}:${this.profile(patent, stage)}`;
  }

  learn(patent, stage, record, identity) {
    if (this.learned.has(identity) || !Number.isFinite(record.seconds) || record.seconds <= 0) return;
    const key = this.historyKey(patent, stage);
    this.history[key] = [...(this.history[key] || []), record].slice(-20);
    this.learned.add(identity);
    this.persist();
  }

  observe(patent, now) {
    const identity = patent.timing?.run_started_at || patent.created_at || "unknown";
    const operation = patent.timing?.operation || patent.operation;
    let job = this.jobs.get(patent.document_id);
    const restarted = job && ["completed", "failed"].includes(job.status) && ["queued", "processing"].includes(patent.status);
    const operationChanged = job && job.operation !== operation;
    if (!job || job.identity !== identity || job.operation !== operation || restarted) {
      job = { identity, operation, status: patent.status, stage: patent.stage,
        started: now, runStarted: restarted || operationChanged || patent.status === "queued"
          ? timestamp(patent.updated_at) ?? now : timestamp(patent.created_at) ?? now,
        stageStarted: now, samples: [],
        completeStage: patent.status === "processing" && Number(patent.stage_current || 0) === 0, tableCount: 0 };
      this.jobs.set(patent.document_id, job);
    }
    for (const record of patent.timing?.completed_stages || []) {
      this.learn(patent, record.stage, { seconds: record.seconds, units: record.units },
        `${patent.document_id}:${identity}:${record.stage}:${record.started_at}`);
    }
    if (job.stage !== patent.stage) {
      const last = job.samples.at(-1);
      if (patent.status !== "failed" && job.completeStage && last && now > job.stageStarted) {
        this.learn(patent, job.stage, { seconds: (now - job.stageStarted) / 1000, units: last.units },
          `${patent.document_id}:${identity}:${job.stage}:${job.stageStarted}`);
      }
      job.stage = patent.stage;
      job.stageStarted = now;
      job.samples = [];
      job.completeStage = true;
    }
    const timing = patent.timing?.current;
    if (timing?.stage === patent.stage && Array.isArray(timing.samples)) {
      job.stageStarted = timestamp(timing.started_at) ?? job.stageStarted;
      job.samples = timing.samples.map((sample) => ({ time: timestamp(sample.at), units: Number(sample.units) }))
        .filter((sample) => sample.time !== null && Number.isFinite(sample.units));
      job.completeStage = timing.start_units === 0;
    }
    const units = Number(patent.stage_current || 0);
    const updated = timestamp(patent.updated_at) ?? now;
    const last = job.samples.at(-1);
    if (last && units < last.units) {
      job.samples = [];
      job.stageStarted = now;
      job.completeStage = units === 0;
    }
    if (!job.samples.length || units > job.samples.at(-1).units) job.samples.push({ time: updated, units });
    job.samples = job.samples.slice(-24);
    if (["agent", "classify"].includes(patent.stage) && patent.operation !== "reparse") {
      job.tableCount = Math.max(job.tableCount, Number(patent.stage_total || 0));
    }
    for (const record of patent.timing?.completed_stages || []) {
      if (["agent", "classify"].includes(record.stage)) job.tableCount = Math.max(job.tableCount, Number(record.total_units || record.units || 0));
    }
    job.status = patent.status;
    return job;
  }

  budget(patent, job, stage) {
    const pages = Math.max(1, Number(patent.page_count || 1));
    const tables = Math.max(1, job.tableCount || Math.ceil(pages * 0.5));
    const total = stage === patent.stage && Number(patent.stage_total) > 0 ? Number(patent.stage_total)
      : ["orientation", "layout", "crop", "render"].includes(stage) ? pages : ["agent", "classify"].includes(stage) ? tables : 1;
    const history = this.history[this.historyKey(patent, stage)] || [];
    const rate = median(history.filter((sample) => sample.units > 0).map((sample) => sample.seconds / sample.units));
    const duration = median(history.map((sample) => sample.seconds));
    if (history.length && (rate || duration)) {
      const seconds = rate ? rate * total : duration;
      return { low: seconds * 0.7, high: seconds * 1.5, total, learned: true };
    }
    const defaults = {
      orientation: [pages * 0.5, pages * 3],
      layout: [15 + pages, 45 + pages * 8], crop: [2 + pages * 0.05, 10 + pages * 0.5],
      render: [2 + pages * 0.03, 10 + pages * 0.3], agent: [tables * 20, tables * 90],
      classify: [Math.ceil(tables / 4) * 60, Math.ceil(tables / 4) * 240], template: [120, 900],
      sequence_extract: [60, 900],
    };
    if ((patent.timing?.operation || patent.operation) === "reparse") defaults.agent = [60, 600];
    const [low, high] = defaults[stage] || [60, 600];
    return { low, high, total, learned: false };
  }

  estimate(patent, now = Date.now()) {
    const job = this.observe(patent, now);
    if (!["queued", "processing"].includes(patent.status)) return null;
    const queued = patent.status === "queued";
    const plan = stagePlan(patent);
    const index = queued ? 0 : Math.max(0, plan.indexOf(patent.stage));
    const remaining = plan.slice(index);
    let low = 0;
    let high = 0;
    let observed = false;
    let preliminary = false;
    let recalculating = false;
    for (const stage of remaining) {
      const budget = this.budget(patent, job, stage);
      let stageLow = budget.low;
      let stageHigh = budget.high;
      if (!queued && stage === patent.stage) {
        const rates = [];
        for (let sampleIndex = 1; sampleIndex < job.samples.length; sampleIndex++) {
          const previous = job.samples[sampleIndex - 1];
          const next = job.samples[sampleIndex];
          const delta = next.units - previous.units;
          const seconds = (next.time - previous.time) / 1000;
          if (delta > 0 && seconds > 0) rates.push(seconds / delta);
        }
        const rate = median(rates.slice(-8));
        const current = Number(patent.stage_current || 0);
        const total = Number(patent.stage_total || 0);
        const latest = job.samples.at(-1);
        const age = Math.max(0, (now - (latest?.time ?? now)) / 1000);
        const elapsed = Math.max(0, (now - job.stageStarted) / 1000);
        if (rate && total > current) {
          observed = true;
          stageLow = Math.max(rate * 0.5, (total - current) * rate * 0.7 - age);
          stageHigh = Math.max(rate * 1.5, (total - current) * rate * 1.5 - age);
          recalculating = age > Math.max(120, rate * 3);
        } else if (total > 0 && current > 0) {
          const fraction = Math.max(0.03, (total - current) / total);
          stageLow *= fraction;
          stageHigh = Math.max(30, stageHigh * fraction);
          recalculating = current >= total && age > 120;
        } else {
          stageLow = Math.max(15, stageLow - elapsed);
          stageHigh = Math.max(60, stageHigh - elapsed);
          recalculating = elapsed > budget.high;
        }
      }
      low += stageLow;
      high += stageHigh;
      preliminary ||= !budget.learned;
    }
    const started = timestamp(patent.timing?.run_started_at) ?? job.runStarted;
    return { lowSeconds: Math.max(15, low), highSeconds: Math.max(30, high),
      elapsedSeconds: Math.max(0, (now - started) / 1000),
      preliminary, observed, recalculating, queued, queueAhead: 0 };
  }

  estimateAll(patents, now = Date.now()) {
    const results = new Map(patents.map((patent) => [patent.document_id, this.estimate(patent, now)]));
    const active = patents.filter((patent) => patent.status === "processing");
    let waitingLow = active.reduce((sum, patent) => sum + (results.get(patent.document_id)?.lowSeconds || 0), 0);
    let waitingHigh = active.reduce((sum, patent) => sum + (results.get(patent.document_id)?.highSeconds || 0), 0);
    let ahead = active.length;
    const blocked = active.some((patent) => results.get(patent.document_id)?.recalculating);
    const queued = patents.filter((patent) => patent.status === "queued")
      .sort((left, right) => (timestamp(left.updated_at || left.created_at) || 0) - (timestamp(right.updated_at || right.created_at) || 0));
    for (const patent of queued) {
      const estimate = results.get(patent.document_id);
      estimate.lowSeconds += waitingLow;
      estimate.highSeconds += waitingHigh;
      estimate.queueAhead = ahead++;
      estimate.recalculating ||= blocked;
      waitingLow = estimate.lowSeconds;
      waitingHigh = estimate.highSeconds;
    }
    return results;
  }
}

export function formatEtaDuration(seconds, locale = "zh") {
  if (seconds < 60) return locale === "zh" ? "不到 1 分钟" : "<1 min";
  const minutes = Math.max(1, Math.ceil(Math.max(0, seconds) / 60));
  if (minutes < 60) return locale === "zh" ? `${minutes} 分钟` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return locale === "zh" ? `${hours} 小时${remainder ? ` ${remainder} 分钟` : ""}`
    : `${hours} h${remainder ? ` ${remainder} min` : ""}`;
}

export function formatEtaRange(low, high, locale = "zh") {
  const first = formatEtaDuration(low, locale);
  const last = formatEtaDuration(Math.max(low, high), locale);
  return first === last ? first : `${first}–${last}`;
}

export function formatEtaCompletion(estimate, now = Date.now(), locale = "zh") {
  const current = new Date(now);
  const timeZone = "Asia/Shanghai";
  const calendar = new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  });
  const clock = (seconds) => {
    const finish = new Date(now + seconds * 1000);
    const sameDay = calendar.format(finish) === calendar.format(current);
    return new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-GB", {
      timeZone,
      ...(sameDay ? {} : { month: "2-digit", day: "2-digit" }),
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(finish);
  };
  const first = clock(estimate.lowSeconds);
  const last = clock(estimate.highSeconds);
  return first === last ? first : `${first}–${last}`;
}
