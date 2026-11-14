from typing import Any

from pydantic import BaseModel, Field


class GroupLeg(BaseModel):
    """One flight of a group fare, as the supplier describes it (normalized at the vendor boundary)."""

    direction: str = Field(description="outbound or return")
    flight_number: str | None = None
    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    departure_time: str | None = Field(None, description="HH:MM")
    arrival_date: str | None = None
    arrival_time: str | None = Field(None, description="HH:MM")
    baggage: str | None = Field(None, description="Checked baggage in kg as the supplier sends it, e.g. '20'.")
    hand_carry: str | None = Field(None, description="Hand carry in kg as the supplier sends it, e.g. '7'.")
    meal: str | int | None = None


class GroupTicket(BaseModel):
    vendor: str
    pnr: str | None = None
    departure_date: str | None = None
    sector: str | None = None
    airline: str | None = None
    airline_logo: str | None = None
    price: str | int | float | None = Field(None, description="Supplier price per adult, as sent.")
    currency: str | None = None
    remaining: int | None = None
    updated_at: str | None = None
    sold: str | int | None = None
    baggage: str | int | None = Field(None, description="Checked baggage of the outbound leg.")
    hand_carry: str | int | None = Field(None, description="Hand carry of the outbound leg.")
    meal: str | int | None = None
    ticket_type: str | int | None = None
    legs: list[GroupLeg] = Field(default_factory=list)
    subparts: list[dict[str, Any]] = Field(default_factory=list)
    #: Supplier-side identifiers a hold needs (Skypass ticket id and open_seats_slot). Never returned to callers.
    source_ticket_id: str | None = None
    source_slot: str | None = None
    #: Group identity, copied onto our booking when a hold is created. Only for viewers with the supplier
    #: names permission. Dhillu & Sahi: first subpart's group_id and id. Skypass: no group ID; ticket id.
    supplier_group_id: str | None = None
    supplier_ticket_id: str | None = None


#: How each group source is named to viewers with the supplier names permission.
PROVIDER_NAMES = {"dhillu_sahi": "Dhillu & Sahi", "skypass": "Skypass"}


class GroupSource(BaseModel):
    """Which supplier fare this is. Only for viewers with the supplier names permission."""

    provider: str = Field(description="Source code, e.g. dhillu_sahi or skypass.")
    provider_name: str = Field(description="Display name, e.g. Dhillu & Sahi.")
    supplier_pnr: str | None = None
    supplier_group_id: str | None = None
    supplier_ticket_id: str | None = None

    @classmethod
    def of(cls, provider: str | None, supplier_pnr: str | None, supplier_group_id: str | None,
           supplier_ticket_id: str | None) -> "GroupSource | None":
        if not provider:
            return None
        return cls(provider=provider, provider_name=PROVIDER_NAMES.get(provider, provider), supplier_pnr=supplier_pnr or None,
                   supplier_group_id=supplier_group_id or None, supplier_ticket_id=supplier_ticket_id or None)


class GroupInventory(BaseModel):
    id: str
    vendor: str
    sector: str
    airline: str | None = None
    airline_logo: str | None = None
    tickets: list[GroupTicket]
