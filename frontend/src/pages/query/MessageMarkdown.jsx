/**
 * MessageMarkdown — render assistant/user message text as GitHub-flavored
 * markdown so the model's headings, lists, code, and pipe-tables show up
 * formatted instead of as raw characters in the chat bubble.
 *
 * Wraps ``ReactMarkdown`` with ``remark-gfm`` (tables, strikethrough,
 * task lists, autolinks) and overrides a few node renderers so tables and
 * code blocks sit inside the Cloudscape bubble without overflowing.
 */
import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const components = {
  // Tighten paragraph spacing so single-line replies don't get a big margin.
  p: ({ node, ...props }) => (
    <p style={{ margin: "0 0 0.5em 0", lineHeight: 1.5 }} {...props} />
  ),
  // Wrap tables so wide pipe-tables (10+ columns) scroll horizontally
  // inside the bubble instead of overflowing the chat container.
  table: ({ node, ...props }) => (
    <div style={{ overflowX: "auto", margin: "0.5em 0" }}>
      <table
        style={{
          borderCollapse: "collapse",
          fontSize: "0.85em",
          minWidth: "100%",
        }}
        {...props}
      />
    </div>
  ),
  th: ({ node, ...props }) => (
    <th
      style={{
        border: "1px solid #d5dbdb",
        padding: "4px 8px",
        background: "#f4f4f4",
        textAlign: "left",
      }}
      {...props}
    />
  ),
  td: ({ node, ...props }) => (
    <td
      style={{ border: "1px solid #d5dbdb", padding: "4px 8px" }}
      {...props}
    />
  ),
  code: ({ node, inline, className, children, ...props }) => {
    if (inline) {
      return (
        <code
          style={{
            background: "#f4f4f4",
            padding: "1px 4px",
            borderRadius: 3,
            fontSize: "0.9em",
          }}
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },
  pre: ({ node, ...props }) => (
    <pre
      style={{
        background: "#f4f4f4",
        padding: 8,
        borderRadius: 4,
        overflowX: "auto",
        fontSize: "0.85em",
        margin: "0.5em 0",
      }}
      {...props}
    />
  ),
  ul: ({ node, ...props }) => (
    <ul style={{ margin: "0.25em 0 0.5em 1.25em" }} {...props} />
  ),
  ol: ({ node, ...props }) => (
    <ol style={{ margin: "0.25em 0 0.5em 1.25em" }} {...props} />
  ),
  h1: ({ node, ...props }) => (
    <h1 style={{ fontSize: "1.2em", margin: "0.5em 0 0.25em 0" }} {...props} />
  ),
  h2: ({ node, ...props }) => (
    <h2 style={{ fontSize: "1.1em", margin: "0.5em 0 0.25em 0" }} {...props} />
  ),
  h3: ({ node, ...props }) => (
    <h3 style={{ fontSize: "1.05em", margin: "0.5em 0 0.25em 0" }} {...props} />
  ),
  hr: () => (
    <hr
      style={{
        border: "none",
        borderTop: "1px solid #e9ebed",
        margin: "0.5em 0",
      }}
    />
  ),
};

export default function MessageMarkdown({ text }) {
  if (!text) return null;
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {text}
    </ReactMarkdown>
  );
}
