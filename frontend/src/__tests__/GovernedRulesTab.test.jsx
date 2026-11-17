/**
 * GovernedRulesTab behaviour.
 *
 * Focused on the governance affordances that must not silently regress — the
 * per-threshold human approval, the validation ladder, the publish gate, and
 * the epistemic status wording — not on Cloudscape rendering details.
 *
 * Uses jest + the `{success, data}` envelope, not the plan's vitest sketch:
 * every `rulesAPI` method goes through `handleResponse`, which CATCHES and
 * returns `{success:false, error, details}`. A test that mocks a rejected
 * promise would exercise a code path the real client can never produce.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockList = jest.fn();
const mockApproveThreshold = jest.fn();
const mockApprove = jest.fn();
const mockValidate = jest.fn();
const mockPublish = jest.fn();
const mockRemove = jest.fn();
const mockImportMined = jest.fn();
const mockCreate = jest.fn();
const mockUpdate = jest.fn();

jest.mock("../services/api", () => ({
  rulesAPI: {
    list: (...a) => mockList(...a),
    approveThreshold: (...a) => mockApproveThreshold(...a),
    approve: (...a) => mockApprove(...a),
    validate: (...a) => mockValidate(...a),
    publish: (...a) => mockPublish(...a),
    remove: (...a) => mockRemove(...a),
    importMined: (...a) => mockImportMined(...a),
    create: (...a) => mockCreate(...a),
    update: (...a) => mockUpdate(...a),
  },
}));

import GovernedRulesTab from "../components/GovernedRulesTab";

// Shaped after agents/shared/rule_models.py:Rule — every required field is
// present so the fixture cannot drift from what the API actually returns.
const RULE = {
  rule_id: "RULE-004",
  namespace: "ont-1",
  name: "lapse risk",
  description: "days since last payment",
  rule_type: "risk",
  level: "general",
  lifecycle: "DRAFT",
  scope: { subject_class: "https://ex.org/vkg#Holding", process_stage: null },
  conditions: [
    {
      binding: "https://ex.org/vkg#daysSinceLastPayment",
      operator: ">",
      threshold_id: "THR-011",
      value: null,
    },
  ],
  conclusion: {
    outcome: "https://ex.org/vkg#LapseRisk",
    epistemic_status: "candidateContributorTo",
    attested_by: null,
    attestation_note: null,
  },
  thresholds: [
    {
      threshold_id: "THR-011",
      value: 90,
      unit: "days",
      source_span: "lapses after 90 days of non-payment",
      approved_by: null,
    },
  ],
  missing_inputs: [],
  synonyms: [],
  test_cases: [],
  provenance: {
    document_id: "doc-1",
    page: 4,
    source_sentence: "A holding lapses after 90 days of non-payment.",
    model_id: "claude",
    confidence: 0.9,
  },
  compiled_sparql: null,
  version: 1,
};

const APPROVED_RULE = {
  ...RULE,
  lifecycle: "APPROVED",
  reviewed_by: "alice@x",
  thresholds: [{ ...RULE.thresholds[0], approved_by: "alice@x" }],
};

const ALL_PASSED = {
  success: true,
  data: {
    reports: [
      { level: "structural", passed: true, violations: [] },
      { level: "semantic", passed: true, violations: [] },
      { level: "operational", passed: true, violations: [] },
      { level: "epistemic", passed: true, violations: [] },
    ],
    missing_inputs: [],
  },
};

beforeEach(() => {
  [
    mockList,
    mockApproveThreshold,
    mockApprove,
    mockValidate,
    mockPublish,
    mockRemove,
    mockImportMined,
    mockCreate,
    mockUpdate,
  ].forEach((m) => m.mockReset());
  mockList.mockResolvedValue({ success: true, data: [RULE] });
});

test("lists rules with their lifecycle", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  expect((await screen.findAllByText("lapse risk")).length).toBeGreaterThan(0);
  expect(screen.getByText("DRAFT")).toBeInTheDocument();
});

test("shows the source span for each threshold", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  // Exact match, not a regex: the provenance sentence legitimately contains the
  // span as a substring, and the assertion is about the threshold's own cell.
  expect(
    await screen.findByText("lapses after 90 days of non-payment"),
  ).toBeInTheDocument();
});

test("disables Publish while a threshold is unapproved", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  expect(screen.getByRole("button", { name: /^publish$/i })).toBeDisabled();
});

test("sends the approver when approving a threshold", async () => {
  mockApproveThreshold.mockResolvedValue({
    success: true,
    data: APPROVED_RULE,
  });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="VKG"
      currentUser="alice@x"
    />,
  );
  await screen.findAllByText("lapse risk");
  await userEvent.click(
    screen.getByRole("button", { name: /approve threshold/i }),
  );
  await waitFor(() =>
    expect(mockApproveThreshold).toHaveBeenCalledWith(
      "ont-1",
      "RULE-004",
      "THR-011",
      "alice@x",
    ),
  );
});

test("renders one row per validation level", async () => {
  mockValidate.mockResolvedValue({
    success: true,
    data: {
      reports: [
        { level: "structural", passed: true, violations: [] },
        { level: "semantic", passed: true, violations: [] },
        {
          level: "operational",
          passed: false,
          violations: ["binding X unmapped"],
        },
        { level: "epistemic", passed: true, violations: [] },
      ],
      missing_inputs: [],
    },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /^validate$/i }));
  expect(await screen.findByText("operational")).toBeInTheDocument();
  expect(screen.getByText(/binding X unmapped/)).toBeInTheDocument();
});

test("shows missing inputs as a knowledge gap, not as an error", async () => {
  mockValidate.mockResolvedValue({
    success: true,
    data: {
      reports: [{ level: "structural", passed: true, violations: [] }],
      missing_inputs: ["https://ex.org/vkg#coolingCapacity"],
    },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /^validate$/i }));
  expect(await screen.findByText(/coolingCapacity/)).toBeInTheDocument();
  expect(screen.getByText(/will advise/i)).toBeInTheDocument();
});

test("surfaces a blocked publish reason verbatim", async () => {
  mockList.mockResolvedValue({ success: true, data: [APPROVED_RULE] });
  mockValidate.mockResolvedValue(ALL_PASSED);
  mockPublish.mockResolvedValue({
    success: false,
    error: "Request failed with status code 422",
    details: { detail: "Ontop could not translate the compiled rule" },
  });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="VKG"
      currentUser="alice@x"
    />,
  );
  await screen.findAllByText("lapse risk");
  // Publish is gated on a green ladder, so the reviewer validates first.
  await userEvent.click(screen.getByRole("button", { name: /^validate$/i }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /^publish$/i })).toBeEnabled(),
  );
  await userEvent.click(screen.getByRole("button", { name: /^publish$/i }));
  expect(
    await screen.findByText(/Ontop could not translate/),
  ).toBeInTheDocument();
});

test("shows the epistemic status in words", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  expect(await screen.findByText(/candidate contributor/i)).toBeInTheDocument();
});

test("renders an empty state when there are no rules", async () => {
  mockList.mockResolvedValue({ success: true, data: [] });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  expect(await screen.findByText(/no governed rules/i)).toBeInTheDocument();
});

// ── Import of doc-pipeline mined rules ──────────────────────────────────────
// The admin types a document id; the tab derives the artefact key the pipeline
// wrote (`mined-rules/<documentId>.json`). Tested because a key the tab builds
// wrongly 404s, which the empty-list state would render as "that document had
// no rules in it" — the one wrong conclusion an admin must not be led to.

test("derives the artefact key from the document id", async () => {
  mockImportMined.mockResolvedValue({
    success: true,
    data: { rules: [RULE], count: 1 },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /import mined/i }));
  await userEvent.type(await screen.findByPlaceholderText(/doc-/i), "doc-7");
  await userEvent.click(screen.getByRole("button", { name: /^import$/i }));
  await waitFor(() =>
    expect(mockImportMined).toHaveBeenCalledWith(
      "ont-1",
      "mined-rules/doc-7.json",
    ),
  );
});

test("refreshes the list after a successful import", async () => {
  mockImportMined.mockResolvedValue({
    success: true,
    data: { rules: [RULE], count: 3 },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  mockList.mockClear();
  await userEvent.click(screen.getByRole("button", { name: /import mined/i }));
  await userEvent.type(await screen.findByPlaceholderText(/doc-/i), "doc-7");
  await userEvent.click(screen.getByRole("button", { name: /^import$/i }));
  // The count is the reviewer's only confirmation that anything landed, and the
  // list must re-read from the server rather than trusting the response body.
  expect(await screen.findByText(/3 rule/i)).toBeInTheDocument();
  await waitFor(() => expect(mockList).toHaveBeenCalled());
});

test("says an artefact is missing rather than reporting a failure", async () => {
  // 404 here means the pipeline mined nothing for that document (or the id is
  // wrong) — a fact about the document, not a fault the admin can fix by retry.
  mockImportMined.mockResolvedValue({
    success: false,
    error: "Request failed with status code 404",
    details: { detail: "mined-rules artefact not found" },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /import mined/i }));
  await userEvent.type(await screen.findByPlaceholderText(/doc-/i), "nope");
  await userEvent.click(screen.getByRole("button", { name: /^import$/i }));
  expect(await screen.findByText(/no mined rules/i)).toBeInTheDocument();
});

test("surfaces a malformed artefact reason verbatim", async () => {
  // 422 is a miner regression: the reason names the field, and hiding it behind
  // a generic message would leave nobody able to say WHICH artefact is bad.
  mockImportMined.mockResolvedValue({
    success: false,
    error: "Request failed with status code 422",
    details: { detail: "sourceSpan must not be blank" },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /import mined/i }));
  await userEvent.type(await screen.findByPlaceholderText(/doc-/i), "doc-7");
  await userEvent.click(screen.getByRole("button", { name: /^import$/i }));
  // Matched on the whole sentence, not on `sourceSpan` alone: the dialog now also
  // documents the field in its format section, so the bare field name appears in
  // several places and would make this query ambiguous rather than wrong.
  expect(
    await screen.findByText(/sourceSpan must not be blank/),
  ).toBeInTheDocument();
});

test("will not import without a document id", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /import mined/i }));
  await screen.findByPlaceholderText(/doc-/i);
  expect(screen.getByRole("button", { name: /^import$/i })).toBeDisabled();
  expect(mockImportMined).not.toHaveBeenCalled();
});

// ── The artefact contract, stated in the dialog ──────────────────────────────
// Import is the only way a rule reaches a layer without being typed by hand, yet
// nothing in the UI used to say what an artefact looks like or where it must
// live. These two tests pin the facts an author cannot guess and cannot recover
// from getting wrong: the key the file must occupy (a wrong key 404s, which
// reads as "that document had no rules"), and the camelCase spelling of a schema
// whose server-side counterpart is snake_case.
//
// The Cloudscape mock renders an ExpandableSection's children whether or not it
// is expanded, so these assert the content EXISTS in the dialog, not that it is
// visible without a click. That is the right level: which disclosure widget
// holds the text is a layout decision, its presence is the contract.

test("states where a mined-rules artefact must live", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /import mined/i }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("ARTIFACTS_BUCKET");
  expect(dialog).toHaveTextContent("mined-rules/<documentId>.json");
});

test("states the artefact's shape, casing and the fields the server forces", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /import mined/i }));
  const dialog = await screen.findByRole("dialog");

  // A worked example, not a prose description of one: an author copies it.
  expect(dialog).toHaveTextContent('"documentId"');
  expect(dialog).toHaveTextContent('"subjectClass"');
  expect(dialog).toHaveTextContent('"epistemicStatus"');
  // The single most confusable thing about the format, so it is called out in
  // words as well as being visible in the example.
  expect(dialog).toHaveTextContent(/camelCase/);

  // Nothing in a file can grant itself governance — an author who does not know
  // this will believe an approval they wrote into JSON took effect.
  expect(dialog).toHaveTextContent(/forced to DRAFT/);
  expect(dialog).toHaveTextContent(/rule_id/);
  expect(dialog).toHaveTextContent("RULE-<sha256(documentId|index|name)[:12]>");
  // Downgrades are silent by design; unstated they look like data loss.
  expect(dialog).toHaveTextContent(/candidateContributorTo/);
  // And a partial import would hide a miner regression behind plausible drafts.
  expect(dialog).toHaveTextContent(/all-or-nothing/i);
});

// ── Layer-type applicability ────────────────────────────────────────────────
// The tab is reachable from a page that used to hardcode layerType="VKG", so
// this guard could never fire. It now receives the layer's real type, and a
// Hybrid layer must NOT be told rules don't apply to it — they do.

const UNSUPPORTED = /apply to VKG and Hybrid layers/i;

test("says rules do not apply to a Semantic RAG layer", async () => {
  mockList.mockResolvedValue({ success: true, data: [] });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="SemanticRAG" />);
  expect(await screen.findByText(UNSUPPORTED)).toBeInTheDocument();
});

test.each(["VKG", "Hybrid"])(
  "does not warn on a %s layer, which has an ontology graph",
  async (layerType) => {
    render(<GovernedRulesTab ontologyId="ont-1" layerType={layerType} />);
    await screen.findAllByText("lapse risk");
    expect(screen.queryByText(UNSUPPORTED)).not.toBeInTheDocument();
  },
);

test("makes no applicability claim when the layer type has not loaded", async () => {
  // The type comes from a separate config fetch that can fail independently of
  // the rules list. Rendering "rules do not apply here" off a failed fetch
  // would state something false about the reviewer's layer.
  render(<GovernedRulesTab ontologyId="ont-1" layerType={null} />);
  await screen.findAllByText("lapse risk");
  expect(screen.queryByText(UNSUPPORTED)).not.toBeInTheDocument();
});

// ── Manual authoring ────────────────────────────────────────────────────────
// `rulesAPI.create`/`update` existed from the first commit but nothing ever
// called them: the only way to get a rule into a layer was to mine one from a
// document. These tests cover the two things a hand-authoring form can get
// wrong that the backend will NOT catch for us:
//
//   1. `rule_graph.rule_to_graph` mints IRIs by string concatenation and wraps
//      `binding`/`subject_class`/`outcome` in a bare `URIRef(...)`. rdflib does
//      not validate IRIs, so a rule id containing a space or a relative binding
//      persists *silently wrong* triples. Rejecting those is a client-side job.
//   2. A threshold's `approved_by` is a human signature on one specific number.
//      The form builds the whole PUT body, so it is the only place that can
//      carry an approval across an edit that changed the number it signed.
//
// The "both comparands set" case pydantic guards against is unreachable here by
// construction — a condition's comparand is a select, not two free fields — so
// what is tested instead is a threshold reference that names nothing.

// Every placeholder in the form, so a renamed hint breaks the tests loudly
// instead of turning `getByPlaceholderText` into a silent no-match.
const PH = {
  ruleId: "RULE-lapse-risk-90d",
  name: "Lapse risk after 90 days",
  description: "A holding unpaid for more than 90 days is at risk of lapsing.",
  synonyms: "non-payment lapse, dormancy",
  subjectClass: "https://example.org/vkg#Holding",
  processStage: "renewal",
  binding: "https://example.org/vkg#daysSinceLastPayment",
  conditionThreshold: "THR-090 (defined below)",
  literal: '90, true or "renewal"',
  thresholdId: "THR-090",
  thresholdValue: "90",
  thresholdUnit: "days",
  sourceSpan: "lapses after 90 days of non-payment",
  outcome: "https://example.org/vkg#LapseRisk",
  attestationNote: "Confirmed by the 2026 actuarial review",
  missingInputs: "https://example.org/vkg#coolingCapacity",
};

/**
 * Replace the content of one field, addressed by its placeholder.
 *
 * @param {string} placeholder Exact placeholder text of the input.
 * @param {string} value Text to type; an empty string just clears the field.
 * @returns {Promise<void>}
 */
