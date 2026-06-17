import "@testing-library/jest-dom";

// JSDOM in this CRA toolchain doesn't expose TextEncoder/TextDecoder as
// globals — required by api.streamChat tests that build ReadableStream
// chunks. Pull them off Node's `util` and put them on `global`.
import { TextEncoder, TextDecoder } from "util";
import { ReadableStream } from "stream/web";
if (typeof global.TextEncoder === "undefined") {
  global.TextEncoder = TextEncoder;
}
if (typeof global.TextDecoder === "undefined") {
  global.TextDecoder = TextDecoder;
}
if (typeof global.ReadableStream === "undefined") {
  global.ReadableStream = ReadableStream;
}

// Mock Cloudscape Design components
jest.mock("@cloudscape-design/components", () => ({
  Alert: ({ type, children }) => (
    <div data-testid={`alert-${type}`}>{children}</div>
  ),
  Box: ({ children, variant, ...props }) => (
    <div data-testid="box" data-variant={variant} {...props}>
      {children}
    </div>
  ),
  SpaceBetween: ({ children }) => (
    <div data-testid="space-between">{children}</div>
  ),
  StatusIndicator: ({ type, children }) => (
    <div data-testid={`status-indicator-${type}`}>{children}</div>
  ),
  Button: ({ children, ariaLabel, disabled, ...props }) => (
    <button
      data-testid="button"
      aria-label={ariaLabel}
      disabled={disabled}
      {...props}
    >
      {children}
    </button>
  ),
  Select: ({ selectedOption, options, onChange, disabled, ...props }) => (
    <select
      role="combobox"
      data-testid="select"
      value={selectedOption?.value || ""}
      onChange={(e) => {
        const selected = options.find((o) => o.value === e.target.value);
        onChange?.({ detail: { selectedOption: selected } });
      }}
      disabled={disabled}
      {...props}
    >
      {options.map((opt) => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
  ),
  Header: ({ variant, children, actions }) => (
    <div data-testid="header" data-variant={variant}>
      {children}
      {actions}
    </div>
  ),
  FormField: ({ children, label, ...props }) => (
    <div data-testid="form-field" {...props}>
      {label && <label>{label}</label>}
      {children}
    </div>
  ),
  Toggle: ({ children, ...props }) => (
    <label data-testid="toggle">
      <input type="checkbox" {...props} />
      {children}
    </label>
  ),
  Badge: ({ children }) => <span data-testid="badge">{children}</span>,
  ColumnLayout: ({ children }) => (
    <div data-testid="column-layout">{children}</div>
  ),
  Multiselect: (props) => <select data-testid="multiselect" {...props} />,
  Modal: ({ visible, header, children, footer, onDismiss }) =>
    visible && (
      <div role="dialog" data-testid="modal">
        {header && <div>{header}</div>}
        {children}
        {footer && <div>{footer}</div>}
      </div>
    ),
  Textarea: ({ value, onChange, placeholder, rows, ...props }) => (
    <textarea
      data-testid="textarea"
      value={value}
      onChange={(e) => onChange?.({ detail: { value: e.target.value } })}
      placeholder={placeholder}
      rows={rows}
      {...props}
    />
  ),
  Table: ({ items, empty, columnDefinitions, header }) => (
    <div data-testid="table">
      {header}
      <table>
        <tbody>
          {items && items.length > 0 ? (
            items.map((item, idx) => (
              <tr key={idx}>
                {(columnDefinitions || []).map((col) => (
                  <td key={col.id}>
                    {col.cell ? col.cell(item) : item[col.id]}
                  </td>
                ))}
              </tr>
            ))
          ) : (
            <tr>
              <td>{empty || "Empty"}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  ),
  ButtonGroup: ({ items, onItemClick }) => (
    <div data-testid="button-group">
      {(items || []).map((item) => (
        <button
          key={item.id}
          aria-label={item.text}
          aria-pressed={item.pressed || false}
          onClick={() => onItemClick?.({ detail: { id: item.id } })}
        >
          {item.text}
        </button>
      ))}
    </div>
  ),
  ExpandableSection: ({ headerText, header, children, defaultExpanded }) => (
    <section data-testid="expandable-section">
      <div>{headerText || header}</div>
      <div>{children}</div>
    </section>
  ),
  Pagination: ({ currentPageIndex, pagesCount, onChange }) => (
    <div
      data-testid="pagination"
      data-current={currentPageIndex}
      data-pages={pagesCount}
    >
      <button onClick={() => onChange?.({ detail: { currentPageIndex: 1 } })}>
        page
      </button>
    </div>
  ),
  Spinner: () => <span data-testid="spinner" />,
  TextFilter: ({ filteringText, onChange, filteringPlaceholder }) => (
    <input
      data-testid="text-filter"
      value={filteringText || ""}
      placeholder={filteringPlaceholder}
      onChange={(e) =>
        onChange?.({ detail: { filteringText: e.target.value } })
      }
    />
  ),
  Container: ({ children, header }) => (
    <div data-testid="container">
      {header}
      {children}
    </div>
  ),
  Tabs: ({ tabs, activeTabId, onChange }) => (
    <div data-testid="tabs">
      {(tabs || []).map((tab) => (
        <div key={tab.id}>
          <div
            className="tab-label"
            onClick={() => onChange?.({ detail: { activeTabId: tab.id } })}
          >
            {tab.label}
          </div>
          <div className="tab-content">{tab.content}</div>
        </div>
      ))}
    </div>
  ),
}));

// Mock Amplify
jest.mock("aws-amplify", () => ({
  Auth: {
    currentSession: jest.fn(),
  },
}));

// Mock Amplify UI
jest.mock("@aws-amplify/ui-react", () => ({
  Authenticator: ({ children }) => children,
  useAuthenticator: () => ({
    user: { username: "testuser" },
  }),
}));
