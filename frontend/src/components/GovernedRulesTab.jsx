import { useEffect, useState, useCallback } from "react";
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  ColumnLayout,
  Container,
  ExpandableSection,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
  Textarea,
} from "@cloudscape-design/components";
import { rulesAPI } from "../services/api";
import {
  RULES_UNSUPPORTED_MESSAGE,
  rulesUnsupportedFor,
} from "../utils/layerCapabilities";

// Human-readable epistemic status labels. The closed set lives in
// agents/shared/rule_vocab.py (EPISTEMIC_STATUSES); this map exists so a
// reviewer never has to read a raw camelCase IRI fragment. A status missing
// from the map falls back to its raw value rather than being hidden — a label
// we forgot to add must still be visible, because the status is exactly what
// distinguishes a correlation from a claimed cause.
const EPISTEMIC_LABEL = {
  associatedWith: "associated with",
  requiresFurtherEvidence: "requires further evidence",
  candidateContributorTo: "candidate contributor to",
  supportsHypothesis: "supports hypothesis",
  indicatesRiskOf: "indicates risk of",
  confirmedCauseOf: "confirmed cause of (attested)",
};

// Mirrors rule_vocab.DEFAULT_EPISTEMIC_STATUS / STRONGEST_EPISTEMIC_STATUS.
// The weakest default is deliberate: a newly authored rule claims the least it
// can, and strengthening the claim is an explicit act with a name attached.
const DEFAULT_EPISTEMIC_STATUS = "candidateContributorTo";
const STRONGEST_EPISTEMIC_STATUS = "confirmedCauseOf";

const LIFECYCLE_COLOR = {
  DRAFT: "grey",
  APPROVED: "blue",
  PUBLISHED: "green",
};

// ── Authoring vocabulary ────────────────────────────────────────────────────
// Every closed set below is declared in agents/shared/rule_models.py or
// rule_vocab.py and enforced by pydantic. Offering them as selects is not
// convenience: a typo in `rule_type` is a 422 the author cannot diagnose, and
// the label is where the distinction between the four types gets explained.

const RULE_TYPE_OPTIONS = [
  { value: "diagnostic", label: "diagnostic — explains an observed state" },
  { value: "risk", label: "risk — flags a likely future state" },
  { value: "compliance", label: "compliance — asserts a required state" },
  { value: "advisory", label: "advisory — recommends, never decides" },
];

const LEVEL_OPTIONS = [
  {
    value: "general",
    label: "general — holds wherever the subject class does",
  },
  {
    value: "contextualised",
    label: "contextualised — holds within one process stage",
  },
  { value: "decision", label: "decision — drives an action, not just a claim" },
];

// rule_vocab.ALLOWED_OPERATORS. A Condition with anything else is rejected by
// the model validator, so this is the whole set.
const OPERATOR_OPTIONS = ["=", "!=", ">", ">=", "<", "<="].map((o) => ({
  value: o,
  label: o,
}));

// A Condition carries exactly one comparand — never both, never neither. A
// select makes the "both" case unrepresentable rather than merely invalid.
const COMPARAND_OPTIONS = [
  { value: "threshold", label: "an approved threshold" },
  { value: "literal", label: "an inline literal" },
];

const EPISTEMIC_OPTIONS = Object.entries(EPISTEMIC_LABEL).map(
  ([value, label]) => ({ value, label }),
);

/**
 * Look a raw value up in an option list for a Cloudscape Select.
 *
 * @param {Array<{value:string,label:string}>} options The option list.
 * @param {string} value The stored value.
 * @returns {{value:string,label:string}} The matching option, or a synthesised
 *          one so a value we have no label for is still displayed rather than
 *          silently reset to the first option.
 */
function optionFor(options, value) {
  return options.find((o) => o.value === value) ?? { value, label: value };
}

// Safe as an IRI local name AND as a JSON key. `rule_graph` mints a rule's IRI
// as `VKGR + rule_id` by plain concatenation and rdflib does not validate the
// result, so an id with a space produces triples that parse and never match.
const ID_PATTERN = /^[A-Za-z0-9._:-]+$/;

// Absolute IRI: a scheme, a colon, and no whitespace. `binding`, `subject_class`
// and `outcome` are wrapped in a bare `URIRef(...)`, which happily accepts a
// relative reference and resolves it against nothing.
const ABSOLUTE_IRI = /^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$/;

/**
 * Split a comma-separated field into a trimmed list, dropping empties.
 *
 * @param {string} text Raw field text.
 * @returns {string[]} The parsed list.
 */
