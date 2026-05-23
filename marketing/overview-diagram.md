# Voitta — Unified Semantic Layer for Engineering Data

Voitta continuously indexes your organisation's data across every system of record,
making it accessible to the tools and workflows that turn raw information into decisions.

---

## The Big Picture

```mermaid
graph LR
    subgraph SOURCES["Where your data lives today"]
        direction TB
        S1["Document & File Stores\nshared drives · intranets · file servers"]
        S2["Project & Work Tracking\nticketing · wikis · change management"]
        S3["Source & Version Control\ncode · configuration · release history"]
        S4["Engineering & Design\nPLMs · CAD models · BOMs · specifications"]
    end

    VOITTA["Voitta\n\nunified semantic layer\nover all your engineering data"]

    subgraph OUTPUTS["What gets delivered"]
        direction TB
        O1["Reports & Presentations\nauto-generated from live data"]
        O2["Data Exports\nstructured tables · spreadsheets · dashboards"]
        O3["Answers & Summaries\ncross-system synthesis on demand"]
        O4["Automated Workflows\nscheduled digests · change alerts · approvals"]
    end

    S1 --> VOITTA
    S2 --> VOITTA
    S3 --> VOITTA
    S4 --> VOITTA

    VOITTA --> O1
    VOITTA --> O2
    VOITTA --> O3
    VOITTA --> O4
```

---

## How It Works in Practice

The key pattern that sets Voitta apart from simple search:

```mermaid
sequenceDiagram
    actor User as Stakeholder
    participant AI as AI Assistant
    participant V as Voitta
    participant D as Source Systems

    User->>AI: "Produce a change summary for the pump assembly since Q1"

    AI->>V: semantic search across all indexed systems
    V-->>AI: relevant excerpts + URIs to source files

    AI->>D: fetch full documents via URIs\n(specs · tickets · drawings · approvals)
    D-->>AI: raw data

    AI->>AI: process, correlate, format

    AI-->>User: structured report\n(Word · Excel · slides · PDF)
```

> Voitta does not just answer questions — it gives an AI assistant **direct access to the underlying data**,
> so the output can be a fully worked deliverable, not a summary.

---

## What Teams Get

| Deliverable | How Voitta enables it |
|---|---|
| **Change reports** | Correlate design revisions, tickets, and approvals across systems automatically |
| **Specification exports** | Pull the latest approved specs from PLM, CAD, and docs into one structured file |
| **Status dashboards** | Aggregate live data from project tracking, engineering, and document stores |
| **Compliance packages** | Collect and format evidence from multiple systems on demand |
| **Onboarding materials** | Synthesise relevant context from all sources for a given project or product area |
| **Cross-system search** | One question, one answer — regardless of where the information lives |

---

## Why This Is Different

Traditional approaches require someone to know *which system* holds the answer, then log in, search, export, and manually stitch results together.

Voitta removes that friction: the knowledge layer sits above all systems, speaks the same language as modern AI tools, and returns not just text but **links to the actual source files** — so downstream processing works on real, complete data rather than excerpts.
