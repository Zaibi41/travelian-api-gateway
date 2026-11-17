"""Shared leg rule for group suppliers whose listing gives flights in order without saying which are the return.

A ticket is a return when, after its outbound flights, a flight leaves from the outbound destination (it turns back):
that flight and the ones after it are the return. One-way tickets never match, so they are unchanged. Skypass says it
itself (sector_type Out / In) and doesn't use this."""

from __future__ import annotations

from app.schemas.group_ticketing import GroupLeg


def mark_return(legs: list[GroupLeg], outbound_destination: str | None) -> list[GroupLeg]:
    if not outbound_destination:
        return legs
    for index, leg in enumerate(legs):
        if index > 0 and leg.origin == outbound_destination:
            return legs[:index] + [later.model_copy(update={"direction": "return"}) for later in legs[index:]]
    return legs
