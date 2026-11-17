import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import DeltaPreview from "../components/DeltaPreview";

test("renders plan rows and fires rebuild with delta scope", async () => {
  const onCompute = jest.fn().mockResolvedValue({
    rebuildSet: ["db.a"],
    removed: ["db.gone"],
    schemaChanged: ["db.a"],
    dataChanged: [],
    added: [],
    fkNeighbors: [],
    unchanged: ["db.b"],
    perTable: {
      "db.a": { reason: "schema" },
      "db.gone": { reason: "removed" },
    },
  });
  const onRebuild = jest.fn();
  render(
    <DeltaPreview
      onCompute={onCompute}
      onRebuild={onRebuild}
      totalTables={2}
    />,
  );
  fireEvent.click(screen.getByText(/check for updates/i));
  await waitFor(() => screen.getByText("db.a"));
  expect(screen.getByText("db.gone")).toBeInTheDocument();
  fireEvent.click(screen.getByText(/rebuild changed/i));
  expect(onRebuild).toHaveBeenCalledWith(
    "delta",
    expect.objectContaining({
      rebuildSet: ["db.a"],
      removed: ["db.gone"],
    }),
  );
});

test("shows no-changes state and disables rebuild changed when rebuildSet empty", async () => {
  const onCompute = jest.fn().mockResolvedValue({
    rebuildSet: [],
    removed: [],
    schemaChanged: [],
    dataChanged: [],
    added: [],
    fkNeighbors: [],
    unchanged: ["db.a"],
    perTable: {},
  });
  render(
    <DeltaPreview
      onCompute={onCompute}
      onRebuild={jest.fn()}
      totalTables={1}
    />,
  );
  fireEvent.click(screen.getByText(/check for updates/i));
  await waitFor(() => screen.getByText(/no changes/i));
});
