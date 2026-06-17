/**
 * Tests for FeedbackTab — regression guard for the response-envelope bug.
 *
 * feedbackAPI.list resolves to the handleResponse envelope {success, data},
 * where the backend body {feedback: [...]} lives at res.data.feedback. The tab
 * previously read res.feedback (undefined) → always rendered "No feedback
 * recorded yet" even when rows existed in DynamoDB. These tests assert the
 * stored rows actually render.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import FeedbackTab from "../components/FeedbackTab";

const mockList = jest.fn();
const mockRemove = jest.fn();

jest.mock("../services/api", () => ({
  feedbackAPI: {
    list: (...a) => mockList(...a),
    remove: (...a) => mockRemove(...a),
  },
}));

beforeEach(() => {
  mockList.mockReset();
  mockRemove.mockReset();
});

test("renders feedback rows from the res.data.feedback envelope", async () => {
  // Shape mirrors handleResponse: {success, data: <backend body>}.
  mockList.mockResolvedValue({
    success: true,
    data: {
      feedback: [
        {
          feedbackId: "f1",
          rating: "down",
          comment: "wrong table picked",
          question: "how many parties?",
          createdAt: "2026-06-05T04:09:04.983+00:00",
          userId: "u1",
        },
      ],
    },
  });

  render(
    <FeedbackTab ontologyId="semantic-rag-multi_table_curated_layer-ac2ea13f" />,
  );

  // The stored comment must appear — proving res.data.feedback was read.
  expect(await screen.findByText("wrong table picked")).toBeInTheDocument();
  // The empty-state sentinel must NOT be shown.
  expect(
    screen.queryByText(/No feedback recorded yet/i),
  ).not.toBeInTheDocument();
  // Queried by the ontology id the chat writes under.
  expect(mockList).toHaveBeenCalledWith(
    "semantic-rag-multi_table_curated_layer-ac2ea13f",
    { limit: 200 },
  );
});

test("shows the user email (not the raw sub) and a human-readable date", async () => {
  mockList.mockResolvedValue({
    success: true,
    data: {
      feedback: [
        {
          feedbackId: "f1",
          rating: "up",
          comment: "great",
          createdAt: "2026-06-05T04:09:04.983+00:00",
          userId: "d448b448-0021-706a-dfcc-1e4b0695f2af",
          userEmail: "alice@example.com",
        },
      ],
    },
  });

  render(<FeedbackTab ontologyId="ont-1" />);

  // Email is shown; the raw Cognito sub is not.
  expect(await screen.findByText("alice@example.com")).toBeInTheDocument();
  expect(
    screen.queryByText("d448b448-0021-706a-dfcc-1e4b0695f2af"),
  ).not.toBeInTheDocument();
  // The Created cell is humanized via toLocaleString — the raw ISO string with
  // its "T" separator must not appear verbatim.
  expect(
    screen.queryByText("2026-06-05T04:09:04.983+00:00"),
  ).not.toBeInTheDocument();
});

test("falls back to the sub when a row has no email (old rows)", async () => {
  mockList.mockResolvedValue({
    success: true,
    data: {
      feedback: [
        {
          feedbackId: "f1",
          rating: "up",
          comment: "great",
          createdAt: "2026-06-05T04:09:04.983+00:00",
          userId: "sub-legacy",
          // no userEmail — pre-change row
        },
      ],
    },
  });

  render(<FeedbackTab ontologyId="ont-1" />);

  expect(await screen.findByText("sub-legacy")).toBeInTheDocument();
});

test("shows the empty state when the backend returns no rows", async () => {
  mockList.mockResolvedValue({ success: true, data: { feedback: [] } });
  render(<FeedbackTab ontologyId="ont-1" />);
  expect(
    await screen.findByText(/No feedback recorded yet/i),
  ).toBeInTheDocument();
});

test("surfaces an error when the list call fails", async () => {
  mockList.mockResolvedValue({ success: false, error: "boom" });
  render(<FeedbackTab ontologyId="ont-1" />);
  expect(await screen.findByText("boom")).toBeInTheDocument();
});