async function type(placeholder, value) {
  const el = screen.getByPlaceholderText(placeholder);
  await userEvent.clear(el);
  if (value) await userEvent.type(el, value);
}

/**
 * Fill the form with a rule that passes every client-side check.
 *
 * Each negative test overrides exactly one field, so the assertion is about
 * that field rather than about whichever error happens to be reported first.
 *
 * @param {object} [overrides] Field values replacing the valid defaults.
 * @returns {Promise<void>}
 */
async function fillValidRule(overrides = {}) {
  const v = {
    ruleId: "RULE-lapse",
    name: "Lapse risk",
    description: "Unpaid for more than the allowed window.",
    subjectClass: "https://ex.org/vkg#Holding",
    binding: "https://ex.org/vkg#daysSinceLastPayment",
    conditionThreshold: "THR-090",
    thresholdId: "THR-090",
    thresholdValue: "90",
    thresholdUnit: "days",
    sourceSpan: "lapses after 90 days of non-payment",
    outcome: "https://ex.org/vkg#LapseRisk",
    ...overrides,
  };
  await type(PH.ruleId, v.ruleId);
  await type(PH.name, v.name);
  await type(PH.description, v.description);
  await type(PH.subjectClass, v.subjectClass);
  await type(PH.binding, v.binding);
  await type(PH.conditionThreshold, v.conditionThreshold);
  await type(PH.thresholdId, v.thresholdId);
  await type(PH.thresholdValue, v.thresholdValue);
  await type(PH.thresholdUnit, v.thresholdUnit);
  await type(PH.sourceSpan, v.sourceSpan);
  await type(PH.outcome, v.outcome);
}

