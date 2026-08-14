---
name: writing
description: "Basic Markdown conventions for inline-discussion documents: linking local files with line or character ranges and preferring Mermaid for diagrams. Use when writing content that will be opened in the inline-discussion browser."
---

# Inline-discussion writing conventions

## Local file links

Use ordinary Markdown links whose target ends in one of these suffixes:

- `path/to/file.md:40` — line 40.
- `path/to/file.md:40-45` — lines 40 through 45.
- `path/to/file.md:40:5-45:12` — line 40, character 5 through line 45, character 12.

Lines and characters are 1-based and inclusive. Prefer a path relative to the Markdown document because it remains portable when the project moves. Use an absolute filesystem path or a server-root path beginning with `/` only when a document-relative path is unsuitable. Do not hard-code the discussion server's host or port because it changes between launches.

```markdown
[Provisioning decision](./decision.md:40-45)
[Exact setting](../infra/main.tf:18:3-18:31)
```

Use angle brackets around a Markdown link target containing spaces. Do not append this range syntax to remote HTTP(S) URLs.

## Diagram preference

Prefer a fenced `mermaid` diagram over an SVG or PNG when expressing structure, flow, sequence, state, or relationships. Keep screenshots and other images whose exact visual appearance is the evidence.
