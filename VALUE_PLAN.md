# Value Plan – Expense Tracker

This document lists potential improvements to the client‑side expense
tracking app and scores each using the **ICE** framework.

| Opportunity | Impact | Confidence | Effort | ICE | Notes |
| --- | --- | --- | --- | --- | --- |
| **Edit expenses:** enable users to modify existing entries instead of deleting and re‑adding them | 7 | 8 | 5 | **11.2** | Improves usability and reduces frustration when correcting mistakes. |
| **Import/export data:** provide buttons to download expenses as JSON/CSV and upload from a file | 6 | 7 | 4 | **10.5** | Allows users to back up data or move between browsers. |
| **Search by description/date:** add a search box to filter expenses quickly | 4 | 8 | 3 | **10.7** | Helpful for users with many records. |
| **Dynamic categories:** allow adding/removing categories from the UI instead of editing HTML manually | 5 | 7 | 6 | **5.8** | Adds flexibility but requires more state management. |
| **Dark‑mode toggle:** support a dark colour scheme to improve accessibility | 4 | 9 | 4 | **9.0** | Relatively straightforward with CSS variables. |
| **Unit tests:** set up Jest and React Testing Library to verify core components | 5 | 6 | 6 | **5.0** | Increases confidence when refactoring. |
| **CI pipeline:** create a GitHub Actions workflow for linting and testing | 4 | 8 | 4 | **8.0** | Automates quality control. |

### Priorities

The highest impact/ease tasks are editing expenses and import/export of
data.  Implementing search and a dark‑mode toggle also rank highly and
can be tackled in subsequent batches.  Tests and CI provide long‑term
maintainability benefits and should be set up alongside feature work.