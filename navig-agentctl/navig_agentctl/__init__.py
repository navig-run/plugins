"""navig-agentctl — the Agent Control Center (devops section).

Phase 1 (Observer): a read-only view over what is already true — live agent
sessions, git worktrees and task records — exposed as deck routes, a CLI
(``navig agentctl``) and the Control Center OS tile. It never launches,
stops or merges anything; that is Phase 2+.

The Agent Monitor (0.4.0) adds the run ledger: sessions, subagents, commands and
``npm run land`` as persistent, verified, redacted execution records.
"""

__version__ = "0.4.2"
