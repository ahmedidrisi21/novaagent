"""Answers to questions asked mid-call, from the company knowledge the agent is granted.

Read-only, by construction: the only thing this module can do is search an index. Nothing
a caller says on the line can make the gateway write, send or call — those are proposed
after the call, through the same policy as everything else.

**Answer from sources or not at all.** A hit becomes the passage itself, cited, for the
voice to paraphrase. No hit becomes an explicit "not found" with an instruction to offer
a written follow-up, because a voice model with nothing to go on will otherwise improvise
a price or a policy, and say it in a confident human voice.

Scope is the agent's own knowledge grant, applied as a SQL predicate by
:func:`nova.knowledge.query.search` — the caller's words choose the query, never the corpus.
Every lookup is written to the audit log: "which document did the agent read out on that
call" must be answerable afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Sequence

from nova.audit import AuditLog, new_correlation_id
from nova.communications.contracts import Answerer
from nova.knowledge.query import SNIPPET_CLOSE, SNIPPET_ELLIPSIS, SNIPPET_OPEN

#: Passages read out per answer. More than two is a monologue on a phone line.
MAX_PASSAGES = 2

NOT_FOUND = (
    "The company's records do not cover this. Say so plainly and offer to follow up in "
    "writing; do not guess."
)

#: ``(question, source_ids) -> hits``, each hit a dict with ``snippet`` and ``citation``.
Search = Callable[[str, Sequence[str]], list]


def index_search(index_path: Path) -> Search:
    """Search the tenant's knowledge index, opened read-only per lookup."""
    from nova.knowledge.index import KnowledgeIndex

    def search(question: str, source_ids: Sequence[str]) -> list:
        if not index_path.is_file():
            return []
        with KnowledgeIndex.open(index_path, create=False) as index:
            hits = index.search(question, source_ids=list(source_ids), limit=MAX_PASSAGES)
            return [hit.to_dict() for hit in hits]

    return search


def _clean(snippet: str) -> str:
    """The snippet as speakable text: the index's highlight markers removed."""
    text = snippet or ""
    for marker in (SNIPPET_OPEN, SNIPPET_CLOSE, SNIPPET_ELLIPSIS.strip()):
        text = text.replace(marker, " ")
    return " ".join(text.split())


def knowledge_answerer(
    *,
    call_id: str,
    agent: str,
    sources: Sequence[str],
    search: Search,
    audit: AuditLog,
) -> Answerer:
    """The answerer for one call: this agent's grant, this call's audit subject."""
    granted = tuple(sources)

    def answer(question: str) -> str:
        hits = search(question, granted) if granted else []
        passages = [hit for hit in hits if _clean(hit.get("snippet", ""))][:MAX_PASSAGES]
        audit.record(
            "communications.knowledge.searched",
            correlation_id=new_correlation_id(),
            subject=call_id,
            detail={
                "agent": agent,
                "question": question[:300],
                "sources": list(granted),
                "citations": [hit.get("citation", "") for hit in passages],
            },
        )
        if not passages:
            return NOT_FOUND
        lines = [
            f"From {hit.get('citation', 'company records')}: {_clean(hit['snippet'])}"
            for hit in passages
        ]
        return (
            "Answer only from these passages; if they do not answer the question, say so.\n"
            + "\n".join(lines)
        )

    return answer


def grants_by_agent(agents: Sequence) -> Mapping[str, tuple[str, ...]]:
    """``agent id -> knowledge source ids`` from a bundle's agent specs."""
    return {spec.id: tuple(spec.knowledge.sources) for spec in agents}
