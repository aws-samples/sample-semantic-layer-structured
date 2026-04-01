import '@testing-library/jest-dom';

// Mock Cloudscape Design components
jest.mock('@cloudscape-design/components', () => ({
  Alert: ({ type, children }) => <div data-testid={`alert-${type}`}>{children}</div>,
  Box: ({ children, variant, ...props }) => <div data-testid="box" data-variant={variant} {...props}>{children}</div>,
  SpaceBetween: ({ children }) => <div data-testid="space-between">{children}</div>,
  StatusIndicator: ({ type, children }) => (
    <div data-testid={`status-indicator-${type}`}>{children}</div>
  ),
  Button: ({ children, ariaLabel, disabled, ...props }) => (
    <button data-testid="button" aria-label={ariaLabel} disabled={disabled} {...props}>
      {children}
    </button>
  ),
  Select: ({ selectedOption, options, onChange, disabled, ...props }) => (
    <select
      role="combobox"
      data-testid="select"
      value={selectedOption?.value || ''}
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
  ColumnLayout: ({ children }) => <div data-testid="column-layout">{children}</div>,
  Multiselect: (props) => <select data-testid="multiselect" {...props} />,
  Modal: ({ visible, header, children, footer, onDismiss }) => (
    visible && (
      <div role="dialog" data-testid="modal">
        {header && <div>{header}</div>}
        {children}
        {footer && <div>{footer}</div>}
      </div>
    )
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
}));

// Mock Amplify
jest.mock('aws-amplify', () => ({
  Auth: {
    currentSession: jest.fn(),
  },
}));

// Mock Amplify UI
jest.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }) => children,
  useAuthenticator: () => ({
    user: { username: 'testuser' },
  }),
}));
