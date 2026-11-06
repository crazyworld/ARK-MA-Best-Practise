"""Record ownership/coverage checks. Does not certify semantic event quality."""
from c2_runtime import unique_ids


def inspect_ownership(source, events, eventness, id_col="record_id"):
    source_ids = set(unique_ids(source, id_col))
    unique_ids(events, "event_id")
    unique_ids(eventness)
    en = {r["record_id"]: r for r in eventness}
    if set(en) - source_ids:
        raise ValueError("eventness references records outside this input")
    owner = {}
    degraded = []
    for event in events:
        members = event.get("member_record_ids")
        if not isinstance(members, list) or not members:
            raise ValueError("Event must have members")
        if type(event.get("member_count")) is not int or event["member_count"] != len(members):
            raise ValueError("member_count mismatch")
        if not isinstance(event.get("event_name"), str) or not event["event_name"].strip():
            raise ValueError("Missing event name")
        for rid in members:
            if rid not in source_ids:
                raise ValueError("Event contains unknown record")
            if rid in owner:
                raise ValueError("Duplicate event membership")
            if en.get(rid, {}).get("eventness") == "NON_EVENT":
                raise ValueError("NON_EVENT has event ownership")
            owner[rid] = event
            if (event.get("block_archive_failed") or event.get("block_archive_refusal_degraded")
                    or event.get("model_omission") or (event.get("fallback") and not event.get("singleton"))):
                degraded.append(rid)
        for facet in event.get("facets") or []:
            if set(facet.get("members") or []) - set(members):
                raise ValueError("Facet references records outside its event")
    non_event = sorted(rid for rid in source_ids if rid not in owner and
                       en.get(rid, {}).get("eventness") == "NON_EVENT")
    technical_missing = sorted(source_ids - set(owner) - set(non_event))
    return {"schema": "c2-output-coverage/1", "input_count": len(source),
            "assigned_count": len(owner), "non_event_count": len(non_event),
            "non_event_ids": non_event, "technical_missing_ids": technical_missing,
            "degraded_ids": sorted(degraded),
            "original_retained_ids": sorted(rid for rid, event in owner.items()
                                            if event.get('retention_status') == 'ORIGINAL_RETAINED'),
            "coverage_status": "PARTIAL" if technical_missing else "COMPLETE",
            "semantic_quality": "NOT_EVALUATED", "review_completion": "NOT_VERIFIED"}
