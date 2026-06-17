/**
 * Tests for LessonsLearnedTab — regression guard for the response-envelope bug.
 *
 * lessonsAPI.list resolves to the handleResponse envelope {success, data},
 * where the backend body {lessons: [...]} lives at res.data.lessons. The tab
 * previously read res.lessons (undefined) → always rendered "No lessons
 * recorded yet" even when AgentCore Memory returned records. These tests assert
 * the records actually render.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

const mockList = jest.fn();
const mockRemove = jest.fn();

jest.mock("../services/api", () => ({
  lessonsAPI: {
    list: (...a) => mockList(...a),
    remove: (...a) => mockRemove(...a),
  },
}));

import LessonsLearnedTab from "../components/LessonsLearnedTab";

beforeEach(() => {
  mockList.mockReset();
  mockRemove.mockReset();
});

test("renders lesson records from the res.data.lessons envelope", async () => {
  mockList.mockResolvedValue({
    success: true,
    data: {
      lessons: [
        {
          memoryRecordId: "m1",
          content: "Prefer admin_codes for code lookups",
          createdAt: "2026-06-05T04:09:04.983+00:00",
        },
      ],
    },
  });

  render(<LessonsLearnedTab ontologyId="ont-1" />);

  expect(
    await screen.findByText("Prefer admin_codes for code lookups"),
  ).toBeInTheDocument();
  expect(
    screen.queryByText(/No lessons recorded yet/i),
  ).not.toBeInTheDocument();
  expect(mockList).toHaveBeenCalledWith("ont-1", { limit: 100 });
});

test("shows the empty state when AgentCore Memory returns no records", async () => {
  mockList.mockResolvedValue({ success: true, data: { lessons: [] } });
  render(<LessonsLearnedTab ontologyId="ont-1" />);
  expect(
    await screen.findByText(/No lessons recorded yet/i),
  ).toBeInTheDocument();
});

test("surfaces an error when the list call fails", async () => {
  mockList.mockResolvedValue({ success: false, error: "memory unavailable" });
  render(<LessonsLearnedTab ontologyId="ont-1" />);
  expect(await screen.findByText("memory unavailable")).toBeInTheDocument();
});