/**
 * Open the empty authoring form.
 *
 * @returns {Promise<void>}
 */
async function openNewRuleForm() {
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /add rule/i }));
  await screen.findByPlaceholderText(PH.ruleId);
}

const save = () =>
  userEvent.click(screen.getByRole("button", { name: /save rule/i }));

test("creates a rule in the nested shape the API validates", async () => {
  mockCreate.mockResolvedValue({ success: true, data: RULE });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="alice@x"
    />,
  );
  await openNewRuleForm();
  await fillValidRule();
  await save();

  await waitFor(() => expect(mockCreate).toHaveBeenCalled());
  const [ns, body] = mockCreate.mock.calls[0];
  expect(ns).toBe("ont-1");
  // `namespace` is forced from the route's layer id, never typed: the backend
  // 400s on a mismatch with the path, which a free-text field invites.
  expect(body.namespace).toBe("ont-1");
  // Three fields the server owns. Sending anything else here would let a
  // client claim a compiled, published rule.
  expect(body.lifecycle).toBe("DRAFT");
  expect(body.compiled_sparql).toBeNull();
  expect(body.reviewed_by).toBeNull();
  expect(body.scope).toEqual({
    subject_class: "https://ex.org/vkg#Holding",
    process_stage: null,
  });
  expect(body.conditions).toEqual([
    {
      binding: "https://ex.org/vkg#daysSinceLastPayment",
      operator: ">",
      threshold_id: "THR-090",
      value: null,
    },
  ]);
  expect(body.thresholds).toEqual([
    {
      threshold_id: "THR-090",
      value: 90,
      unit: "days",
      source_span: "lapses after 90 days of non-payment",
      approved_by: null,
    },
  ]);
  expect(body.conclusion.outcome).toBe("https://ex.org/vkg#LapseRisk");
  // A hand-authored rule has no extraction to cite. `Provenance` requires a
  // model_id and a confidence, so filling them in would fabricate one.
  expect(body.provenance).toBeNull();
});

