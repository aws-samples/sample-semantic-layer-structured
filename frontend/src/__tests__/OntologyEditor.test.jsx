import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OntologyEditor from "../components/OntologyEditor";
import { ontologyAPI } from "../services/api";

jest.mock("../services/api");

describe("OntologyEditor", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    ontologyAPI.getOntologyVersions = jest.fn().mockResolvedValue({
      success: true,
      data: {
        versions: [
          {
            version: "v1",
            status: "completed",
            updatedAt: "2026-03-03T00:00:00Z",
          },
        ],
      },
    });
    ontologyAPI.getOntologyContent = jest.fn().mockResolvedValue({
      success: true,
      data: {
        content:
          "<http://ex/PolicyHolder> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://g> .",
        version: "v1",
      },
    });
  });

  it("renders the N-Quads content after loading", async () => {
    render(<OntologyEditor id="abc" />);
    await waitFor(() => {
      expect(screen.getByText(/PolicyHolder/)).toBeInTheDocument();
    });
  });

  it("shows loading indicator before data arrives", () => {
    ontologyAPI.getOntologyVersions = jest.fn(
      () =>
        new Promise((resolve) => {
          setTimeout(
            () =>
              resolve({
                success: true,
                data: { versions: [{ version: "v1", status: "completed" }] },
              }),
            100,
          );
        }),
    );
    ontologyAPI.getOntologyContent = jest.fn(
      () =>
        new Promise((resolve) => {
          setTimeout(
            () =>
              resolve({
                success: true,
                data: { content: "<test>", version: "v1" },
              }),
            100,
          );
        }),
    );

    render(<OntologyEditor id="abc" />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it("shows Annotate selection button after text is selected in raw view", async () => {
    render(<OntologyEditor id="abc" />);
    await waitFor(() => screen.getByText(/PolicyHolder/));
    await userEvent.click(screen.getByLabelText("View raw N-Quads"));
    window.getSelection = jest
      .fn()
      .mockReturnValue({ toString: () => "PolicyHolder" });
    fireEvent.mouseUp(document);
    expect(await screen.findByText(/annotate selection/i)).toBeInTheDocument();
  });

  it("opens modal and saves annotation on submit", async () => {
    render(<OntologyEditor id="abc" />);
    await waitFor(() => screen.getByText(/PolicyHolder/));
    await userEvent.click(screen.getByLabelText("View raw N-Quads"));
    window.getSelection = jest
      .fn()
      .mockReturnValue({ toString: () => "PolicyHolder" });
    fireEvent.mouseUp(document);
    await userEvent.click(await screen.findByText(/annotate selection/i));
    await waitFor(() => screen.getByRole("dialog"));
    await userEvent.type(screen.getByRole("textbox"), "Add a subclass");
    await userEvent.click(screen.getByText(/confirm/i));
    await waitFor(() => {
      expect(screen.getByText("Add a subclass")).toBeInTheDocument();
    });
  });

  it("highlights annotated text in the viewer", async () => {
    render(<OntologyEditor id="abc" />);
    await waitFor(() => screen.getByText(/PolicyHolder/));
    await userEvent.click(screen.getByLabelText("View raw N-Quads"));
    window.getSelection = jest
      .fn()
      .mockReturnValue({ toString: () => "PolicyHolder" });
    fireEvent.mouseUp(document);
    await userEvent.click(await screen.findByText(/annotate selection/i));
    await waitFor(() => screen.getByRole("dialog"));
    await userEvent.type(screen.getByRole("textbox"), "Test comment");
    await userEvent.click(screen.getByText(/confirm/i));
    await waitFor(() => {
      const marks = document.querySelectorAll("mark");
      expect(marks.length).toBeGreaterThan(0);
    });
  });

  it("deletes annotation when delete button clicked", async () => {
    render(<OntologyEditor id="abc" />);
    await waitFor(() => screen.getByText(/PolicyHolder/));
    await userEvent.click(screen.getByLabelText("View raw N-Quads"));
    window.getSelection = jest
      .fn()
      .mockReturnValue({ toString: () => "PolicyHolder" });
    fireEvent.mouseUp(document);
    await userEvent.click(await screen.findByText(/annotate selection/i));
    await waitFor(() => screen.getByRole("dialog"));
    await userEvent.type(screen.getByRole("textbox"), "to be deleted");
    await userEvent.click(screen.getByText(/confirm/i));
    await waitFor(() => screen.getByText("to be deleted"));
    await userEvent.click(screen.getByLabelText(/delete annotation/i));
    await waitFor(() => expect(screen.queryByText("to be deleted")).toBeNull());
  });

  it("shows version selector with loaded versions", async () => {
    render(<OntologyEditor id="abc" />);
    await waitFor(() => screen.getByText(/v1/));
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("generate button is disabled when no annotations", async () => {
    render(<OntologyEditor id="abc" />);
    await waitFor(() => screen.getByText(/generate new version/i));
    expect(
      screen.getByText(/generate new version/i).closest("button"),
    ).toBeDisabled();
  });

  it("calls reviseOntology and reloads on completion", async () => {
    // Mock the interval to execute immediately
    const originalSetInterval = global.setInterval;
    global.setInterval = jest.fn((callback) => {
      callback();
      return 1;
    });

    ontologyAPI.reviseOntology = jest
      .fn()
      .mockResolvedValue({ success: true, data: { nextVersion: "v2" } });
    ontologyAPI.getBuildStatus = jest
      .fn()
      .mockResolvedValue({ success: true, data: { status: "completed" } });
    ontologyAPI.getOntologyVersions = jest
      .fn()
      .mockResolvedValueOnce({
        success: true,
        data: {
          versions: [
            {
              version: "v1",
              status: "completed",
              updatedAt: "2026-03-03T00:00:00Z",
            },
          ],
        },
      })
      .mockResolvedValueOnce({
        success: true,
        data: {
          versions: [
            { version: "v2", status: "completed", updatedAt: "" },
            { version: "v1", status: "completed", updatedAt: "" },
          ],
        },
      });
    ontologyAPI.getOntologyContent = jest.fn().mockResolvedValue({
      success: true,
      data: {
        content:
          "<http://ex/PolicyHolder> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://g> .",
        version: "v1",
      },
    });

    render(<OntologyEditor id="abc" />);
    await waitFor(() => screen.getByText(/generate new version/i));
    await userEvent.click(screen.getByLabelText("View raw N-Quads"));

    // Add one annotation first
    window.getSelection = jest
      .fn()
      .mockReturnValue({ toString: () => "PolicyHolder" });
    fireEvent.mouseUp(document);
    await userEvent.click(await screen.findByText(/annotate selection/i));
    await waitFor(() => screen.getByRole("dialog"));
    await userEvent.type(screen.getByRole("textbox"), "Change this");
    await userEvent.click(screen.getByText(/confirm/i));

    await waitFor(() => screen.getByText(/1 annotation/));

    // Click generate button
    const generateButton = screen
      .getByText(/generate new version/i)
      .closest("button");
    expect(generateButton).not.toBeDisabled();
    await userEvent.click(generateButton);

    // Verify reviseOntology was called with correct parameters
    await waitFor(() =>
      expect(ontologyAPI.reviseOntology).toHaveBeenCalledWith("abc", "v1", [
        expect.objectContaining({ highlightedText: "PolicyHolder" }),
      ]),
    );

    // Verify polling completed and reloaded
    await waitFor(() =>
      expect(ontologyAPI.getOntologyVersions).toHaveBeenCalledTimes(2),
    );

    global.setInterval = originalSetInterval;
  });
});
