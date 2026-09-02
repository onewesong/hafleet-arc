"""Requirement-to-capability normalization for agent prompts.

The normalizer deliberately consumes only the ARC requirement tree.  It does not
inspect benchmark/grading tests, making the resulting model useful for arbitrary
domains while giving the implementer a compact, traceable execution contract.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable


_TEXT_KEYS = ("description", "text", "statement", "details", "expected", "acceptance")
_CRITERIA_KEYS = ("acceptance_criteria", "acceptanceCriteria", "criteria", "acceptance")


def _text(value: Any, limit: int = 600) -> str:
    if isinstance(value, str):
        return " ".join(value.split())[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _strings(value: Any, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item_text = next((_text(item.get(key)) for key in _TEXT_KEYS if _text(item.get(key))), "")
            else:
                item_text = _text(item)
            if item_text:
                result.append(item_text)
            if len(result) >= limit:
                break
        return result
    return []


def _references(*values: Any, limit: int = 8) -> list[str]:
    """Extract author-supplied visual/reference paths without reading the files."""
    found: list[str] = []
    pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for value in values:
        text = value if isinstance(value, str) else ""
        for path in pattern.findall(text):
            path = path.strip()
            if path and path not in found:
                found.append(path)
            if len(found) >= limit:
                return found
    return found


def _observable_strings(*values: Any, limit: int = 24) -> list[str]:
    """Extract author-provided UI/API literals without consulting evaluator data.

    Requirement prose frequently embeds exact labels and error messages in quotes.
    Keeping those literals separately prevents a bounded description field from
    dropping the contract an implementer must expose, while remaining domain-neutral.
    """
    found: list[str] = []
    pattern = re.compile(r"[\"“”']([^\"“”']{2,120})[\"“”']")
    for value in values:
        text = value if isinstance(value, str) else ""
        for match in pattern.findall(text):
            literal = " ".join(match.split()).strip()
            if literal and literal not in found and not literal.startswith(("./", "/")):
                found.append(literal)
            if len(found) >= limit:
                return found
    return found


def _scenario(scenario: dict[str, Any], fallback_id: str, index: int) -> dict[str, Any]:
    scenario_id = _text(scenario.get("id") or scenario.get("scenario_id")) or f"{fallback_id}-S{index:03d}"
    name = _text(scenario.get("name") or scenario.get("title")) or scenario_id
    steps = scenario.get("steps") or scenario.get("actions") or []
    normalized_steps: list[dict[str, str]] = []
    if isinstance(steps, list):
        for step in steps[:12]:
            if isinstance(step, dict):
                keyword = _text(step.get("keyword") or step.get("type"))
                content = _text(step.get("content") or step.get("text"))
                action = _text(step.get("action") or step.get("when") or step.get("do"))
                expected = _text(step.get("expected") or step.get("then") or step.get("result"))
                # ARC requirement YAML commonly represents Gherkin steps as
                # {keyword, content}; retain them instead of silently dropping
                # the actual behavior statement.
                if not action and content and keyword.upper() not in {"THEN", "AND_THEN"}:
                    action = content
                if not expected and content and keyword.upper() in {"THEN", "AND_THEN"}:
                    expected = content
                normalized_steps.append({"keyword": keyword, "action": action, "expected": expected, "content": content})
            elif _text(step):
                normalized_steps.append({"action": _text(step), "expected": ""})
    expected = _strings(scenario.get("expected") or scenario.get("outcomes") or scenario.get("acceptance"), 6)
    given = [step["content"] for step in normalized_steps if step.get("keyword", "").upper() == "GIVEN" and step.get("content")]
    when = [step["content"] for step in normalized_steps if step.get("keyword", "").upper() == "WHEN" and step.get("content")]
    then = [step["content"] for step in normalized_steps if step.get("keyword", "").upper() in {"THEN", "AND_THEN"} and step.get("content")]
    return {
        "id": scenario_id,
        "name": name,
        "steps": normalized_steps,
        "transition": {
            "preconditions": given,
            "actions": when,
            "observable_results": then,
        },
        "expected": expected,
        "references": _references(scenario.get("description"), scenario.get("text")),
    }


def _gateway_key(value: str) -> str:
    """Normalize a public GIVEN precondition for cross-scenario grouping.

    Requirement authors often repeat the same prerequisite with only case,
    whitespace, or terminal-punctuation differences.  Keep the original public
    wording for agents, but use this conservative key to discover fan-out without
    inventing domain semantics or consulting evaluator tests.
    """

    normalized = " ".join(str(value or "").casefold().split()).strip()
    return normalized.rstrip(". ;:")


def _gateway_contracts(requirements: list[dict[str, Any]], limit: int = 32) -> list[dict[str, Any]]:
    consumers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"precondition": "", "scenario_ids": [], "requirement_ids": []}
    )
    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        for scenario in requirement.get("scenarios", []):
            scenario_id = str(scenario.get("id") or "")
            transition = scenario.get("transition") if isinstance(scenario, dict) else {}
            preconditions = transition.get("preconditions", []) if isinstance(transition, dict) else []
            seen_in_scenario: set[str] = set()
            for raw in preconditions if isinstance(preconditions, list) else []:
                wording = " ".join(str(raw or "").split()).strip()
                key = _gateway_key(wording)
                if not key or key in seen_in_scenario:
                    continue
                seen_in_scenario.add(key)
                entry = consumers[key]
                entry["precondition"] = entry["precondition"] or wording
                if scenario_id and scenario_id not in entry["scenario_ids"]:
                    entry["scenario_ids"].append(scenario_id)
                if requirement_id and requirement_id not in entry["requirement_ids"]:
                    entry["requirement_ids"].append(requirement_id)

    result = []
    for entry in consumers.values():
        count = len(entry["scenario_ids"])
        if count < 2:
            continue
        result.append(
            {
                **entry,
                "consumer_count": count,
                "release_gate": True,
                "verification_contract": [
                    "Establish this prerequisite through the public UI/API from a clean isolated state.",
                    "Assert its visible state and canonical URL before running dependent scenarios.",
                    "Reload or open a second isolated context and prove the prerequisite can be reconstructed.",
                    "When the prerequisite establishes authentication or another client session, prove two isolated clients can hold it concurrently unless the public requirement explicitly requires single-session behavior.",
                    "If this gate fails, repair it before interpreting downstream failures.",
                ],
            }
        )
    result.sort(key=lambda item: (-int(item["consumer_count"]), str(item["precondition"]).casefold()))
    return result[:limit]


def _state_interference_contract(requirements: list[dict[str, Any]], limit: int = 80) -> dict[str, Any]:
    """Derive a bounded concurrency contract from public state-changing scenarios.

    The classification is intentionally coarse and domain-neutral. It tells agents
    which supplied requirements can interfere when exercised against one service,
    without inventing evaluator fixtures, selectors, or product-specific behavior.
    """

    mutation = re.compile(
        r"\b(add|create|register|save|update|change|edit|modify|delete|remove|cancel|refund|pay|book|reserve|confirm|reset)\b",
        re.IGNORECASE,
    )
    credential = re.compile(r"\b(password|credential|e-?mail|mailbox|mobile|phone|security|login|account)\b", re.IGNORECASE)
    destructive = re.compile(r"\b(delete|remove|cancel|refund|reset|revoke)\b", re.IGNORECASE)
    mutable_requirement_ids: list[str] = []
    credential_requirement_ids: list[str] = []
    destructive_requirement_ids: list[str] = []
    scenario_ids: list[str] = []

    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        # Titles and explicit WHEN/action text are strong signals. Descriptions,
        # acceptance prose, and THEN results often mention related operations only
        # as navigation context, which would over-classify broad parent sections.
        requirement_text = str(requirement.get("title") or "")
        matched = bool(mutation.search(requirement_text))
        credential_matched = bool(credential.search(requirement_text) and mutation.search(requirement_text))
        destructive_matched = bool(destructive.search(requirement_text))
        for scenario in requirement.get("scenarios", []):
            transition = scenario.get("transition") if isinstance(scenario, dict) else {}
            scenario_text = " ".join(
                [str(scenario.get("name") or "")]
                + list(transition.get("actions", []) if isinstance(transition, dict) else [])
            )
            if mutation.search(scenario_text):
                matched = True
                scenario_id = str(scenario.get("id") or "")
                if scenario_id and scenario_id not in scenario_ids and len(scenario_ids) < limit:
                    scenario_ids.append(scenario_id)
            credential_matched = credential_matched or bool(credential.search(scenario_text) and mutation.search(scenario_text))
            destructive_matched = destructive_matched or bool(destructive.search(scenario_text))
        if matched and requirement_id and requirement_id not in mutable_requirement_ids and len(mutable_requirement_ids) < limit:
            mutable_requirement_ids.append(requirement_id)
        if credential_matched and requirement_id and requirement_id not in credential_requirement_ids and len(credential_requirement_ids) < limit:
            credential_requirement_ids.append(requirement_id)
        if destructive_matched and requirement_id and requirement_id not in destructive_requirement_ids and len(destructive_requirement_ids) < limit:
            destructive_requirement_ids.append(requirement_id)

    if not mutable_requirement_ids:
        return {}
    return {
        "mutable_requirement_ids": mutable_requirement_ids,
        "credential_requirement_ids": credential_requirement_ids,
        "destructive_requirement_ids": destructive_requirement_ids,
        "scenario_ids": scenario_ids,
        "release_gate": True,
        "verification_contract": [
            "Exercise state-changing scenarios against one running service with at least two isolated clients, not only one serial in-memory client.",
            "While one client mutates state, prove another active client keeps its independent session and unrelated actor-owned data; a credential change must not silently destroy already-authenticated sessions unless the public requirement explicitly says so.",
            "After create/update/delete/cancel/refund operations, verify the resulting owner-scoped collection and dependent views through the public API/UI and after refresh.",
            "When a mutation reserves, locks, decrements, or otherwise consumes a finite resource, observe the resource before and after the mutation and prove every cancel/refund/expiry path restores or finalizes it exactly once as required; reap expired reservations before every public availability projection or competing reservation attempt, without requiring the original owner to revisit its order.",
            "For countdowns, expirations, polling, progress, or other time-driven UI, prove liveness by observing at least two distinct values or the required terminal transition; rendering a static timer-shaped string is insufficient.",
            "Run destructive scenarios from deterministic reset state and repeat the same project-owned suite; a prior invocation must not consume fixtures required by the next invocation.",
            "Use atomic persistence updates so concurrent session, collection, inventory, or order mutations cannot overwrite unrelated state through a stale read-modify-write cycle.",
            "Do not solve interference by serializing all tests, adding evaluator-only reset hooks, or weakening observable assertions.",
        ],
    }


def _identity_consistency_contract(
    requirements: list[dict[str, Any]], seed_contracts: list[dict[str, Any]], limit: int = 80
) -> dict[str, Any]:
    """Derive an authentication identity gate only from public requirement text.

    Authentication is a high-fan-out prerequisite. A generated application can appear
    correct for one account while ambiguous seed identifiers, destructive session
    replacement, or single-use recovery state break every protected downstream flow.
    This contract stays domain-neutral and never consults evaluator fixtures.
    """

    auth = re.compile(
        r"\b(auth(?:entication)?|sign[ -]?in|log[ -]?in|register|account|username|password|credential|mobile|phone|e-?mail|reset|recover)\b",
        re.IGNORECASE,
    )
    identifier = re.compile(r"\b(username|mobile|phone|e-?mail|account|identifier|login)\b", re.IGNORECASE)
    recovery = re.compile(r"\b(reset|recover|forgot|verification|challenge|token)\b", re.IGNORECASE)
    requirement_ids: list[str] = []
    identifier_requirement_ids: list[str] = []
    recovery_requirement_ids: list[str] = []

    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        pieces = [
            str(requirement.get("title") or ""),
            str(requirement.get("description") or ""),
            *[str(item) for item in requirement.get("acceptance", [])],
        ]
        for scenario in requirement.get("scenarios", []):
            pieces.append(str(scenario.get("name") or ""))
            transition = scenario.get("transition") if isinstance(scenario, dict) else {}
            if isinstance(transition, dict):
                pieces.extend(str(item) for key in ("preconditions", "actions", "observable_results") for item in transition.get(key, []))
        text = " ".join(pieces)
        if auth.search(text) and requirement_id and requirement_id not in requirement_ids:
            requirement_ids.append(requirement_id)
        if identifier.search(text) and requirement_id and requirement_id not in identifier_requirement_ids:
            identifier_requirement_ids.append(requirement_id)
        if recovery.search(text) and requirement_id and requirement_id not in recovery_requirement_ids:
            recovery_requirement_ids.append(requirement_id)
        if len(requirement_ids) >= limit:
            break

    seed_signals = [
        item for item in seed_contracts
        if auth.search(f"{item.get('category', '')} {item.get('statement', '')}")
    ][:limit]
    if not requirement_ids and not seed_signals:
        return {}
    return {
        "requirement_ids": requirement_ids,
        "identifier_requirement_ids": identifier_requirement_ids,
        "recovery_requirement_ids": recovery_requirement_ids,
        "seed_signals": seed_signals,
        "release_gate": True,
        "verification_contract": [
            "Build one canonical identity resolver for every publicly accepted sign-in identifier; username, e-mail, mobile/phone, aliases, and case normalization must resolve deterministically to exactly one owning account.",
            "Give app-owned seeded accounts unique values for every accepted unique identifier. Never silently choose the first record when an identifier is duplicated; reject ambiguous credentials with the public validation/error contract.",
            "From clean isolated persistence, iterate every public seeded account through every supported login identifier and assert the returned durable identity, protected navigation, and refresh reconstruction belong to that same account.",
            "Exercise two concurrent sessions for the same account and two different accounts. A later login, credential update, or logout must not overwrite or revoke unrelated active sessions unless the public requirements explicitly require it.",
            "When recovery/reset is supported, create overlapping challenges and consume each by its own opaque token. Creating or consuming one challenge must not invalidate another still-valid challenge unless the public contract explicitly says so.",
            "Repeat the identity gateway suite after process restart and from a migrated older persisted data shape; bootstrap/migration must preserve user-owned unique identifiers and repair only app-owned ambiguous defaults.",
        ],
    }


def _root_data_contracts(tree: dict[str, Any], limit: int = 160) -> list[dict[str, Any]]:
    """Preserve author-supplied seed/example records as first-class contracts.

    These records are part of the requirement input, not evaluator fixtures.  Keeping
    them in the compact model prevents feature-scoped turns from silently dropping
    accounts, routes, empty states, or other prerequisite data described at ROOT.
    """

    raw = tree.get("data") or tree.get("fixtures") or tree.get("seed_data") or []
    if not isinstance(raw, list):
        return []
    contracts: list[dict[str, Any]] = []
    for group in raw:
        if len(contracts) >= limit:
            break
        if isinstance(group, dict):
            category = _text(group.get("category") or group.get("name") or group.get("title"), 160)
            items = group.get("items") or group.get("records") or group.get("values") or []
            if not isinstance(items, list):
                items = [items]
            for item in items:
                statement = _text(item, 800)
                if statement:
                    contracts.append({"category": category, "statement": statement})
                if len(contracts) >= limit:
                    break
        else:
            statement = _text(group, 800)
            if statement:
                contracts.append({"category": "", "statement": statement})
    return contracts


def build_capability_model(tree: dict[str, Any], *, max_requirements: int = 160) -> dict[str, Any]:
    """Return a bounded, deterministic capability matrix from any requirement tree."""

    requirements: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], parent_id: str = "") -> None:
        if len(requirements) >= max_requirements:
            return
        req_id = _text(node.get("id") or node.get("req_id") or node.get("requirement_id"))
        if not req_id:
            req_id = parent_id or "REQUIREMENT"
        title = _text(node.get("name") or node.get("title")) or req_id
        dependencies = node.get("dependencies") or node.get("depends_on") or []
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        dependencies = [_text(item, 120) for item in dependencies if _text(item, 120)][:16] if isinstance(dependencies, list) else []
        description = next((_text(node.get(key)) for key in _TEXT_KEYS if _text(node.get(key))), "")
        criteria: list[str] = []
        for key in _CRITERIA_KEYS:
            criteria.extend(_strings(node.get(key), 8))
        scenarios_raw = node.get("scenarios") or node.get("use_cases") or []
        scenarios = [_scenario(item, req_id, i) for i, item in enumerate(scenarios_raw, 1) if isinstance(item, dict)] if isinstance(scenarios_raw, list) else []
        # Keep this contract domain-neutral.  It gives an agent an observable
        # boundary checklist without revealing (or depending on) any evaluator
        # implementation details.
        requirements.append(
            {
                "id": req_id,
                "title": title,
                "type": _text(node.get("type")) or "requirement",
                "dependencies": dependencies,
                "description": description,
                "references": _references(node.get("description"), node.get("text"), node.get("acceptance")),
                "observable_strings": _observable_strings(
                    node.get("description"), node.get("text"), node.get("acceptance"),
                    *criteria,
                    *[step.get("action", "") for scenario in scenarios for step in scenario.get("steps", [])],
                    *[step.get("expected", "") for scenario in scenarios for step in scenario.get("steps", [])],
                ),
                "acceptance": criteria[:12],
                "scenarios": scenarios[:12],
                "observable_contract": {
                    "actor_and_preconditions": "Identify the actor, required permissions, and initial state.",
                    "success": "Describe the visible response and durable state after a valid action.",
                    "invalid_and_failure": "Define validation, conflict, unauthorized/not-found, and server failure behavior when applicable.",
                    "navigation_and_refresh": "Define a stable public entry point and behavior after direct navigation or refresh for web flows.",
                    "persistence": "Verify state survives the supported storage boundary or process restart when the requirement implies persistence.",
                    "ui_api_parity": "The public UI and API must expose the same state transition, result data, and structured errors.",
                },
            }
        )
        children = node.get("children") or node.get("requirements") or []
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child, req_id)

    visit(tree)
    dependent_counts: dict[str, int] = {}
    for requirement in requirements:
        for dependency in requirement.get("dependencies", []):
            dependent_counts[dependency] = dependent_counts.get(dependency, 0) + 1
    for requirement in requirements:
        count = dependent_counts.get(str(requirement.get("id") or ""), 0)
        requirement["dependent_count"] = count
        requirement["critical_prerequisite"] = count >= 2
    seed_contracts = _root_data_contracts(tree)
    return {
        "source": "arc_requirement_tree",
        "requirements": requirements,
        "seed_contracts": seed_contracts,
        "gateway_contracts": _gateway_contracts(requirements),
        "state_interference_contract": _state_interference_contract(requirements),
        "identity_consistency_contract": _identity_consistency_contract(requirements, seed_contracts),
        # This is a domain-neutral browser/API contract.  It gives an agent a
        # stable interoperability target without revealing evaluator details or
        # prescribing product-specific selectors.
        "web_contract": {
            "routing": [
                "Every requirement-mentioned page has a stable canonical URL path.",
                "Opening a deep URL directly and refreshing it renders the same page; the server falls back to the app shell for client routes.",
                "Navigation uses real links/buttons with accessible names; hash-only navigation is not the sole way to reach a page.",
                "Each route has one canonical spelling and normalizes trailing slashes, query defaults, and legacy aliases without losing state.",
                "Authenticated routes have an explicit unauthenticated redirect or visible access-denied state.",
                "Successful account creation hands off to sign-in unless the requirement explicitly establishes an authenticated session; successful sign-in returns to the intended route or the stable application landing page.",
                "Every successful or rejected form action has an asserted canonical URL outcome, including the conventional outcome when prose specifies only success feedback.",
            ],
            "forms": [
                "Every input, select, radio group, checkbox, and date control has a visible label or equivalent accessible name.",
                "Choose native control semantics that match the requirement and authored reference: enumerable choices use a labeled select/radio group, while free-form values use inputs; dialogs and expandable forms expose a stable accessible region name.",
                "Choose native interactive elements by intent: anchors with real href values navigate, while buttons perform submissions, sign-out, deletion, payment, cancellation, toggles, and other state-changing actions; do not emulate actions with anchors plus preventDefault or navigation with spans/divs.",
                "Every requirement-named clickable affordance is discoverable by its semantic role and accessible name. When the authenticated identity is the entry to an available account/profile center, render the displayed identity as a real named link to that destination rather than a non-interactive span.",
                "Required fields expose required semantics and deterministic validation messages without losing entered values.",
                "Validation is enforced at the API boundary as well as the UI; errors identify the field or business conflict and use a non-2xx status for rejected mutations.",
                "Submit controls expose a stable accessible name and prevent duplicate submissions while pending.",
                "Inactive wizard steps, closed dialogs, collapsed panels, and elements carrying the hidden attribute are absent from layout, pointer interaction, focus order, and the accessibility tree; author CSS must not accidentally override their hidden state.",
            ],
            "api": [
                "JSON endpoints use consistent success and error envelopes with appropriate HTTP status codes.",
                "Mutations validate input, persist state, and return the resulting resource or a structured error.",
                "Loading, empty, error, and retry states are observable in the UI for asynchronous data.",
            ],
            "state": [
                "Successful auth and mutations update navigation and visible state immediately.",
                "Creating a new authenticated session for an account must not invalidate another active client session unless the public requirement explicitly specifies single-session behavior.",
                "Authentication gateways must be proven with at least two isolated clients: both clients remain authenticated, refresh reconstructs each identity, protected navigation works, and logout affects only the initiating session unless global logout is explicitly required.",
                "Refresh/reload reconstructs state from the public API or durable storage rather than in-memory-only fixtures.",
                "Every async view has explicit loading, empty, recoverable error, and retry states; stale data is not presented as a successful response.",
                "Logout clears client credentials and protects authenticated views after reload.",
                "Runtime dates and seeded records derive from the requirement-configured clock/environment rather than generation-time constants.",
                "Every accepted authentication identifier resolves to exactly one durable identity; app-owned seed accounts use unique identifiers and ambiguous matches are rejected rather than selecting the first record.",
                "Concurrent password/account-recovery challenges are token-scoped and do not erase another still-valid challenge unless the public contract explicitly requires single-challenge behavior.",
            ],
            "collections": [
                "A filter, sort, pagination, date switch, or navigation preset must project the actual visible collection, update its count and empty state, and preserve the same projection in the canonical URL and after refresh.",
                "When a requirement says the current actor, account holder, owner, or self record always appears and cannot be deleted, derive that protected row from the authenticated durable identity on every read, deduplicate legacy persisted owner rows, omit destructive controls for it, and prove ordinary collection mutations and restart cannot remove it.",
                "When a collection query changes, stale rows must be hidden or marked busy synchronously before the URL/result transition; clients must never observe the new query together with rows from the previous projection.",
                "Executable tests must inspect every visible data item after each composed filter, not merely wait for one matching item that was already present before the transition.",
                "Filter and sort fixtures must be non-vacuous: provide multiple distinct selectable values and prove the action excludes or reorders at least one item; a changed URL alone is not behavioral evidence.",
                "When requirement prose specifies hover, focus, keyboard activation, click, drag, or native selection, exercise that exact interaction modality in a browser test rather than substituting a different action.",
                "Composite or multi-segment results use a semantic list item/article/card boundary and visibly label every segment, connection/wait duration, total duration, price, and action; ordering and result limits are deterministic.",
            ],
        },
        "coverage_rules": [
            "Trace every leaf requirement and scenario to an implementation behavior and executable test.",
            "For each user-visible flow cover success, invalid input, empty state, failure response, and refresh/persistence where applicable.",
            "For each API cover input validation, authorization, success response, error status, and persistence where applicable.",
            "Exercise requirement-named controls through their native semantic roles: links for navigation and buttons for state-changing actions; verify any displayed account identity that opens an account center is a named link and survives authentication refresh.",
            "When requirements mention seeded/example records, verify a fresh process bootstraps deterministic app-owned data without evaluator setup.",
            "When author-provided visual references exist, verify the corresponding observable layout/content states without relying on hidden selectors.",
            "Execute prerequisite scenarios before dependents and treat a failed high-fan-out prerequisite as blocking because it can invalidate many downstream flows.",
            "Build a compact smoke gate for every repeated GIVEN precondition in gateway_contracts and run those gates before dependent scenarios.",
            "For authentication/session gateways, exercise two isolated clients concurrently and reject implementations where a later login silently invalidates an earlier active session without an explicit requirement.",
            "For identity gateways, iterate every public seeded account through every supported sign-in identifier and prove each identifier resolves uniquely to its owning durable identity after refresh and restart.",
            "For recovery gateways, create overlapping reset challenges and prove consuming one opaque token does not destroy a different still-valid challenge unless explicitly required.",
            "For public state-changing scenarios, run two isolated clients against one service and prove credential, profile, collection, inventory, and order mutations preserve unrelated sessions and actor-owned state.",
            "For finite-resource workflows, assert inventory or capacity before reservation, after reservation, and after every cancellation/refund/expiry path, including a competing client reading availability and reserving immediately after expiry before the original owner calls an order endpoint, so resources are neither leaked nor released twice.",
            "For time-driven behavior, observe the value changing or reaching its specified terminal state under a bounded clock interval; a static countdown/progress label does not prove liveness.",
            "Repeat destructive project-owned scenarios from deterministic reset state and reject non-atomic persistence or fixture consumption that makes a second invocation fail.",
            "For collection filters and sorts, prove the URL, visible count, empty state, and every visible item describe one atomic projection; a stale pre-transition row is a failed gate.",
            "For every filter and sort, use non-vacuous fixtures with distinct values, prove at least one item is excluded or reordered, and assert the complete order in every required direction instead of checking only query parameters.",
            "Exercise the exact public interaction modality named by each requirement, including hover, focus, keyboard activation, click, drag, or native selection; an easier substitute does not prove the stated behavior.",
            "For protected self/owner collection entries, delete all optional persisted owner copies in a test fixture and prove the authenticated actor is still projected exactly once, has no destructive action, and remains after deleting unrelated rows and restarting.",
            "For composite results, verify semantic item boundaries and labeled aggregate values in addition to segment details and ordering.",
            "For each scenario assert the full GIVEN/WHEN/THEN transition, including canonical URL, visible result, API state, and reload behavior where applicable.",
            "For multi-step or conditionally rendered UI, assert both sides of every transition: the inactive controls are truly hidden and non-focusable before/after the action, while exactly one active control set is visible. Do not silence duplicate-control failures merely by narrowing a locator.",
            "Do not invent hidden acceptance-test details; derive behavior only from the supplied requirements and observable contracts.",
        ],
    }


__all__ = ["build_capability_model"]