test("stamps a threshold as unapproved however the form is filled", async () => {
  // The value is the author's; the signature is not. A newly authored bound
  // must still be approved through the per-threshold action.
  mockCreate.mockResolvedValue({ success: true, data: RULE });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="alice@x"
    />,
  );
  await openNewRuleForm();
  await fillValidRule();
  await save();
  await waitFor(() => expect(mockCreate).toHaveBeenCalled());
  expect(mockCreate.mock.calls[0][1].thresholds[0].approved_by).toBeNull();
});

test("stores a numeric literal comparand as a number", async () => {
  // Ontop compiles `?v > "90"` and `?v > 90` differently: the string form is a
  // lexicographic comparison, so "9" > "90" and the rule silently misfires.
  mockCreate.mockResolvedValue({ success: true, data: RULE });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="alice@x"
    />,
  );
  await openNewRuleForm();
  await fillValidRule();
  await userEvent.selectOptions(
    screen.getByLabelText("Comparand source for condition 1"),
    "literal",
  );
  await type(PH.literal, "90");
  await save();
  await waitFor(() => expect(mockCreate).toHaveBeenCalled());
  expect(mockCreate.mock.calls[0][1].conditions[0]).toEqual({
    binding: "https://ex.org/vkg#daysSinceLastPayment",
    operator: ">",
    threshold_id: null,
    value: 90,
  });
});

