---
name: demo
description: Prepare an interview-ready live demo plan for this repository
when-to-use: Before a live demo or interview, to extract the strongest story, commands, and likely follow-up questions
argument-hint: focus
context: fork
allowed-tools: list_files, read_file, search, run_shell
user-invocable: true
---
You are preparing a polished live demo for this repository.

Use the repository itself as evidence. Inspect the README, project configuration, the current brand/customization layer, and any docs that explain architecture or differentiators.

If `$ARGUMENTS` is provided, treat it as the focus for the demo. Examples:
- `backend`
- `agent architecture`
- `memory and compact`
- `interview`

Produce the answer with these sections:

## 30-second pitch
Explain what this project is and why it is interesting in a short, spoken style.

## What Is Custom Here
List the most important customizations in this repo compared with a generic local coding agent. Ground every point in files you actually inspected.

## Demo Flow
Give a concrete step-by-step sequence for a live demo. Include what to show first, what command to type, and what to say while showing it.

## Commands To Type
Provide only the exact commands worth typing live. Prefer the shortest commands that work in this repo.

## Likely Follow-up Questions
List the most likely technical questions an interviewer might ask, with concise but strong answers.

## Risks To Avoid
Call out anything that is brittle, confusing, or likely to waste time in a live demo.

Constraints:
- Keep the output practical and spoken, not essay-like.
- Prefer a strong, short demo over a broad, messy one.
- Do not invent features; verify claims from repository files or command output.
- When useful, mention file paths that support the point.
