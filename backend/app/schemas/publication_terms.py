from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Term = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)]


class PublicationTermsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terms: list[Term] = Field(max_length=1000)
    expected_revision: int = Field(ge=0, strict=True)

    @field_validator("terms")
    @classmethod
    def check_terms(cls, terms: list[str]) -> list[str]:
        if any("\x00" in term for term in terms):
            raise ValueError("词条不能包含空字符")
        if len(set(terms)) != len(terms):
            raise ValueError("词表包含重复词条")
        return terms


class PublicationTermsOut(BaseModel):
    terms: list[str]
    revision: int