test("rejects a relative binding IRI instead of persisting a bad triple", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="Hybrid" />);
  await openNewRuleForm();
  await fillValidRule({ binding: "daysSinceLastPayment" });
  await save();
  expect(await screen.findByText(/absolute IRI/i)).toBeInTheDocument();
  expect(mockCreate).not.toHaveBeenCalled();
});

test("rejects a rule id that would not survive IRI minting", async () => {
  // The rule's IRI is `VKGR + rule_id` by concatenation, so a space produces an
  // IRI rdflib accepts and no SPARQL query can ever match.
  render(<GovernedRulesTab ontologyId="ont-1" layerType="Hybrid" />);
  await openNewRuleForm();
  await fillValidRule({ ruleId: "RULE lapse risk" });
  await save();
  // Matched on the error's own wording, not on "rule id" — the form's field
  // label says that too, and a two-match query would pass on the label alone.
  expect(
    await screen.findByText(/may only contain letters/i),
  ).toBeInTheDocument();
  expect(mockCreate).not.toHaveBeenCalled();
});

test("rejects a condition referencing a threshold that is not defined", async () => {
  render(<GovernedRulesTab ontologyId="ont-1" layerType="Hybrid" />);
  await openNewRuleForm();
  await fillValidRule({ conditionThreshold: "THR-999" });
  await save();
  expect(await screen.findByText(/THR-999/)).toBeInTheDocument();
  expect(mockCreate).not.toHaveBeenCalled();
});