function csvToList(text) {
  return (text ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Coerce an inline literal comparand to the type it reads as.
 *
 * Ontop compiles `?v > "90"` as a lexicographic comparison and `?v > 90` as a
 * numeric one, so storing a bound as a string makes the rule silently misfire
 * ("9" sorts after "90"). Booleans get the same treatment for the same reason.
 *
 * @param {string} raw The typed literal.
 * @returns {string|number|boolean} The coerced value.
 */
function coerceLiteral(raw) {
  const t = (raw ?? "").trim();
  if (t === "true") return true;
  if (t === "false") return false;
  // Number("") is 0, so the empty case has to be excluded before the test.
  if (t !== "" && !Number.isNaN(Number(t))) return Number(t);
  return t;
}

/** @returns {object} A blank condition row. */
function emptyCondition() {
  return {
    binding: "",
    operator: ">",
    mode: "threshold",
    threshold_id: "",
    literal: "",
  };
}

/** @returns {object} A blank threshold row. */
function emptyThreshold() {
  return { threshold_id: "", value: "", unit: "", source_span: "" };
}

/**
 * A threshold row the author never touched.
 *
 * The form starts with one row because most rules need one, but a rule whose
 * comparands are all literals legitimately has none — so an untouched row is
 * absent, not invalid.
 *
 * @param {object} t The threshold row.
 * @returns {boolean} True when every field is blank.
 */
function isBlankThreshold(t) {
  return (
    !t.threshold_id.trim() &&
    !String(t.value).trim() &&
    !t.unit.trim() &&
    !t.source_span.trim()
  );
}

/** @returns {object} The form state for a brand-new rule. */
function emptyForm() {
  return {
    rule_id: "",
    name: "",
    description: "",
    synonyms: "",
    rule_type: "diagnostic",
    level: "general",
    subject_class: "",
    process_stage: "",
    conditions: [emptyCondition()],
    thresholds: [emptyThreshold()],
    outcome: "",
    epistemic_status: DEFAULT_EPISTEMIC_STATUS,
    attested: false,
    attestation_note: "",
    missing_inputs: "",
  };
}

/**
 * Project a stored rule onto the flat form state.
 *
 * Nested `scope` / `conclusion` are flattened and lists become comma-separated
 * text, because that is what an editable field can hold. `buildPayload` is the
 * exact inverse.
 *
 * @param {object} rule A Rule as returned by the API.
 * @returns {object} Form state.
 */
function formFromRule(rule) {
  const conditions = (rule.conditions ?? []).map((c) => ({
    binding: c.binding ?? "",
    operator: c.operator ?? ">",
    // Condition guarantees exactly one comparand, so which one is set IS the
    // mode — there is no third state to represent.
    mode: c.threshold_id ? "threshold" : "literal",
    threshold_id: c.threshold_id ?? "",
    literal: c.value === null || c.value === undefined ? "" : String(c.value),
  }));
  return {
    rule_id: rule.rule_id ?? "",
    name: rule.name ?? "",
    description: rule.description ?? "",
    synonyms: (rule.synonyms ?? []).join(", "),
    rule_type: rule.rule_type ?? "diagnostic",
    level: rule.level ?? "general",
    subject_class: rule.scope?.subject_class ?? "",
    process_stage: rule.scope?.process_stage ?? "",
    // A stored rule always has at least one condition, but a row must exist for
    // the author to type into if the record somehow arrived without one.
    conditions: conditions.length ? conditions : [emptyCondition()],
    thresholds: (rule.thresholds ?? []).map((t) => ({
      threshold_id: t.threshold_id ?? "",
      value: t.value === null || t.value === undefined ? "" : String(t.value),
      unit: t.unit ?? "",
      source_span: t.source_span ?? "",
    })),
    outcome: rule.conclusion?.outcome ?? "",
    epistemic_status:
      rule.conclusion?.epistemic_status ?? DEFAULT_EPISTEMIC_STATUS,
    attested: !!rule.conclusion?.attested_by,
    attestation_note: rule.conclusion?.attestation_note ?? "",
    missing_inputs: (rule.missing_inputs ?? []).join(", "),
  };
}

/**
 * Everything wrong with the form, in the order an author would fix it.
 *
 * These are not duplicated server-side checks for their own sake. The backend
 * fails loudly on a malformed Rule, but `rule_graph` accepts any string as an
 * IRI — so a relative binding or a spaced id is the one class of bad input that
 * persists silently and can only be stopped here.
 *
 * @param {object} form Current form state.
 * @param {string|null|undefined} currentUser Signed-in reviewer, needed to
 *        record an attestation.
 * @returns {string[]} Human-readable problems; empty when the form is savable.
 */
function validateForm(form, { currentUser }) {
  const errors = [];

  const id = form.rule_id.trim();
  if (!id) {
    errors.push("Rule id is required.");
  } else if (!ID_PATTERN.test(id)) {
    errors.push(
      `Rule id "${id}" may only contain letters, digits and . _ : - — it is ` +
        "concatenated into the rule's IRI, and a broken IRI is stored without " +
        "complaint and then matches nothing.",
    );
  }
  if (!form.name.trim()) {
    errors.push(
      "Name is required — it is what the query agent matches a question against.",
    );
  }
  if (!form.description.trim()) {
    errors.push(
      "Description is required — it is matched alongside the name, and it is " +
        "what the next reviewer reads.",
    );
  }
  if (!ABSOLUTE_IRI.test(form.subject_class.trim())) {
    errors.push(
      "Subject class must be an absolute IRI, e.g. " +
        "https://example.org/vkg#Holding.",
    );
  }
  if (!ABSOLUTE_IRI.test(form.outcome.trim())) {
    errors.push(
      "Outcome must be an absolute IRI, e.g. https://example.org/vkg#LapseRisk.",
    );
  }

  // Thresholds first: the condition checks below need to know which ids exist.
  const defined = new Set();
  form.thresholds
    .filter((t) => !isBlankThreshold(t))
    .forEach((t, i) => {
      const label = `Threshold ${i + 1}`;
      const tid = t.threshold_id.trim();
      if (!tid) {
        errors.push(`${label}: an id is required.`);
      } else if (!ID_PATTERN.test(tid)) {
        errors.push(
          `${label}: id "${tid}" may only contain letters, digits and . _ : -`,
        );
      } else if (defined.has(tid)) {
        errors.push(
          `Threshold id "${tid}" is used more than once — a condition ` +
            "referencing it would bind to whichever row came first.",
        );
      }
      if (tid) defined.add(tid);
      if (String(t.value).trim() === "" || Number.isNaN(Number(t.value))) {
        errors.push(`${label}: value must be a number.`);
      }
      if (!t.unit.trim()) {
        errors.push(
          `${label}: a unit is required — "90" alone is not a bound.`,
        );
      }
      if (!t.source_span.trim()) {
        errors.push(
          `${label}: the source span is required. A bound no document states ` +
            "belongs in Missing inputs, not invented here.",
        );
      }
    });

  if (!form.conditions.length) {
    errors.push("A rule needs at least one condition.");
  }
  form.conditions.forEach((c, i) => {
    const label = `Condition ${i + 1}`;
    const binding = c.binding.trim();
    if (!binding) {
      errors.push(`${label}: a binding is required.`);
    } else if (!ABSOLUTE_IRI.test(binding)) {
      errors.push(
        `${label}: binding must be an absolute IRI — "${binding}" would be ` +
          "minted as a relative IRI that no query can match.",
      );
    }
    if (c.mode === "threshold") {
      const ref = c.threshold_id.trim();
      if (!ref) {
        errors.push(
          `${label}: name the threshold it compares against, or switch it to ` +
            "an inline literal.",
        );
      } else if (!defined.has(ref)) {
        errors.push(`${label}: no threshold "${ref}" is defined below.`);
      }
    } else if (!c.literal.trim()) {
      errors.push(`${label}: an inline literal is required.`);
    }
  });

  if (form.epistemic_status === STRONGEST_EPISTEMIC_STATUS) {
    if (!form.attested) {
      errors.push(
        '"confirmed cause of" is the one status a machine may not grant ' +
          "itself. Tick the attestation to record that you personally stand " +
          "behind the causal claim, or choose a weaker status.",
      );
    } else if (!form.attestation_note.trim()) {
      errors.push("An attestation needs a note saying what you relied on.");
    }
    if (form.attested && !currentUser) {
      errors.push(
        "Cannot attest: no signed-in user to record as the attester. Sign in " +
          "again and reopen this form.",
      );
    }
  }

  return errors;
}

/**
 * Build the Rule body the API validates, from the flat form state.
 *
 * Four fields are forced rather than offered, because the server owns them and
 * a control the server ignores is a lie: `lifecycle`, `compiled_sparql`,
 * `reviewed_by` and `test_cases`. `namespace` is taken from the route so it can
 * never disagree with the path (the backend 400s on a mismatch).
 *
 * @param {object} form Current form state.
 * @param {string} ontologyId The layer id, used as the namespace.
 * @param {object|null} editing The stored rule being edited, or null when new.
 * @param {string|null|undefined} currentUser Signed-in reviewer.
 * @returns {object} A Rule payload.
 */
function buildPayload({ form, ontologyId, editing, currentUser }) {
  const thresholds = form.thresholds
    .filter((t) => !isBlankThreshold(t))
    .map((t) => {
      const tid = t.threshold_id.trim();
      const value = Number(t.value);
      const unit = t.unit.trim();
      const span = t.source_span.trim();
      const prior = (editing?.thresholds ?? []).find(
        (p) => p.threshold_id === tid,
      );
      // An approval is a named human's signature on ONE number backed by ONE
      // sentence. It survives an edit only when all three are byte-identical;
      // anything else silently re-uses a signature for a bound nobody signed.
      const unchanged =
        prior &&
        Number(prior.value) === value &&
        prior.unit === unit &&
        prior.source_span === span;
      return {
        threshold_id: tid,
        value,
        unit,
        source_span: span,
        approved_by: unchanged ? (prior.approved_by ?? null) : null,
      };
    });

  const attested =
    form.epistemic_status === STRONGEST_EPISTEMIC_STATUS
      ? form.attested
      : false;

  return {
    rule_id: form.rule_id.trim(),
    namespace: ontologyId,
    name: form.name.trim(),
    description: form.description.trim(),
    rule_type: form.rule_type,
    level: form.level,
    scope: {
      subject_class: form.subject_class.trim(),
      process_stage: form.process_stage.trim() || null,
    },
    conditions: form.conditions.map((c) => ({
      binding: c.binding.trim(),
      operator: c.operator,
      threshold_id: c.mode === "threshold" ? c.threshold_id.trim() : null,
      value: c.mode === "literal" ? coerceLiteral(c.literal) : null,
    })),
    conclusion: {
      outcome: form.outcome.trim(),
      epistemic_status: form.epistemic_status,
      // Never free text: the attester is whoever is signed in, which is the
      // only claim about identity this form is entitled to make.
      attested_by: attested ? currentUser : null,
      attestation_note: attested ? form.attestation_note.trim() : null,
    },
    thresholds,
    // A hand-authored rule has no extraction to cite, and `Provenance` requires
    // a model_id and a confidence — filling those in would fabricate one. An
    // EDITED mined rule keeps its provenance verbatim: this form builds the
    // whole PUT body, so it is the only place that could erase it.
    provenance: editing?.provenance ?? null,
    synonyms: csvToList(form.synonyms),
    missing_inputs: csvToList(form.missing_inputs),
    // Regenerated deterministically at publish; sending stale cases would put
    // assertions about the previous logic on the new rule.
    test_cases: [],
    specialises: editing?.specialises ?? null,
    // Server-owned. Sent explicitly so this payload is a complete Rule rather
    // than one that relies on pydantic defaults to be safe.
    lifecycle: "DRAFT",
    reviewed_by: null,
    compiled_sparql: null,
    version: editing?.version ?? 1,
  };
}

// A complete, minimal mined-rules artefact, shown verbatim in the import
// dialog. Kept as a template literal rather than JSON.stringify of an object so
// key ORDER and the camelCase spelling are exactly what an author will copy —
// the artefact's keys are camelCase while the API's Rule fields are snake_case,
// and that is the single most confusable thing about this format.
//
// Mirrors lambda/rest-api/services/rule_import.py:mined_rules_to_drafts. If a
// field is added there, it belongs here too, or an author writing an artefact by
// hand will silently omit it.
const ARTEFACT_EXAMPLE = `{
  "documentId": "doc-7",
  "rules": [
    {
      "name": "Lapse risk after 90 days",
      "description": "A holding unpaid for more than 90 days is at risk of lapsing.",
      "ruleType": "risk",
      "scope": {
        "subjectClass": "https://example.org/vkg#Holding",
        "processStage": "renewal"
      },
      "conditions": [
        {
          "binding": "https://example.org/vkg#daysSinceLastPayment",
          "operator": ">",
          "thresholdId": "THR-090"
        }
      ],
      "thresholds": [
        {
          "thresholdId": "THR-090",
          "value": 90,
          "unit": "days",
          "sourceSpan": "lapses after 90 days of non-payment"
        }
      ],
      "conclusion": {
        "outcome": "https://example.org/vkg#LapseRisk",
        "epistemicStatus": "indicatesRiskOf"
      },
      "missingInputs": [],
      "sourceSentence": "A holding lapses after 90 days of non-payment.",
      "modelId": "claude-sonnet-4",
      "confidence": 0.0,
      "page": 4
    }
  ]
}`;

// Trim an IRI to its local name for display. The full IRI stays in the title
// attribute so a reviewer can still verify which property is meant.
function localName(iri) {
  if (!iri) return "—";
  const cut = Math.max(iri.lastIndexOf("#"), iri.lastIndexOf("/"));
  return cut >= 0 ? iri.slice(cut + 1) : iri;
}

/**
 * Publish is only offered when every validation level passed AND every
 * threshold has a named approver AND the rule is APPROVED.
 *
 * This is a CONVENIENCE, not a control: the server runs the same gate at
 * publish time, so a disabled button never has to be trusted. It exists so a
 * reviewer sees WHY publishing is unavailable before clicking.
 */
function canPublish(rule, reports) {
  if (!rule) return false;
  const allApproved = (rule.thresholds ?? []).every((t) => !!t.approved_by);
  const allPassed =
    (reports ?? []).length > 0 && reports.every((r) => r.passed);
  return allApproved && allPassed && rule.lifecycle === "APPROVED";
}

// Reasons publish is unavailable, in the order a reviewer would fix them.
// Returned as text so the gate is explained rather than merely enforced.
function publishBlockers(rule, reports) {
  if (!rule) return [];
  const out = [];
  const unapproved = (rule.thresholds ?? []).filter((t) => !t.approved_by);
  if (unapproved.length) {
    out.push(
      `${unapproved.length} threshold(s) still need a human approver: ${unapproved
        .map((t) => t.threshold_id)
        .join(", ")}`,
    );
  }
  if (rule.lifecycle === "DRAFT") out.push("The rule itself is not approved.");
  if (!(reports ?? []).length) {
    out.push("Validation has not been run in this session.");
  } else if (!reports.every((r) => r.passed)) {
    out.push("At least one validation level is failing.");
  }
  return out;
}

/**
 * Admin tab — reviews and publishes the governed rules for one VKG layer.
 *
 * A rule is business logic a human authored or a document miner proposed:
 * "a holding unpaid after 90 days is a lapse risk". Rules live as RDF in the
 * Neptune named graph `<ontologyId>/rules`, but this tab deliberately renders
 * them as a form — a reviewer approves a business bound, not a triple.
 *
 * Two things here are governance, not UI polish:
 *   1. Each threshold carries the verbatim `source_span` it came from and needs
 *      an explicit per-threshold approval. The compiler refuses to compile an
 *      unapproved bound, so this is the one action a machine cannot take.
 *   2. `missing_inputs` is shown as a knowledge gap, not a validation failure.
 *      A rule with missing inputs still has value — it advises rather than
 *      evaluates — so presenting it as an error would invite deleting it.
 *
 * @param {string} ontologyId  semantic-layer id, used as the API namespace.
 * @param {string} layerType   'VKG' | 'Hybrid' | 'SemanticRAG'; rules need an
 *                             ontology graph, so Semantic RAG is excluded. May
 *                             be null before the layer's config has loaded.
 * @param {string} currentUser reviewer identity recorded on every approval.
 */
export default function GovernedRulesTab({
  ontologyId,
  layerType,
  currentUser,
}) {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // True when the backend has no /rules routes at all — the feature flag is
  // off. Distinguished from `error` so a configuration state does not read as
  // a failure the reviewer is expected to fix.
  const [notEnabled, setNotEnabled] = useState(false);
  // Rules that are stored but cannot be deserialized. Held separately from
  // `error` because the request succeeded — the graph is what is damaged, and
  // every other rule on screen is still trustworthy.
  const [unreadable, setUnreadable] = useState([]);

  // Validation is per-session and per-rule: it is re-run on demand and never
  // cached server-side, because the ontology it validates against can change
  // under a rule at any time.
  const [reports, setReports] = useState([]);
  const [missingInputs, setMissingInputs] = useState([]);
  const [validating, setValidating] = useState(false);

  const [busy, setBusy] = useState(null); // 'approve' | 'publish' | threshold_id
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);

  // Import of a doc-pipeline artefact. `importNote` is deliberately separate
  // from `error`: "that document mined no rules" is a fact about the document,
  // not a failure, and rendering it as an error would invite a pointless retry.
  const [importOpen, setImportOpen] = useState(false);
  const [importDocId, setImportDocId] = useState("");
  const [importing, setImporting] = useState(false);
  const [importNote, setImportNote] = useState(null); // {type, text}

  // Manual authoring. `editing` holds the STORED rule, not a copy of the form:
  // it is the only source for the fields the form deliberately does not expose
  // (provenance, specialises, version) and for deciding whether a threshold's
  // existing approval still applies to the edited bound.
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [formErrors, setFormErrors] = useState([]);
  const [saving, setSaving] = useState(false);

  // Surface the actionable text from a failed envelope. The 422 `detail` from a
  // blocked publish IS the reason — it names the level or threshold at fault —
  // so it wins over the generic axios message.
  const failureText = (res, fallback) =>
    res.details?.detail || res.error || fallback;

  const refresh = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    setError(null);
    setNotEnabled(false);
    // Cleared per refresh, or a warning about a rule that has since been fixed
    // would stay on screen for the rest of the session.
    setUnreadable([]);
    // rulesAPI.* resolve to the handleResponse envelope {success, data} and
    // never throw. The router returns {rules, unreadable}; the bare-array branch
    // keeps this working against an older deployment.
    const res = await rulesAPI.list(ontologyId);
    if (res.success) {
      const list = Array.isArray(res.data) ? res.data : (res.data?.rules ?? []);
      setItems(list);
      // Surfaced, not swallowed: a rule missing from the table with no warning
      // is indistinguishable from a rule that was deleted.
      setUnreadable(
        Array.isArray(res.data?.unreadable) ? res.data.unreadable : [],
      );
      // Land the reviewer in a review pane rather than an empty right-hand
      // side; re-select the same rule across a refresh so an approval does not
      // bounce them back to the top of the queue.
      setSelected((prev) => {
        const same = prev && list.find((r) => r.rule_id === prev.rule_id);
        return same ?? list[0] ?? null;
      });
    } else if (res.details?.detail === "Not Found") {
      setNotEnabled(true);
      setItems([]);
      setSelected(null);
    } else {
      setError(failureText(res, "Failed to load rules"));
    }
    setLoading(false);
  }, [ontologyId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Replace one rule in place after a mutation returns the updated record.
  const applyUpdated = (rule) => {
    setItems((prev) =>
      prev.map((r) => (r.rule_id === rule.rule_id ? rule : r)),
    );
    setSelected(rule);
  };

  const handleImport = async () => {
    const docId = importDocId.trim();
    if (!docId) return;
    setImporting(true);
    setImportNote(null);
    // The pipeline's ruleminer stage writes `mined-rules/{documentId}.json` and
    // reports it as `minedRulesKey`; the key is built here from the id rather
    // than typed, because a typo in a path is far less recoverable than one in
    // an id the admin already knows.
    const res = await rulesAPI.importMined(
      ontologyId,
      `mined-rules/${docId}.json`,
    );
    setImporting(false);
    if (res.success) {
      const count = res.data?.count ?? (res.data?.rules ?? []).length;
      setImportOpen(false);
      setImportDocId("");
      setImportNote({
        type: "success",
        // Naming DRAFT here is the point: nothing that just arrived is
        // approved, attested or compiled, whatever the artefact claimed.
        text: `Imported ${count} rule(s) from ${docId} as DRAFT — no threshold is approved yet.`,
      });
      // Re-read from the server rather than trusting the response body, so the
      // table reflects what actually persisted.
      await refresh();
    } else if (res.details?.detail === "mined-rules artefact not found") {
      setImportNote({
        type: "info",
        text: `No mined rules for document "${docId}". Either the pipeline extracted none, or the document id is wrong.`,
      });
    } else {
      // A 422 names the malformed field — that text is the only way to identify
      // which artefact regressed, so it is shown as-is.
      setImportNote({
        type: "error",
        text: failureText(res, "Failed to import mined rules"),
      });
    }
  };

  // ── Authoring ──
  const openNewRule = () => {
    setEditing(null);
    setForm(emptyForm());
    setFormErrors([]);
    setFormOpen(true);
  };

  const openEditRule = (r) => {
    setEditing(r);
    setForm(formFromRule(r));
    setFormErrors([]);
    setFormOpen(true);
  };

  // Patch one field of the flat form state.
  const setField = (key, value) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  // Patch one field of one row in `conditions` or `thresholds`.
  const setRowField = (listKey, index, key, value) =>
    setForm((prev) => ({
      ...prev,
      [listKey]: prev[listKey].map((row, i) =>
        i === index ? { ...row, [key]: value } : row,
      ),
    }));

  const addRow = (listKey, factory) =>
    setForm((prev) => ({ ...prev, [listKey]: [...prev[listKey], factory()] }));

  const removeRow = (listKey, index) =>
    setForm((prev) => ({
      ...prev,
      [listKey]: prev[listKey].filter((_, i) => i !== index),
    }));

  const handleSave = async () => {
    const problems = validateForm(form, { currentUser });
    if (problems.length) {
      // Reported all at once rather than one per attempt: the author needs to
      // see the whole shape of what is wrong before rewriting the rule.
      setFormErrors(problems);
      return;
    }
    setFormErrors([]);
    setSaving(true);
    const payload = buildPayload({ form, ontologyId, editing, currentUser });
    const res = editing
      ? await rulesAPI.update(ontologyId, editing.rule_id, payload)
      : await rulesAPI.create(ontologyId, payload);
    setSaving(false);
    if (res.success) {
      setFormOpen(false);
      setEditing(null);
      // Re-read rather than trusting the response body: the server re-derives
      // lifecycle, drops compiled SPARQL and may reject fields silently, so the
      // stored record — not the payload just sent — is what the reviewer must
      // now be looking at.
      await refresh();
    } else {
      // Kept INSIDE the modal, and the modal stays open: a 422 names the field
      // at fault, and closing would discard everything just typed.
      setFormErrors([
        failureText(
          res,
          editing ? "Failed to save the rule" : "Failed to create the rule",
        ),
      ]);
    }
  };

  const handleValidate = async () => {
    if (!selected) return;
    setValidating(true);
    setError(null);
    const res = await rulesAPI.validate(ontologyId, selected.rule_id);
    setValidating(false);
    if (res.success) {
      setReports(res.data?.reports ?? []);
      setMissingInputs(res.data?.missing_inputs ?? []);
    } else {
      setError(failureText(res, "Failed to validate rule"));
    }
  };

  const handleApproveThreshold = async (threshold) => {
    if (!selected) return;
    setBusy(threshold.threshold_id);
    setError(null);
    const res = await rulesAPI.approveThreshold(
      ontologyId,
      selected.rule_id,
      threshold.threshold_id,
      currentUser,
    );
    setBusy(null);
    if (res.success) {
      applyUpdated(res.data);
    } else {
      setError(failureText(res, "Failed to approve threshold"));
    }
  };

  const handleApproveRule = async () => {
    if (!selected) return;
    setBusy("approve");
    setError(null);
    const res = await rulesAPI.approve(
      ontologyId,
      selected.rule_id,
      currentUser,
    );
    setBusy(null);
    if (res.success) {
      applyUpdated(res.data);
    } else {
      setError(failureText(res, "Failed to approve rule"));
    }
  };

  const handlePublish = async () => {
    if (!selected) return;
    setBusy("publish");
    setError(null);
    const res = await rulesAPI.publish(ontologyId, selected.rule_id);
    setBusy(null);
    if (res.success) {
      applyUpdated(res.data);
    } else {
      // The server's gate refused. Its reason names the failing level, the
      // unapproved threshold, or the Ontop translation error — show it as-is.
      setError(failureText(res, "Publish was blocked"));
    }
  };

  const handleDelete = async () => {
    if (!confirmDelete) return;
    setDeleting(true);
    const res = await rulesAPI.remove(ontologyId, confirmDelete.rule_id);
    setDeleting(false);
    if (res.success) {
      const remaining = items.filter(
        (r) => r.rule_id !== confirmDelete.rule_id,
      );
      setItems(remaining);
      setSelected(remaining[0] ?? null);
      setReports([]);
      setMissingInputs([]);
      setConfirmDelete(null);
    } else {
      setError(failureText(res, "Failed to delete rule"));
    }
  };

  // Render a condition's comparand: either the referenced threshold's approved
  // bound or an inline literal. Condition guarantees exactly one of the two.
  const comparandCell = (rule, c) => {
    if (c.threshold_id) {
      const t = (rule.thresholds ?? []).find(
        (x) => x.threshold_id === c.threshold_id,
      );
      if (!t) {
        // A dangling reference is a structural validation failure; say so here
        // rather than rendering a blank cell that looks like missing data.
        return (
          <StatusIndicator type="error">
            {`${c.threshold_id} (not defined)`}
          </StatusIndicator>
        );
      }
      return `${t.value} ${t.unit}`;
    }
    return typeof c.value === "string" ? `"${c.value}"` : String(c.value);
  };

  const rule = selected;
  const blockers = publishBlockers(rule, reports);

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            counter={items.length ? `(${items.length})` : undefined}
            description="Business logic a human approved. A PUBLISHED rule is compiled to SPARQL, smoke-executed through Ontop, and consulted by the query agent before it writes SQL of its own. Every numeric bound carries the sentence it came from and needs an explicit approval."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="primary"
                  iconName="add-plus"
                  onClick={openNewRule}
                  // Disabled where a rule could not resolve: no /rules API at
                  // all, or a layer with no ontology for a binding to bind to.
                  disabled={notEnabled || rulesUnsupportedFor(layerType)}
                >
                  Add rule
                </Button>
                <Button
                  onClick={() => {
                    setImportNote(null);
                    setImportOpen(true);
                  }}
                  disabled={notEnabled}
                >
                  Import mined rules
                </Button>
                <Button iconName="refresh" onClick={refresh} disabled={loading}>
                  Refresh
                </Button>
              </SpaceBetween>
            }
          >
            Governed Rules
          </Header>
        }
      >
        <SpaceBetween size="m">
          {notEnabled && (
            <Alert type="info" header="Governed rules are not enabled">
              Governed rules are not enabled on this deployment — it has no
              /rules API. Redeploy with{" "}
              <Box variant="code" display="inline">
                enableGovernedRules
              </Box>{" "}
              set to true in cdk.json to author rules for this layer.
            </Alert>
          )}

          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}

          {unreadable.length > 0 && (
            <Alert
              type="warning"
              header={`${unreadable.length} rule${
                unreadable.length === 1 ? "" : "s"
              } could not be read`}
            >
              <SpaceBetween size="xs">
                <Box variant="p">
                  These rules are stored in this layer but are missing required
                  fields, so they are not listed below and are not executing.
                  Every other rule on this page is unaffected.
                </Box>
                {unreadable.map((entry) => (
                  <Box key={entry.rule_id} variant="small">
                    <Box variant="code" display="inline">
                      {entry.rule_id}
                    </Box>{" "}
                    — {entry.error}
                  </Box>
                ))}
              </SpaceBetween>
            </Alert>
          )}

          {importNote && (
            <Alert
              type={importNote.type}
              dismissible
              onDismiss={() => setImportNote(null)}
            >
              {importNote.text}
            </Alert>
          )}

          {rulesUnsupportedFor(layerType) && !notEnabled && (
            <Alert type="info">{RULES_UNSUPPORTED_MESSAGE}</Alert>
          )}

          <Table
            loading={loading}
            loadingText="Loading rules"
            items={items}
            selectionType="single"
            selectedItems={rule ? [rule] : []}
            onSelectionChange={({ detail }) => {
              setSelected(detail.selectedItems[0] ?? null);
              // Reports belong to the rule they were produced for.
              setReports([]);
              setMissingInputs([]);
            }}
            trackBy="rule_id"
            columnDefinitions={[
              {
                id: "name",
                header: "Name",
                cell: (r) => <Box fontWeight="bold">{r.name}</Box>,
                width: 200,
              },
              {
                id: "description",
                header: "Description",
                cell: (r) => r.description || "—",
              },
              {
                id: "rule_type",
                header: "Type",
                cell: (r) => r.rule_type,
                width: 120,
              },
              {
                id: "lifecycle",
                header: "Lifecycle",
                cell: (r) => (
                  <Badge color={LIFECYCLE_COLOR[r.lifecycle] ?? "grey"}>
                    {r.lifecycle}
                  </Badge>
                ),
                width: 130,
              },
              {
                id: "evaluable",
                header: "Evaluable",
                // Mirrors Rule.is_evaluable(): a rule with missing inputs or an
                // unapproved bound can only advise, and the query tier treats
                // those two cases very differently.
                cell: (r) =>
                  (r.missing_inputs ?? []).length ? (
                    <StatusIndicator type="info">advises only</StatusIndicator>
                  ) : (
                    <StatusIndicator
                      type={r.compiled_sparql ? "success" : "pending"}
                    >
                      {r.compiled_sparql ? "executes" : "not compiled"}
                    </StatusIndicator>
                  ),
                width: 150,
              },
              {
                id: "actions",
                header: "",
                // Editing resets the rule to DRAFT and drops its compiled
                // SPARQL server-side, so this is a governed action even when it
                // only fixes a typo — hence a per-row link rather than an
                // always-visible inline editor.
                cell: (r) => (
                  <Button variant="inline-link" onClick={() => openEditRule(r)}>
                    Edit
                  </Button>
                ),
                width: 90,
              },
            ]}
            empty={
              loading ? (
                <Spinner />
              ) : (
                <Box
                  textAlign="center"
                  color="text-status-inactive"
                  padding="m"
                >
                  <SpaceBetween size="xs">
                    <span>No governed rules yet for this layer.</span>
                    <span>
                      Author one with <b>Add rule</b>, or mine them from a
                      document and import the drafts for review.
                    </span>
                  </SpaceBetween>
                </Box>
              )
            }
          />
        </SpaceBetween>
      </Container>

      {rule && (
        <Container
          header={
            <Header
              variant="h2"
              description={rule.description}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={handleValidate} loading={validating}>
                    Validate
                  </Button>
                  {rule.lifecycle === "DRAFT" && (
                    <Button
                      onClick={handleApproveRule}
                      loading={busy === "approve"}
                    >
                      Approve rule
                    </Button>
                  )}
                  <Button
                    variant="primary"
                    onClick={handlePublish}
                    loading={busy === "publish"}
                    disabled={!canPublish(rule, reports)}
                  >
                    Publish
                  </Button>
                  <Button onClick={() => setConfirmDelete(rule)}>Delete</Button>
                </SpaceBetween>
              }
            >
              {rule.name}
            </Header>
          }
        >
          <SpaceBetween size="l">
            <ColumnLayout columns={3} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Subject class</Box>
                <Box title={rule.scope?.subject_class}>
                  {localName(rule.scope?.subject_class)}
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Process stage</Box>
                <Box>{rule.scope?.process_stage || "any"}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Level</Box>
                <Box>{rule.level}</Box>
              </div>
            </ColumnLayout>

            <Table
              variant="embedded"
              header={<Header variant="h3">Conditions</Header>}
              items={rule.conditions ?? []}
              columnDefinitions={[
                {
                  id: "binding",
                  header: "Binding",
                  cell: (c) => (
                    <Box variant="code" title={c.binding}>
                      {localName(c.binding)}
                    </Box>
                  ),
                },
                {
                  id: "operator",
                  header: "Operator",
                  cell: (c) => <Box variant="code">{c.operator}</Box>,
                  width: 110,
                },
                {
                  id: "comparand",
                  header: "Comparand",
                  cell: (c) => comparandCell(rule, c),
                },
              ]}
              empty="No conditions"
            />

            <Table
              variant="embedded"
              header={
                <Header
                  variant="h3"
                  description="A bound the source document never stated cannot be represented here — it is recorded as a missing input instead. Approving a bound is the one step a machine may not take on your behalf."
                >
                  Thresholds
                </Header>
              }
              items={rule.thresholds ?? []}
              columnDefinitions={[
                {
                  id: "threshold_id",
                  header: "ID",
                  cell: (t) => <Box variant="code">{t.threshold_id}</Box>,
                  width: 130,
                },
                {
                  id: "value",
                  header: "Value",
                  cell: (t) => `${t.value} ${t.unit}`,
                  width: 130,
                },
                {
                  id: "source_span",
                  header: "Source span",
                  cell: (t) => <Box variant="p">{t.source_span}</Box>,
                },
                {
                  id: "approval",
                  header: "Approval",
                  cell: (t) =>
                    t.approved_by ? (
                      <StatusIndicator type="success">
                        {t.approved_by}
                      </StatusIndicator>
                    ) : (
                      <Button
                        variant="inline-link"
                        loading={busy === t.threshold_id}
                        onClick={() => handleApproveThreshold(t)}
                      >
                        Approve threshold
                      </Button>
                    ),
                  width: 210,
                },
              ]}
              empty="No thresholds — every comparand is a literal."
            />

            <Container header={<Header variant="h3">Conclusion</Header>}>
              <SpaceBetween size="xs">
                <Box>
                  <Box variant="strong">
                    {EPISTEMIC_LABEL[rule.conclusion?.epistemic_status] ??
                      rule.conclusion?.epistemic_status}
                  </Box>{" "}
                  <Box
                    variant="code"
                    display="inline"
                    title={rule.conclusion?.outcome}
                  >
                    {localName(rule.conclusion?.outcome)}
                  </Box>
                </Box>
                {rule.conclusion?.attested_by && (
                  <Box fontSize="body-s" color="text-body-secondary">
                    Attested by {rule.conclusion.attested_by}
                    {rule.conclusion.attestation_note
                      ? ` — ${rule.conclusion.attestation_note}`
                      : ""}
                  </Box>
                )}
              </SpaceBetween>
            </Container>

            {rule.provenance && (
              <Container
                header={
                  <Header
                    variant="h3"
                    description="Travels with the rule for its whole life. A rule you cannot trace back to a sentence cannot be reviewed."
                  >
                    Provenance
                  </Header>
                }
              >
                <SpaceBetween size="xs">
                  <Box fontSize="body-s" color="text-body-secondary">
                    {rule.provenance.document_id}
                    {rule.provenance.page
                      ? `, page ${rule.provenance.page}`
                      : ""}
                    {rule.provenance.model_id
                      ? ` — extracted by ${rule.provenance.model_id}`
                      : ""}
                  </Box>
                  <Box variant="p">
                    &ldquo;{rule.provenance.source_sentence}&rdquo;
                  </Box>
                </SpaceBetween>
              </Container>
            )}

            {(reports.length > 0 || missingInputs.length > 0) && (
              <SpaceBetween size="m">
                <Table
                  variant="embedded"
                  header={<Header variant="h3">Validation</Header>}
                  items={reports}
                  trackBy="level"
                  columnDefinitions={[
                    {
                      id: "level",
                      header: "Level",
                      cell: (r) => r.level,
                      width: 160,
                    },
                    {
                      id: "passed",
                      header: "Result",
                      cell: (r) => (
                        <StatusIndicator type={r.passed ? "success" : "error"}>
                          {r.passed ? "passed" : "failed"}
                        </StatusIndicator>
                      ),
                      width: 130,
                    },
                    {
                      id: "violations",
                      header: "Violations",
                      cell: (r) =>
                        (r.violations ?? []).length
                          ? r.violations.join("; ")
                          : "—",
                    },
                  ]}
                  empty="Not validated yet"
                />

                {missingInputs.length > 0 && (
                  <Alert type="info" header="Missing inputs (knowledge gap)">
                    <SpaceBetween size="xs">
                      <span>
                        This rule will advise rather than evaluate until these
                        inputs are mapped to real columns:
                      </span>
                      <ul>
                        {missingInputs.map((m) => (
                          <li key={m}>
                            <Box variant="code">{m}</Box>
                          </li>
                        ))}
                      </ul>
                    </SpaceBetween>
                  </Alert>
                )}
              </SpaceBetween>
            )}

            {blockers.length > 0 && (
              <Alert type="warning" header="Publish is unavailable">
                <ul>
                  {blockers.map((b) => (
                    <li key={b}>{b}</li>
                  ))}
                </ul>
              </Alert>
            )}
          </SpaceBetween>
        </Container>
      )}

      {/* ── Author / edit a rule by hand ── */}
      <Modal
        visible={formOpen}
        size="large"
        header={editing ? `Edit ${editing.name}` : "New governed rule"}
        onDismiss={() => setFormOpen(false)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setFormOpen(false)}
                disabled={saving}
              >
                Cancel
              </Button>
              <Button variant="primary" onClick={handleSave} loading={saving}>
                Save rule
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="l">
          {formErrors.length > 0 && (
            <Alert type="error" header="This rule cannot be saved yet">
              <ul>
                {formErrors.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            </Alert>
          )}

          <Alert type="info">
            Saved as <b>DRAFT</b> whatever is entered here. Each threshold still
            needs its own approval, the rule then needs approving, and only
            publishing compiles it to SPARQL — so nothing typed here can execute
            until a human walks it through those three steps.
            {editing && (
              <>
                {" "}
                Editing an approved or published rule <b>
                  returns it to DRAFT
                </b>{" "}
                and discards its compiled SPARQL: the previous sign-off was for
                the previous logic.
              </>
            )}
          </Alert>

          <ColumnLayout columns={2}>
            <FormField
              label="Rule id"
              description={
                editing
                  ? "Fixed: the id is the rule's IRI. Delete and re-create to rename."
                  : "Becomes part of the rule's IRI. Letters, digits and . _ : - only."
              }
            >
              <Input
                value={form.rule_id}
                placeholder="RULE-lapse-risk-90d"
                disabled={!!editing}
                onChange={({ detail }) => setField("rule_id", detail.value)}
              />
            </FormField>
            <FormField
              label="Name"
              description="Matched against the user's question, so phrase it the way the business says it."
            >
              <Input
                value={form.name}
                placeholder="Lapse risk after 90 days"
                onChange={({ detail }) => setField("name", detail.value)}
              />
            </FormField>
          </ColumnLayout>

          <FormField
            label="Description"
            description="Also matched against the question, and it is what the next reviewer reads."
          >
            <Textarea
              value={form.description}
              rows={2}
              placeholder="A holding unpaid for more than 90 days is at risk of lapsing."
              onChange={({ detail }) => setField("description", detail.value)}
            />
          </FormField>

          <FormField
            label="Synonyms"
            description="Comma-separated alternate phrasings, matched alongside the name."
          >
            <Input
              value={form.synonyms}
              placeholder="non-payment lapse, dormancy"
              onChange={({ detail }) => setField("synonyms", detail.value)}
            />
          </FormField>

          <ColumnLayout columns={2}>
            <FormField label="Rule type">
              <Select
                ariaLabel="Rule type"
                selectedOption={optionFor(RULE_TYPE_OPTIONS, form.rule_type)}
                options={RULE_TYPE_OPTIONS}
                onChange={({ detail }) =>
                  setField("rule_type", detail.selectedOption.value)
                }
              />
            </FormField>
            <FormField label="Level">
              <Select
                ariaLabel="Level"
                selectedOption={optionFor(LEVEL_OPTIONS, form.level)}
                options={LEVEL_OPTIONS}
                onChange={({ detail }) =>
                  setField("level", detail.selectedOption.value)
                }
              />
            </FormField>
          </ColumnLayout>

          <ColumnLayout columns={2}>
            <FormField
              label="Subject class"
              description="The ontology class each instance the rule judges belongs to."
            >
              <Input
                value={form.subject_class}
                placeholder="https://example.org/vkg#Holding"
                onChange={({ detail }) =>
                  setField("subject_class", detail.value)
                }
              />
            </FormField>
            <FormField
              label="Process stage"
              description="Optional. Limits the rule to one stage, e.g. renewal."
            >
              <Input
                value={form.process_stage}
                placeholder="renewal"
                onChange={({ detail }) =>
                  setField("process_stage", detail.value)
                }
              />
            </FormField>
          </ColumnLayout>

          {/* Conditions */}
          <SpaceBetween size="m">
            <Header
              variant="h3"
              description="All conditions must hold for the rule to fire. Each compares one ontology property against either an approved threshold or an inline literal — never both."
              actions={
                <Button onClick={() => addRow("conditions", emptyCondition)}>
                  Add condition
                </Button>
              }
            >
              Conditions
            </Header>
            {form.conditions.map((c, i) => (
              <Container
                key={i}
                header={
                  <Header
                    variant="h3"
                    actions={
                      form.conditions.length > 1 && (
                        <Button
                          variant="inline-link"
                          onClick={() => removeRow("conditions", i)}
                        >
                          Remove
                        </Button>
                      )
                    }
                  >
                    {`Condition ${i + 1}`}
                  </Header>
                }
              >
                <SpaceBetween size="m">
                  <ColumnLayout columns={3}>
                    <FormField
                      label="Binding"
                      description="An ontology property IRI on the subject class."
                    >
                      <Input
                        value={c.binding}
                        placeholder="https://example.org/vkg#daysSinceLastPayment"
                        onChange={({ detail }) =>
                          setRowField("conditions", i, "binding", detail.value)
                        }
                      />
                    </FormField>
                    <FormField label="Operator">
                      <Select
                        ariaLabel={`Operator for condition ${i + 1}`}
                        selectedOption={optionFor(OPERATOR_OPTIONS, c.operator)}
                        options={OPERATOR_OPTIONS}
                        onChange={({ detail }) =>
                          setRowField(
                            "conditions",
                            i,
                            "operator",
                            detail.selectedOption.value,
                          )
                        }
                      />
                    </FormField>
                    <FormField label="Compared against">
                      <Select
                        ariaLabel={`Comparand source for condition ${i + 1}`}
                        selectedOption={optionFor(COMPARAND_OPTIONS, c.mode)}
                        options={COMPARAND_OPTIONS}
                        onChange={({ detail }) =>
                          setRowField(
                            "conditions",
                            i,
                            "mode",
                            detail.selectedOption.value,
                          )
                        }
                      />
                    </FormField>
                  </ColumnLayout>
                  {c.mode === "threshold" ? (
                    <FormField
                      label="Threshold id"
                      description="Must name one of the thresholds defined below. A threshold carries the sentence its number came from; a literal does not."
                    >
                      <Input
                        value={c.threshold_id}
                        placeholder="THR-090 (defined below)"
                        onChange={({ detail }) =>
                          setRowField(
                            "conditions",
                            i,
                            "threshold_id",
                            detail.value,
                          )
                        }
                      />
                    </FormField>
                  ) : (
                    <FormField
                      label="Literal value"
                      description="For a value that is part of the logic itself, not a reviewable bound. Numbers and true/false are stored as such, everything else as text."
                    >
                      <Input
                        value={c.literal}
                        placeholder={'90, true or "renewal"'}
                        onChange={({ detail }) =>
                          setRowField("conditions", i, "literal", detail.value)
                        }
                      />
                    </FormField>
                  )}
                </SpaceBetween>
              </Container>
            ))}
          </SpaceBetween>

          {/* Thresholds */}
          <SpaceBetween size="m">
            <Header
              variant="h3"
              description="Every reviewable bound, with the verbatim sentence that states it. A bound no document states must go in Missing inputs instead — inventing one here is the failure this form exists to prevent."
              actions={
                <Button onClick={() => addRow("thresholds", emptyThreshold)}>
                  Add threshold
                </Button>
              }
            >
              Thresholds
            </Header>
            {form.thresholds.map((t, i) => {
              const prior = (editing?.thresholds ?? []).find(
                (p) => p.threshold_id === t.threshold_id.trim(),
              );
              return (
                <Container
                  key={i}
                  header={
                    <Header
                      variant="h3"
                      actions={
                        <Button
                          variant="inline-link"
                          onClick={() => removeRow("thresholds", i)}
                        >
                          Remove
                        </Button>
                      }
                    >
                      {`Threshold ${i + 1}`}
                    </Header>
                  }
                >
                  <SpaceBetween size="m">
                    <ColumnLayout columns={3}>
                      <FormField label="Id">
                        <Input
                          value={t.threshold_id}
                          placeholder="THR-090"
                          onChange={({ detail }) =>
                            setRowField(
                              "thresholds",
                              i,
                              "threshold_id",
                              detail.value,
                            )
                          }
                        />
                      </FormField>
                      <FormField label="Value">
                        <Input
                          value={t.value}
                          type="number"
                          placeholder="90"
                          onChange={({ detail }) =>
                            setRowField("thresholds", i, "value", detail.value)
                          }
                        />
                      </FormField>
                      <FormField label="Unit">
                        <Input
                          value={t.unit}
                          placeholder="days"
                          onChange={({ detail }) =>
                            setRowField("thresholds", i, "unit", detail.value)
                          }
                        />
                      </FormField>
                    </ColumnLayout>
                    <FormField
                      label="Source span"
                      description="The sentence, quoted exactly as written, that states this bound."
                    >
                      <Textarea
                        value={t.source_span}
                        rows={2}
                        placeholder="lapses after 90 days of non-payment"
                        onChange={({ detail }) =>
                          setRowField(
                            "thresholds",
                            i,
                            "source_span",
                            detail.value,
                          )
                        }
                      />
                    </FormField>
                    {prior?.approved_by && (
                      <Box fontSize="body-s" color="text-body-secondary">
                        Approved by {prior.approved_by}. Changing the value,
                        unit or span withdraws that approval — the signature was
                        for this exact bound.
                      </Box>
                    )}
                  </SpaceBetween>
                </Container>
              );
            })}
          </SpaceBetween>

          {/* Conclusion */}
          <SpaceBetween size="m">
            <Header
              variant="h3"
              description="What the rule concludes, and how strongly it is entitled to claim it."
            >
              Conclusion
            </Header>
            <ColumnLayout columns={2}>
              <FormField
                label="Outcome"
                description="The ontology class or state the rule concludes."
              >
                <Input
                  value={form.outcome}
                  placeholder="https://example.org/vkg#LapseRisk"
                  onChange={({ detail }) => setField("outcome", detail.value)}
                />
              </FormField>
              <FormField
                label="Epistemic status"
                description="Weakest first. A correlation stored as a confirmed cause is the failure this field prevents."
              >
                <Select
                  ariaLabel="Epistemic status"
                  selectedOption={optionFor(
                    EPISTEMIC_OPTIONS,
                    form.epistemic_status,
                  )}
                  options={EPISTEMIC_OPTIONS}
                  onChange={({ detail }) =>
                    setField("epistemic_status", detail.selectedOption.value)
                  }
                />
              </FormField>
            </ColumnLayout>
            {form.epistemic_status === STRONGEST_EPISTEMIC_STATUS && (
              <SpaceBetween size="s">
                <Checkbox
                  checked={form.attested}
                  onChange={({ detail }) =>
                    setField("attested", detail.checked)
                  }
                >
                  I attest to this causal claim
                  {currentUser ? ` as ${currentUser}` : ""}
                </Checkbox>
                <FormField
                  label="Attestation note"
                  description="What you relied on — the study, the review, the sign-off. Recorded against your identity, not typed as a name."
                >
                  <Textarea
                    value={form.attestation_note}
                    rows={2}
                    placeholder="Confirmed by the 2026 actuarial review"
                    onChange={({ detail }) =>
                      setField("attestation_note", detail.value)
                    }
                  />
                </FormField>
              </SpaceBetween>
            )}
          </SpaceBetween>

          <FormField
            label="Missing inputs"
            description="Comma-separated IRIs this rule needs but the layer does not map yet. A rule with missing inputs still has value — it advises instead of evaluating — and this is where an unstated bound belongs."
          >
            <Input
              value={form.missing_inputs}
              placeholder="https://example.org/vkg#coolingCapacity"
              onChange={({ detail }) =>
                setField("missing_inputs", detail.value)
              }
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* ── Import mined rules ── */}
      <Modal
        visible={importOpen}
        header="Import mined rules"
        onDismiss={() => setImportOpen(false)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setImportOpen(false)}
                disabled={importing}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleImport}
                loading={importing}
                disabled={!importDocId.trim()}
              >
                Import
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            The document pipeline proposes rules from every ingested document.
            They arrive as DRAFT with nothing approved: each threshold still
            needs your sign-off, and no rule is compiled until you publish it.
          </Box>
          <FormField
            label="Document id"
            description="The same id the document has in the Documents tab. The artefact is read from mined-rules/<id>.json."
          >
            <Input
              value={importDocId}
              placeholder="doc-7"
              onChange={({ detail }) => setImportDocId(detail.value)}
            />
          </FormField>

          {/* Collapsed by default: the routine case is importing what the
              pipeline already wrote, and only the author of an artefact by hand
              needs the contract. But it has to BE here — there is no other
              place the format is written down for someone in this dialog. */}
          <ExpandableSection headerText="Artefact format and location">
            <SpaceBetween size="m">
              <Box variant="p">
                The pipeline&apos;s ruleminer stage writes one JSON object per
                document to the artifacts bucket (the REST API reads{" "}
                <Box variant="code" display="inline">
                  ARTIFACTS_BUCKET
                </Box>
                , which app.ts points at the same bucket the pipeline writes to)
                under the key{" "}
                <Box variant="code" display="inline">
                  mined-rules/&lt;documentId&gt;.json
                </Box>
                . Only the document id is typed above; the key is built from it,
                because a typo in a path is far less recoverable than one in an
                id you already know. To import a hand-written artefact, upload
                it to that exact key first.
              </Box>

              <Box variant="p">
                The artefact&apos;s keys are{" "}
                <Box variant="strong" display="inline">
                  camelCase
                </Box>{" "}
                — unlike the snake_case fields of the API&apos;s Rule model. A
                threshold&apos;s{" "}
                <Box variant="code" display="inline">
                  sourceSpan
                </Box>{" "}
                is mandatory and must be the sentence quoted verbatim.
              </Box>

              <Box variant="code">
                <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                  {ARTEFACT_EXAMPLE}
                </pre>
              </Box>

              <Box variant="p">
                <Box variant="strong" display="inline">
                  What the server derives or overrides, whatever the file says:
                </Box>
              </Box>
              <ul>
                <li>
                  <Box variant="code" display="inline">
                    rule_id
                  </Box>{" "}
                  is not read from the artefact. It is derived as{" "}
                  <Box variant="code" display="inline">
                    RULE-&lt;sha256(documentId|index|name)[:12]&gt;
                  </Box>
                  , so re-importing the same artefact updates the same drafts
                  instead of duplicating them — but renaming a rule, or
                  reordering the list, mints a new id and leaves the old draft
                  behind.
                </li>
                <li>
                  <Box variant="code" display="inline">
                    lifecycle
                  </Box>{" "}
                  is forced to DRAFT, every threshold&apos;s approval is
                  dropped, no conclusion is imported as attested, and any{" "}
                  <Box variant="code" display="inline">
                    compiledSparql
                  </Box>{" "}
                  in the file is discarded. Nothing in a JSON file can grant
                  itself governance.
                </li>
                <li>
                  <Box variant="code" display="inline">
                    level
                  </Box>{" "}
                  is set to{" "}
                  <Box variant="code" display="inline">
                    contextualised
                  </Box>
                  : a mined rule always carries a scope, so it is context-bound
                  by construction.
                </li>
                <li>
                  An unrecognised{" "}
                  <Box variant="code" display="inline">
                    ruleType
                  </Box>{" "}
                  becomes{" "}
                  <Box variant="code" display="inline">
                    advisory
                  </Box>{" "}
                  rather than failing (
                  <Box variant="code" display="inline">
                    eligibility
                  </Box>{" "}
                  maps to{" "}
                  <Box variant="code" display="inline">
                    compliance
                  </Box>
                  ,{" "}
                  <Box variant="code" display="inline">
                    classification
                  </Box>{" "}
                  to{" "}
                  <Box variant="code" display="inline">
                    diagnostic
                  </Box>
                  ), and an unrecognised{" "}
                  <Box variant="code" display="inline">
                    epistemicStatus
                  </Box>{" "}
                  is weakened to{" "}
                  <Box variant="code" display="inline">
                    candidateContributorTo
                  </Box>{" "}
                  — a status nobody recognises must never become a causal claim
                  by accident.
                </li>
                <li>
                  A condition carries{" "}
                  <Box variant="code" display="inline">
                    thresholdId
                  </Box>{" "}
                  <b>or</b>{" "}
                  <Box variant="code" display="inline">
                    value
                  </Box>
                  , never both and never neither.
                </li>
              </ul>

              <Alert type="info" header="Import is all-or-nothing">
                A structural defect — a threshold with no{" "}
                <Box variant="code" display="inline">
                  sourceSpan
                </Box>
                , a rule with no conditions — rejects the whole artefact with a
                422 naming the field. Nothing is imported. That is deliberate:
                importing the valid subset would hide a miner regression behind
                a set of plausible-looking drafts.
              </Alert>
            </SpaceBetween>
          </ExpandableSection>
        </SpaceBetween>
      </Modal>

      {/* ── Delete confirm ── */}
      <Modal
        visible={!!confirmDelete}
        header="Delete rule?"
        onDismiss={() => setConfirmDelete(null)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setConfirmDelete(null)}
                disabled={deleting}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleDelete}
                loading={deleting}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        This permanently removes
        {confirmDelete ? ` "${confirmDelete.name}"` : " the rule"} and its
        thresholds from the layer&apos;s rules graph. If it was published, the
        query agent stops consulting it.
      </Modal>
    </SpaceBetween>
  );
}
