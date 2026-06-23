"""Colony Agent Platform (CAP) — strato Control/Execution Plane.

Package introdotto con la spec "Colony Agent Platform v0.1" (giu 2026).
Contiene:

- ``db``            — persistenza unificata (SQLAlchemy, default SQLite,
                      swap-ready verso PostgreSQL via ``COLONY_DB_URL``)
- ``models``        — entità: agents, skills, pipelines, executions,
                      claims, heartbeats, deliverables, events
- ``audit``         — audit trail (ogni operazione → riga in events)
- ``skill_meta``    — parsing metadata estesi delle skill (skill.yaml /
                      frontmatter SKILL.md) per lo Skill Registry
- ``registry_sync`` — sync filesystem → DB di agenti e skill al boot
- ``pipelines``     — Pipeline Registry: pipes.yaml, stati, versioning,
                      validator
- ``provisioning``  — creazione board/lane Trello da una pipeline
- ``strategy``      — invocazione Strategy Agent + Pipeline Generator
- ``executions``    — lifecycle esecuzioni: stati task formali, heartbeat,
                      stale detection, deliver strutturato, retention
                      workspace
- ``selection``     — Agent Selection Engine (skill, permessi, costo,
                      priorità, success-rate)
"""