test("rejects a threshold with no source span", async () => {
  // The span is the structural enforcement of "never invent a bound". A bound
  // the source never stated belongs in missing_inputs, not in a threshold.
  render(<GovernedRulesTab ontologyId="ont-1" layerType="Hybrid" />);
  await openNewRuleForm();
  await fillValidRule({ sourceSpan: "" });
  await save();
  expect(
    await screen.findByText(/belongs in Missing inputs/i),
  ).toBeInTheDocument();
  expect(mockCreate).not.toHaveBeenCalled();
});

test("rejects two thresholds sharing an id", async () => {
  // A duplicate id makes `threshold_by_id` return whichever comes first, so a
  // condition would silently bind to a bound nobody reviewed for it.
  render(<GovernedRulesTab ontologyId="ont-1" layerType="Hybrid" />);
  await openNewRuleForm();
  await fillValidRule();
  await userEvent.click(screen.getByRole("button", { name: /add threshold/i }));
  const ids = screen.getAllByPlaceholderText(PH.thresholdId);
  await userEvent.type(ids[1], "THR-090");
  const values = screen.getAllByPlaceholderText(PH.thresholdValue);
  await userEvent.type(values[1], "120");
  const units = screen.getAllByPlaceholderText(PH.thresholdUnit);
  await userEvent.type(units[1], "days");
  const spans = screen.getAllByPlaceholderText(PH.sourceSpan);
  await userEvent.type(spans[1], "or 120 days for annual policies");
  await save();
  expect(await screen.findByText(/more than once/i)).toBeInTheDocument();
  expect(mockCreate).not.toHaveBeenCalled();
});

