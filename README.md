# OmniTrain (Core Backend)

Offline-first, adaptive training and periodization state-engine for endurance and strength sports.

## Features
- **Deterministic Periodization Engine:** Evidence-based training planning (Daniels / Pfitzinger) with volume progression, mesocycles, and tapering.
- **Local Semantic Extraction:** Interfacing with local LLMs (Ollama / GGUF) for free-text onboarding and check-in parsing into strictly validated JSON schemas.
- **Robust State Reconcile:** Safe guardrails for sickness, pain/injury deloads, missed workouts, and RPE monitoring.
- **SQLite Persistence:** Clean relational schema with support for multi-goal and multi-sport extensibility.

See [`SPECIFICATION.md`](SPECIFICATION.md) for full architectural details and schemas.
