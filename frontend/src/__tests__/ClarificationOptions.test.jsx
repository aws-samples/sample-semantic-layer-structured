/**
 * Tests for ClarificationOptions — the clickable disambiguation chips rendered
 * on a clarification turn (docs/plans/disambiguation.md Part 2).
 *
 * Asserts: one button per option (showing the label), clicking a button calls
 * onSelect with the option *id* (the exact key resolve_clarification_reply
 * matches first), disabled state propagates, and an empty/absent options list
 * renders nothing.
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import ClarificationOptions from "../pages/query/ClarificationOptions";

const clarification = {
  original_question: "List the top 5 most common party types",
  options: [
    { id: "party_license", label: "party_license (database: normalized)" },
    { id: "party_banking", label: "party_banking (database: normalized)" },
  ],
};

test("renders one button per option with its label", () => {
  render(
    <ClarificationOptions clarification={clarification} onSelect={() => {}} />,
  );
  expect(
    screen.getByRole("button", {
      name: /party_license \(database: normalized\)/i,
    }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", {
      name: /party_banking \(database: normalized\)/i,
    }),
  ).toBeInTheDocument();
});

test("clicking an option calls onSelect with the option id", () => {
  const onSelect = jest.fn();
  render(
    <ClarificationOptions clarification={clarification} onSelect={onSelect} />,
  );
  fireEvent.click(
    screen.getByRole("button", {
      name: /party_license \(database: normalized\)/i,
    }),
  );
  expect(onSelect).toHaveBeenCalledTimes(1);
  expect(onSelect).toHaveBeenCalledWith("party_license");
});

test("disabled prop disables every option button", () => {
  render(
    <ClarificationOptions
      clarification={clarification}
      disabled
      onSelect={() => {}}
    />,
  );
  for (const btn of screen.getAllByRole("button")) {
    expect(btn).toBeDisabled();
  }
});

test("renders nothing when there are no options", () => {
  const { container } = render(
    <ClarificationOptions
      clarification={{ options: [] }}
      onSelect={() => {}}
    />,
  );
  expect(container).toBeEmptyDOMElement();
});