test("refuses confirmedCauseOf without a named attestation", async () => {
  // The strongest status is the one claim a machine may never grant itself;
  // validate_epistemic rejects it server-side too, but a reviewer should learn
  // that here rather than from a 422.
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="alice@x"
    />,
  );
  await openNewRuleForm();
  await fillValidRule();
  await userEvent.selectOptions(
    screen.getByLabelText("Epistemic status"),
    "confirmedCauseOf",
  );
  await save();
  expect(await screen.findByText(/may not grant/i)).toBeInTheDocument();
  expect(mockCreate).not.toHaveBeenCalled();
});

test("records the signed-in reviewer as the attester, not typed text", async () => {
  mockCreate.mockResolvedValue({ success: true, data: RULE });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="alice@x"
    />,
  );
  await openNewRuleForm();
  await fillValidRule();
  await userEvent.selectOptions(
    screen.getByLabelText("Epistemic status"),
    "confirmedCauseOf",
  );
  await userEvent.click(screen.getByLabelText(/I attest/i));
  await type(PH.attestationNote, "Confirmed by the actuarial review.");
  await save();
  await waitFor(() => expect(mockCreate).toHaveBeenCalled());
  expect(mockCreate.mock.calls[0][1].conclusion).toEqual({
    outcome: "https://ex.org/vkg#LapseRisk",
    epistemic_status: "confirmedCauseOf",
    attested_by: "alice@x",
    attestation_note: "Confirmed by the actuarial review.",
  });
});

test("shows the backend's rejection inside the form, not behind it", async () => {
  mockCreate.mockResolvedValue({
    success: false,
    error: "Request failed with status code 422",
    details: { detail: "conditions.0.binding is not mapped in this ontology" },
  });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="alice@x"
    />,
  );
  await openNewRuleForm();
  await fillValidRule();
  await save();
  expect(await screen.findByText(/is not mapped/)).toBeInTheDocument();
  // Still open: closing on failure would discard everything just typed.
  expect(screen.getByPlaceholderText(PH.ruleId)).toBeInTheDocument();
});

// ── Editing an existing rule ────────────────────────────────────────────────

/**
 * Open the edit form for the single rule in the list.
 *
 * @returns {Promise<void>}
 */
async function openEditForm() {
  await screen.findAllByText("lapse risk");
  await userEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  await screen.findByPlaceholderText(PH.ruleId);
}

test("carries provenance through an edit and resets the review", async () => {
  // Provenance is the only trace back to the sentence a mined rule came from.
  // The form builds the entire PUT body, so dropping it here would erase it.
  mockList.mockResolvedValue({ success: true, data: [APPROVED_RULE] });
  mockUpdate.mockResolvedValue({ success: true, data: RULE });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="bob@x"
    />,
  );
  await openEditForm();
  await type(PH.name, "lapse risk, revised");
  await save();

  await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
  const [ns, ruleId, body] = mockUpdate.mock.calls[0];
  expect(ns).toBe("ont-1");
  expect(ruleId).toBe("RULE-004");
  expect(body.provenance).toEqual(RULE.provenance);
  expect(body.name).toBe("lapse risk, revised");
  // An edit un-approves the rule: the reviewer signed off on the previous text.
  expect(body.lifecycle).toBe("DRAFT");
  expect(body.reviewed_by).toBeNull();
  expect(body.compiled_sparql).toBeNull();
});

test("keeps a threshold approval when its bound is untouched", async () => {
  // Re-approving every bound after an unrelated wording fix would make the
  // approval a formality, which is exactly what it must not become.
  mockList.mockResolvedValue({ success: true, data: [APPROVED_RULE] });
  mockUpdate.mockResolvedValue({ success: true, data: RULE });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="bob@x"
    />,
  );
  await openEditForm();
  await type(PH.description, "reworded, same bound");
  await save();
  await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
  expect(mockUpdate.mock.calls[0][2].thresholds[0].approved_by).toBe("alice@x");
});

