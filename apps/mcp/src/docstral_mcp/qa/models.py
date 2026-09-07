from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"

_ABSTENTION_MESSAGE = (
    "I couldn't find enough information in the Mistral documentation to answer "
    "this question."
)


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    top_k: int = Field(ge=1)

    @field_validator("query")
    @classmethod
    def _query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class RetrievedChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rank: int = Field(ge=1)
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content_hash: str = Field(pattern=_SHA256_PATTERN)
    locator: str = Field(min_length=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    content: str = Field(min_length=1)
    score: float = Field(allow_inf_nan=False)

    @field_validator("title")
    @classmethod
    def _title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _offsets_must_be_ordered(self) -> Self:
        if self.start_offset > self.end_offset:
            raise ValueError("start_offset must not exceed end_offset")
        return self


class RetrievalResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    chunks: tuple[RetrievedChunk, ...]


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    url: HttpUrl

    @field_validator("title")
    @classmethod
    def _title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value


class AnswerResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(min_length=1)
    abstained: bool
    citations: tuple[Citation, ...]

    @model_validator(mode="after")
    def _state_must_be_consistent(self) -> Self:
        if self.abstained:
            if self.answer != _ABSTENTION_MESSAGE or self.citations:
                raise ValueError(
                    "an abstention must use the fixed message and no citations"
                )
        elif not self.answer.strip() or not self.citations:
            raise ValueError("an answer must be non-blank and cite retrieved evidence")
        return self


class _AnswerDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str
    evidence_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _state_must_be_consistent(self) -> Self:
        if not self.answer:
            if self.evidence_ids:
                raise ValueError("an abstention must have no evidence IDs")
        elif not self.answer.strip() or not self.evidence_ids:
            raise ValueError("an answer must be non-blank and reference evidence")
        return self
