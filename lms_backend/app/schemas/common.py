"""Shared schema primitives: strict bases, sanitised string types, paging."""

from __future__ import annotations

from typing import Annotated, Generic, List, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field

# Rejects the characters used to smuggle markup/templating into stored text.
# Pydantic validates *before* anything reaches the ORM, and the ORM only ever
# emits bound parameters — so this is defence in depth against XSS, not the
# primary control against SQL injection.
_NO_MARKUP = r"^[^<>{}$`\\]*$"

SafeLine = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=160, pattern=_NO_MARKUP
    ),
]
SafeCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$"
    ),
]
SafeText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2000, pattern=r"^[^<>]*$")
]
# bcrypt hard-stops at 72 bytes; anything longer would be silently truncated.
Password = Annotated[str, StringConstraints(min_length=12, max_length=72)]
TimezoneName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=64, pattern=r"^[A-Za-z0-9_+\-/]+$"
    ),
]
PhoneNumber = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=32, pattern=r"^[0-9+\-() ]+$"),
]


class StrictSchema(BaseModel):
    """Base for request bodies: unknown fields are an error, not ignored."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ORMSchema(BaseModel):
    """Base for responses read off SQLAlchemy instances."""

    model_config = ConfigDict(from_attributes=True)


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Envelope for paginated collections."""

    items: List[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pages(self) -> int:
        if self.page_size == 0:
            return 0
        return -(-self.total // self.page_size)  # ceil division


class PaginationParams(StrictSchema):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class MessageResponse(BaseModel):
    message: str