test("drops a threshold approval when the bound changes", async () => {
  mockList.mockResolvedValue({ success: true, data: [APPROVED_RULE] });
  mockUpdate.mockResolvedValue({ success: true, data: RULE });
  render(
    <GovernedRulesTab
      ontologyId="ont-1"
      layerType="Hybrid"
      currentUser="bob@x"
    />,
  );
  await openEditForm();
  await type(PH.thresholdValue, "120");
  await save();
  await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
  const threshold = mockUpdate.mock.calls[0][2].thresholds[0];
  expect(threshold.value).toBe(120);
  // alice approved 90 days, not 120.
  expect(threshold.approved_by).toBeNull();
});

test("will not let an edit change the rule id", async () => {
  // The id is the rule's IRI. Changing it would create a second rule and leave
  // the original in the graph, since the PUT is keyed by the path id.
  mockList.mockResolvedValue({ success: true, data: [APPROVED_RULE] });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="Hybrid" />);
  await openEditForm();
  expect(screen.getByPlaceholderText(PH.ruleId)).toBeDisabled();
});

test("prefills the form from the stored rule", async () => {
  mockList.mockResolvedValue({ success: true, data: [APPROVED_RULE] });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="Hybrid" />);
  await openEditForm();
  expect(screen.getByPlaceholderText(PH.name)).toHaveValue("lapse risk");
  expect(screen.getByPlaceholderText(PH.binding)).toHaveValue(
    "https://ex.org/vkg#daysSinceLastPayment",
  );
  expect(screen.getByPlaceholderText(PH.conditionThreshold)).toHaveValue(
    "THR-011",
  );
  expect(screen.getByPlaceholderText(PH.sourceSpan)).toHaveValue(
    "lapses after 90 days of non-payment",
  );
});

test("offers no authoring affordance on a Semantic RAG layer", async () => {
  // There is no ontology for a binding to resolve against, so offering the form
  // would produce a rule that can never validate.
  mockList.mockResolvedValue({ success: true, data: [] });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="SemanticRAG" />);
  await screen.findByText(UNSUPPORTED);
  expect(screen.getByRole("button", { name: /add rule/i })).toBeDisabled();
});

test("explains a 404 as a disabled feature flag rather than an error", async () => {
  // The router only mounts when RULES_ENABLED and NEPTUNE_GATEWAY_URL are both
  // set. Without them FastAPI returns a bare 404 for every /rules path, which
  // is a configuration state, not a failure the reviewer can act on.
  mockList.mockResolvedValue({
    success: false,
    error: "Request failed with status code 404",
    details: { detail: "Not Found" },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  // The alert says "not enabled" in both its header and its body, so match the
  // body's own wording — it is the part that names the flag to set.
  expect(await screen.findByText(/no \/rules API/i)).toBeInTheDocument();
});

test("reads the {rules, unreadable} envelope the list endpoint now returns", async () => {
  mockList.mockResolvedValue({
    success: true,
    data: { rules: [RULE], unreadable: [] },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  expect((await screen.findAllByText("lapse risk")).length).toBeGreaterThan(0);
});

test("warns about a rule that is stored but cannot be read", async () => {
  // A rule missing from the table with no warning is indistinguishable from a
  // rule that was deleted — which is exactly how the threshold-IRI incident
  // looked to a reviewer.
  mockList.mockResolvedValue({
    success: true,
    data: {
      rules: [RULE],
      unreadable: [
        {
          rule_id: "RULE-BAD",
          error: "rule RULE-BAD: threshold THR-011 is missing required unit",
        },
      ],
    },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  expect(await screen.findByText(/could not be read/i)).toBeInTheDocument();
  expect(screen.getByText("RULE-BAD")).toBeInTheDocument();
  expect(
    screen.getByText(/missing required unit/i, { exact: false }),
  ).toBeInTheDocument();
  // The healthy rule is still listed and still usable.
  expect((await screen.findAllByText("lapse risk")).length).toBeGreaterThan(0);
});

test("does not warn when every rule read cleanly", async () => {
  mockList.mockResolvedValue({
    success: true,
    data: { rules: [RULE], unreadable: [] },
  });
  render(<GovernedRulesTab ontologyId="ont-1" layerType="VKG" />);
  await screen.findAllByText("lapse risk");
  expect(screen.queryByText(/could not be read/i)).not.toBeInTheDocument();
});
